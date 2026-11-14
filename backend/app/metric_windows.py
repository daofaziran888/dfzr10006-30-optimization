"""指标采集窗口仓库：按「采集器 + 窗口键」管理完成标记、断点、水位与公式批次。

设计要点（对应 METR-0001 / METR-0003 采集窗口缺陷修复）：

1. 完成标记：窗口键 = 采集器编号 + 窗口序号，窗口只允许完成一次。重复到达直接读取
   已有标记与快照，不再累加样本，修复短间隔下恢复时第一窗口被重复计入、结果偏大的问题。
2. 断点续采：失败的采集只把已成功样本累积进未完成窗口（断点），恢复时只处理未完成窗口。
3. 持久水位：状态以 JSON 原子落盘，刷新页面/重启进程后仍是同一份水位，重新打开页面
   从该水位继续。
4. 公式快照：聚合公式切换时，旧窗口快照保留旧公式，并把旧口径归档为历史批次；新窗口走
   新公式并记录切换批次。
5. 并发控制：同一「采集器 + 窗口键」串行处理，两个请求争抢同一窗口时只允许一个完成标记。
6. 同窗口序号：采集数据快照与服务健康诊断读取同一份窗口序号的数据。
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.store import store

MODULE = "metric"

# 支持的聚合公式：代码 -> 中文口径。切换公式时快照按各自批次的公式冻结。
FORMULAS: dict[str, str] = {
    "sum": "求和",
    "avg": "平均值",
    "max": "最大值",
    "min": "最小值",
    "count": "计数",
}
DEFAULT_FORMULA = "sum"

# 状态文件路径：可用环境变量覆盖（测试时指向临时目录），默认放在后端运行目录下。
STATE_PATH = Path(
    os.environ.get("METRIC_WINDOW_STATE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "metric_windows.json"))
)


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _coerce_samples(raw: Any) -> list[float]:
    """把入参样本规整成数值列表；非数值样本直接丢弃，避免污染聚合口径。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    samples: list[float] = []
    for item in raw:
        try:
            samples.append(float(item))
        except (TypeError, ValueError):
            continue
    return samples


def aggregate(samples: list[float], formula: str) -> float | int | None:
    """按指定公式计算聚合值；count 返回整数，其余返回浮点，空窗口返回 None。"""
    if not samples:
        return None
    if formula == "sum":
        return sum(samples)
    if formula == "avg":
        return sum(samples) / len(samples)
    if formula == "max":
        return max(samples)
    if formula == "min":
        return min(samples)
    if formula == "count":
        return len(samples)
    raise ValueError(f"不支持的聚合公式：{formula}")


