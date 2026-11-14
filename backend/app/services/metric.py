"""服务指标采集业务规则：状态流转、字段校验、采集窗口完成标记与持久水位。"""
from __future__ import annotations

from typing import Any

from app import metric_windows
from app.metric_windows import FORMULAS
from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["采集器编号", "监控对象", "指标名称"]
STATUS_ORDER = ["草稿", "采集中", "已暂停", "已关闭"]
ACTION_RULES = {"启动采集": "草稿", "暂停采集": "采集中", "关闭采集": "已暂停"}
NEGATIVE_ACTIONS = []


class MetricService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("采集器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标采集器 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于服务指标采集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标采集器已{action}"

    # ---------- 采集窗口：完成标记 / 断点 / 水位 / 公式批次 ----------

    def submit_window(
        self,
        collector_no: str,
        *,
        window_key: str | None,
        samples: Any,
        complete: bool,
        formula: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """提交采集窗口样本；complete=True 时打完成标记并推进水位，否则只保留断点。"""
        return metric_windows.window_store.ingest(
            collector_no,
            window_key=window_key,
            samples=samples,
            complete=complete,
            formula=formula,
        )

    def recover_windows(self, collector_no: str) -> tuple[dict[str, Any] | None, str]:
        """故障恢复：仅处理未完成窗口，已完成窗口不再重复计入。"""
        return metric_windows.window_store.recover(collector_no)

    def switch_formula(self, collector_no: str, formula: str) -> tuple[dict[str, Any] | None, str]:
        """切换聚合公式：旧批次归档保留旧口径，新窗口启用新公式并记录切换批次。"""
        return metric_windows.window_store.switch_formula(collector_no, formula)

    def watermark(self, collector_no: str) -> dict[str, Any] | None:
        return metric_windows.window_store.get_watermark(collector_no)

    def windows(self, collector_no: str) -> dict[str, Any] | None:
        return metric_windows.window_store.list_windows(collector_no)

    def batches(self, collector_no: str) -> dict[str, Any] | None:
        return metric_windows.window_store.list_batches(collector_no)

    def supported_formulas(self) -> dict[str, str]:
        return dict(FORMULAS)