class WindowStore:
    """采集窗口状态仓库。进程内单例，状态持久化到 JSON 文件，窗口级加锁。"""

    def __init__(self, path: Path | str = STATE_PATH) -> None:
        self._path = Path(path)
        self._global_lock = threading.RLock()
        self._window_locks: dict[str, threading.Lock] = {}
        self._state: dict[str, Any] = self._load()

    # ---------- 持久化 ----------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and "collectors" in data:
                    return data
            except (json.JSONDecodeError, OSError):
                # 文件损坏时不拖垮服务，从空状态重建；正常路径不会走到这里。
                pass
        return {"version": 1, "collectors": {}}

    def _persist(self) -> None:
        """原子写盘：先写临时文件再替换，保证刷新/重启读到的总是完整状态。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def reset(self) -> None:
        """清空全部窗口状态（仅供测试使用）。"""
        with self._global_lock:
            self._state = {"version": 1, "collectors": {}}
            self._persist()

    # ---------- 基础读取 ----------

    def _window_lock(self, collector_no: str, window_key: str) -> threading.Lock:
        with self._global_lock:
            lock_key = f"{collector_no}::{window_key}"
            lock = self._window_locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._window_locks[lock_key] = lock
            return lock

    def _collector_entry(self, collector_no: str) -> dict[str, Any] | None:
        """确认采集器在指标采集中已登记，返回其窗口状态；未登记返回 None。"""
        registered = any(
            str(row.get("采集器编号", "")).strip() == collector_no
            for row in store.rows(MODULE)
        )
        if not registered:
            return None
        collector = self._state["collectors"].get(collector_no)
        if collector is None:
            collector = self._new_collector()
            self._state["collectors"][collector_no] = collector
        return collector

    @staticmethod
    def _new_collector() -> dict[str, Any]:
        batch_no = "BATCH-0001"
        return {
            "next_seq": 1,
            "watermark": 0,
            "active_batch": batch_no,
            "batches": [
                {
                    "batch_no": batch_no,
                    "formula": DEFAULT_FORMULA,
                    "formula_label": FORMULAS[DEFAULT_FORMULA],
                    "switch_seq": None,
                    "status": "生效中",
                    "opened_at": _now_text(),
                    "closed_at": None,
                }
            ],
            "windows": {},
        }

    def _find_window(self, collector: dict[str, Any], window_key: str) -> dict[str, Any] | None:
        return collector["windows"].get(window_key)

    def get_watermark(self, collector_no: str) -> dict[str, Any] | None:
        """读取采集器的持久水位与批次概况。"""
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None
            active = next(b for b in collector["batches"] if b["batch_no"] == collector["active_batch"])
            incomplete = [
                key for key, window in collector["windows"].items() if not window["completed"]
            ]
            return {
                "采集器编号": collector_no,
                "水位窗口序号": collector["watermark"],
                "下一窗口序号": collector["next_seq"],
                "当前批次": collector["active_batch"],
                "当前聚合公式": active["formula"],
                "当前聚合口径": active["formula_label"],
                "未完成窗口数": len(incomplete),
                "未完成窗口": sorted(incomplete),
                "批次总数": len(collector["batches"]),
            }

    def list_windows(self, collector_no: str) -> dict[str, Any] | None:
        """列出采集器的全部窗口（完成标记 + 断点），用于关联清单展示同一水位。"""
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None
            windows = sorted(collector["windows"].values(), key=lambda item: item["seq"])
            return {
                "采集器编号": collector_no,
                "水位窗口序号": collector["watermark"],
                "窗口列表": windows,
            }

    def list_batches(self, collector_no: str) -> dict[str, Any] | None:
        """读取公式切换批次历史，旧口径同步归档在这里。"""
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None
            return {
                "采集器编号": collector_no,
                "当前批次": collector["active_batch"],
                "批次历史": list(reversed(collector["batches"])),
            }

    # ---------- 采集写入 ----------

    def ingest(
        self,
        collector_no: str,
        *,
        window_key: str | None = None,
        samples: list[float] | None = None,
        complete: bool = False,
        formula: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """提交一个采集窗口的样本（断点续采 / 完成标记）。

        返回 (窗口视图, 说明)；采集器未登记时窗口视图为 None。
        """
        samples = _coerce_samples(samples)
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None, f"指标采集器 {collector_no} 不存在或已归档"
            # 窗口键在全局锁内生成，保证序号分配与窗口键一致。
            if not window_key:
                window_key = f"win-{collector['next_seq']:04d}"

        # 同一「采集器 + 窗口键」串行：争抢同一窗口的两个请求只会有一个完成标记。
        with self._window_lock(collector_no, window_key):
            with self._global_lock:
                window = self._find_window(collector, window_key)

                if window is not None and window["completed"]:
                    # 幂等：同一窗口重复到达，读取已有完成标记与快照，不再累加。
                    return deepcopy(window), "窗口已完成，读取已有完成标记，未重复累加"

                if window is None:
                    seq = collector["next_seq"]
                    active_batch = collector["active_batch"]
                    active = next(b for b in collector["batches"] if b["batch_no"] == active_batch)
                    effective_formula = active["formula"]
                    effective_label = active["formula_label"]
                else:
                    seq = window["seq"]
                    effective_formula = window["formula"]
                    effective_label = window["formula_label"]

                # 公式先校验、后写入：同一窗口内不允许口径漂移，拒绝时不污染断点。
                if formula is not None and formula != effective_formula:
                    return None, (
                        f"窗口 {window_key} 按「{effective_label}」口径采集，"
                        f"不能在窗口内切换公式，请先完成本窗口再切换聚合方式"
                    )

                if window is None:
                    window = {
                        "window_key": window_key,
                        "seq": seq,
                        "collector_no": collector_no,
                        "completed": False,
                        "samples": [],
                        "sample_count": 0,
                        "batch_no": collector["active_batch"],
                        "formula": effective_formula,
                        "formula_label": effective_label,
                        "snapshot": None,
                        "opened_at": _now_text(),
                        "completed_at": None,
                        "updated_at": _now_text(),
                    }
                    collector["windows"][window_key] = window
                    collector["next_seq"] += 1

                # 断点续采：失败部分的样本累加到未完成窗口，作为恢复断点保留。
                if samples:
                    window["samples"].extend(samples)
                    window["sample_count"] = len(window["samples"])
                window["updated_at"] = _now_text()

                if complete:
                    window["completed"] = True
                    window["completed_at"] = _now_text()
                    value = aggregate(window["samples"], window["formula"])
                    # 采集数据快照：公式与批次随窗口冻结，后续切换公式不影响旧快照。
                    window["snapshot"] = {
                        "window_key": window_key,
                        "seq": window["seq"],
                        "collector_no": collector_no,
                        "formula": window["formula"],
                        "formula_label": window["formula_label"],
                        "batch_no": window["batch_no"],
                        "samples": list(window["samples"]),
                        "value": value,
                        "captured_at": window["completed_at"],
                    }
                    # 持久水位只随完成标记推进；未完成窗口不占水位。
                    collector["watermark"] = max(collector["watermark"], window["seq"])

                self._persist()
                return deepcopy(window), "窗口已完成并写入完成标记" if complete else "样本已写入断点，窗口保持未完成"

    def recover(self, collector_no: str) -> tuple[dict[str, Any] | None, str]:
        """恢复采集：只处理未完成窗口，已完成窗口（含第一窗口）不会被再次计入。"""
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None, f"指标采集器 {collector_no} 不存在或已归档"
            pending_keys = sorted(
                (key for key, window in collector["windows"].items() if not window["completed"]),
                key=lambda key: collector["windows"][key]["seq"],
            )

        recovered: list[dict[str, Any]] = []
        for key in pending_keys:
            with self._window_lock(collector_no, key):
                with self._global_lock:
                    window = collector["windows"][key]
                    if window["completed"]:
                        # 并发兜底：别的请求已经完成了该窗口，这里跳过，不重复处理。
                        continue
                    window["completed"] = True
                    window["completed_at"] = _now_text()
                    window["updated_at"] = window["completed_at"]
                    value = aggregate(window["samples"], window["formula"])
                    window["snapshot"] = {
                        "window_key": key,
                        "seq": window["seq"],
                        "collector_no": collector_no,
                        "formula": window["formula"],
                        "formula_label": window["formula_label"],
                        "batch_no": window["batch_no"],
                        "samples": list(window["samples"]),
                        "value": value,
                        "captured_at": window["completed_at"],
                    }
                    collector["watermark"] = max(collector["watermark"], window["seq"])
                    recovered.append(deepcopy(window))
        with self._global_lock:
            self._persist()
            return (
                {"采集器编号": collector_no, "本次恢复窗口": recovered,
                 "水位窗口序号": collector["watermark"]},
                f"恢复完成，仅处理 {len(recovered)} 个未完成窗口，已完成窗口未重复计入",
            )

    # ---------- 公式切换 ----------

    def switch_formula(self, collector_no: str, new_formula: str) -> tuple[dict[str, Any] | None, str]:
        """切换聚合公式：旧批次归档进历史，新窗口使用新公式并记录切换批次。"""
        if new_formula not in FORMULAS:
            return None, f"不支持的聚合公式：{new_formula}，可选：{'、'.join(FORMULAS)}"
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None, f"指标采集器 {collector_no} 不存在或已归档"
            active = next(b for b in collector["batches"] if b["batch_no"] == collector["active_batch"])
            if active["formula"] == new_formula:
                return deepcopy(active), f"当前批次已经是「{active['formula_label']}」口径，无需切换"

            # 旧口径归档：旧窗口快照保留旧公式不动，仅批次状态转历史。
            active["status"] = "已归档"
            active["closed_at"] = _now_text()
            next_index = len(collector["batches"]) + 1
            new_batch_no = f"BATCH-{next_index:04d}"
            new_batch = {
                "batch_no": new_batch_no,
                "formula": new_formula,
                "formula_label": FORMULAS[new_formula],
                # 切换批次：记录从哪个窗口序号开始启用新公式（未完成窗口继续旧口径）。
                "switch_seq": collector["next_seq"],
                "status": "生效中",
                "opened_at": _now_text(),
                "closed_at": None,
            }
            collector["batches"].append(new_batch)
            collector["active_batch"] = new_batch_no
            self._persist()
            return deepcopy(new_batch), (
                f"聚合公式已切换为「{FORMULAS[new_formula]}」，旧口径归档至 {active['batch_no']}，"
                f"自窗口序号 {collector['next_seq']} 起的新窗口使用新公式"
            )

    # ---------- 诊断读取（与采集快照同一窗口序号） ----------

    def read_snapshot_for_diagnose(
        self, collector_no: str, window_seq: int
    ) -> tuple[dict[str, Any] | None, str]:
        """服务健康诊断读取采集数据快照：必须命中同一采集器、同一窗口序号。"""
        with self._global_lock:
            collector = self._collector_entry(collector_no)
            if collector is None:
                return None, f"指标采集器 {collector_no} 不存在或已归档"
            target = next(
                (window for window in collector["windows"].values() if window["seq"] == window_seq),
                None,
            )
            if target is None:
                return None, f"窗口序号 {window_seq} 尚未建立采集窗口"
            if not target["completed"]:
                return None, f"窗口序号 {window_seq} 尚未完成（断点样本 {target['sample_count']} 条），暂不能诊断"

            snapshot = target["snapshot"]
            samples = snapshot["samples"]
            # 健康分值：基于同一窗口快照计算，确定性口径，重复诊断结果一致。
            if not samples:
                score, level, reason = 60, "待观察", "窗口无有效样本，按基准分 60 评估"
            else:
                avg = sum(samples) / len(samples)
                peak = max(abs(max(samples) - avg), abs(min(samples) - avg))
                deviation = peak / abs(avg) if avg else peak
                if deviation <= 0.2:
                    score, level, reason = 95, "健康", f"窗口波动幅度 {deviation:.2%}，处于稳定区间"
                elif deviation <= 0.5:
                    score, level, reason = 80, "关注", f"窗口波动幅度 {deviation:.2%}，存在轻度抖动"
                else:
                    score, level, reason = 55, "异常", f"窗口波动幅度 {deviation:.2%}，存在明显尖峰"
            diagnosis = {
                "采集器编号": collector_no,
                "窗口序号": window_seq,
                "窗口键": target["window_key"],
                "批次编号": snapshot["batch_no"],
                "聚合公式": snapshot["formula"],
                "聚合口径": snapshot["formula_label"],
                "聚合值": snapshot["value"],
                "样本数": len(samples),
                "健康分值": score,
                "健康等级": level,
                "诊断结论": reason,
                "快照时间": snapshot["captured_at"],
                "诊断时间": _now_text(),
            }
            return diagnosis, "已读取同一窗口序号的采集数据快照完成健康诊断"


# 进程级单例：路由与服务共用，保证刷新页面读到同一份持久水位。
window_store = WindowStore()
