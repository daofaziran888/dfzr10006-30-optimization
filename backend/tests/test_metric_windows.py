"""指标采集窗口子系统测试：覆盖 METR-0001 / METR-0003 的完成标记、断点恢复、
公式批次、并发争抢、持久水位与诊断同窗口序号等全部验收场景。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# 在导入 app 之前把窗口状态文件指到临时目录，避免污染真实运行数据。
_TMP_DIR = tempfile.mkdtemp(prefix="metric-window-test-")
os.environ["METRIC_WINDOW_STATE_PATH"] = str(Path(_TMP_DIR) / "windows.json")

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.metric_windows import WindowStore, aggregate  # noqa: E402
from app.services.diagnose import DiagnoseService  # noqa: E402
from app.services.metric import MetricService  # noqa: E402


class MetricWindowTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一个独立状态文件，互不干扰。
        self.state_path = Path(_TMP_DIR) / f"{self._testMethodName}.json"
        self.store = WindowStore(self.state_path)
        self.metric_service = MetricService()
        # 服务层与诊断层共用同一个窗口仓库单例，测试中替换为本用例的临时仓库。
        import app.metric_windows as metric_windows

        self._singleton_holder = metric_windows
        self._original_store = metric_windows.window_store
        metric_windows.window_store = self.store

    def tearDown(self) -> None:
        self._singleton_holder.window_store = self._original_store
        if self.state_path.exists():
            self.state_path.unlink()


class IngestAndCompletionTests(MetricWindowTestBase):
    def test_first_window_counted_once_on_recover(self) -> None:
        """METR-0003 回归：第二批次失败后恢复，第一窗口不会被再次计入。"""
        window, msg = self.store.ingest("METR-0003", samples=[1, 2, 3], complete=True)
        self.assertTrue(window["completed"])
        self.assertEqual(window["snapshot"]["value"], 6)

        # 第二采集批次失败：只写断点，未完成。
        broken, _ = self.store.ingest("METR-0003", samples=[4])
        self.assertFalse(broken["completed"])
        self.assertEqual(broken["sample_count"], 1)

        # 恢复：只处理未完成窗口。
        result, _ = self.store.recover("METR-0003")
        self.assertEqual(len(result["本次恢复窗口"]), 1)
        self.assertEqual(result["本次恢复窗口"][0]["window_key"], "win-0002")

        # 第一窗口快照仍然是原值，没有被重复累加。
        windows = self.store.list_windows("METR-0003")
        first = windows["窗口列表"][0]
        self.assertEqual(first["snapshot"]["value"], 6)
        self.assertEqual(first["samples"], [1, 2, 3])
        self.assertEqual(windows["水位窗口序号"], 2)

    def test_duplicate_arrival_reads_marker_without_accumulating(self) -> None:
        """同一窗口重复到达：读取已有完成标记，不再累加。"""
        first, _ = self.store.ingest("METR-0001", window_key="win-0001", samples=[10, 20], complete=True)
        self.assertEqual(first["snapshot"]["value"], 30)

        second, message = self.store.ingest("METR-0001", window_key="win-0001", samples=[999], complete=True)
        self.assertIn("已完成", message)
        self.assertEqual(second["samples"], [10, 20])
        self.assertEqual(second["snapshot"]["value"], 30)
        self.assertEqual(second["sample_count"], 2)

    def test_watermark_only_advances_on_completion(self) -> None:
        """未完成窗口只保留断点，不推进持久水位。"""
        self.store.ingest("METR-0001", samples=[1])
        watermark = self.store.get_watermark("METR-0001")
        self.assertEqual(watermark["水位窗口序号"], 0)
        self.assertEqual(watermark["未完成窗口数"], 1)

        self.store.recover("METR-0001")
        watermark = self.store.get_watermark("METR-0001")
        self.assertEqual(watermark["水位窗口序号"], 1)
        self.assertEqual(watermark["未完成窗口数"], 0)

    def test_concurrent_contention_allows_single_completion_marker(self) -> None:
        """两个请求争抢同一监测窗口：只允许一个完成标记，断点不重复累加。"""
        barrier = threading.Barrier(2)
        outcomes: list[dict] = []

        def worker(samples: list[int]) -> None:
            barrier.wait()
            window, _ = self.store.ingest("METR-0003", window_key="hot-win", samples=samples, complete=True)
            outcomes.append(window)

        t1 = threading.Thread(target=worker, args=([7, 8],))
        t2 = threading.Thread(target=worker, args=([9],))
        t1.start(); t2.start(); t1.join(); t2.join()

        windows = self.store.list_windows("METR-0003")["窗口列表"]
        self.assertEqual(len(windows), 1, "争抢同一窗口键只能落地一个窗口")
        window = windows[0]
        self.assertTrue(window["completed"])
        # 两个完成响应看到的是同一份快照，完成标记唯一。
        self.assertEqual({out["window_key"] for out in outcomes}, {"hot-win"})
        self.assertTrue(all(out["completed"] for out in outcomes))
        self.assertEqual(len({tuple(out["snapshot"]["samples"]) for out in outcomes}), 1)
        self.assertEqual(window["sample_count"], len(window["samples"]))


class FormulaSwitchTests(MetricWindowTestBase):
    def test_switch_keeps_old_formula_snapshot_and_records_batch(self) -> None:
        """聚合公式切换：旧快照保留旧公式，新窗口走新公式并记录切换批次。"""
        old, _ = self.store.ingest("METR-0001", samples=[2, 4, 6], complete=True)
        self.assertEqual(old["formula"], "sum")
        self.assertEqual(old["snapshot"]["value"], 12)
        old_batch = old["batch_no"]

        batch, message = self.store.switch_formula("METR-0001", "avg")
        self.assertIn("平均值", message)
        self.assertEqual(batch["switch_seq"], 2)
        self.assertEqual(batch["formula"], "avg")

        new, _ = self.store.ingest("METR-0001", samples=[2, 4, 6], complete=True)
        self.assertEqual(new["formula"], "avg")
        self.assertAlmostEqual(new["snapshot"]["value"], 4.0)
        self.assertNotEqual(new["batch_no"], old_batch)

        # 旧窗口快照仍然按求和口径冻结。
        windows = self.store.list_windows("METR-0001")["窗口列表"]
        self.assertEqual(windows[0]["snapshot"]["formula"], "sum")
        self.assertEqual(windows[0]["snapshot"]["value"], 12)

        batches = self.store.list_batches("METR-0001")
        self.assertEqual(batches["当前批次"], new["batch_no"])
        statuses = {b["batch_no"]: b["status"] for b in batches["批次历史"]}
        self.assertEqual(statuses[old_batch], "已归档")
        self.assertEqual(statuses[new["batch_no"]], "生效中")

    def test_incomplete_window_keeps_old_formula_after_switch(self) -> None:
        """切换前未完成的窗口继续旧口径，只有新窗口使用新公式。"""
        pending, _ = self.store.ingest("METR-0001", samples=[3, 5])
        self.store.switch_formula("METR-0001", "max")
        self.store.recover("METR-0001")

        windows = self.store.list_windows("METR-0001")["窗口列表"]
        self.assertEqual(windows[0]["formula"], "sum")
        self.assertEqual(windows[0]["snapshot"]["value"], 8)

    def test_unknown_formula_rejected(self) -> None:
        batch, message = self.store.switch_formula("METR-0001", "median")
        self.assertIsNone(batch)
        self.assertIn("不支持", message)

    def test_in_window_formula_drift_rejected_before_write(self) -> None:
        """窗口内提交不匹配公式：先拒绝后写入，不落窗口、不污染断点。"""
        window, message = self.store.ingest(
            "METR-0001", window_key="win-0001", samples=[1, 2], formula="max"
        )
        self.assertIsNone(window)
        self.assertIn("不能在窗口内切换公式", message)
        # 拒绝后没有任何窗口落地，水位仍为 0。
        self.assertEqual(self.store.get_watermark("METR-0001")["水位窗口序号"], 0)
        self.assertEqual(self.store.list_windows("METR-0001")["窗口列表"], [])

        # 正常口径提交不受影响，窗口序号仍从 1 开始。
        ok, _ = self.store.ingest("METR-0001", window_key="win-0001", samples=[1, 2], complete=True)
        self.assertEqual(ok["seq"], 1)
        self.assertEqual(ok["snapshot"]["value"], 3)


class PersistenceAndIsolationTests(MetricWindowTestBase):
    def test_watermark_persists_across_reopen(self) -> None:
        """刷新/重开页面（等价于仓库重新加载）后仍是同一份持久水位。"""
        self.store.ingest("METR-0001", samples=[1, 1], complete=True)
        self.store.ingest("METR-0001", samples=[2], complete=False)

        reopened = WindowStore(self.state_path)
        watermark = reopened.get_watermark("METR-0001")
        # 水位只记录已完成窗口；失败窗口已占用序号 2，下一新窗口从 3 开始。
        self.assertEqual(watermark["水位窗口序号"], 1)
        self.assertEqual(watermark["下一窗口序号"], 3)
        self.assertEqual(watermark["未完成窗口"], ["win-0002"])

        # 重新打开后先从断点恢复，再继续采集，序号不复用、水位连续。
        reopened.recover("METR-0001")
        window, _ = reopened.ingest("METR-0001", samples=[3], complete=True)
        self.assertEqual(window["seq"], 3)
        self.assertEqual(reopened.get_watermark("METR-0001")["水位窗口序号"], 3)

    def test_collectors_isolated_by_collector_and_window_key(self) -> None:
        """METR-0001 与 METR-0003 各自维护完成标记与水位，互不影响。"""
        self.store.ingest("METR-0001", samples=[1], complete=True)
        self.store.ingest("METR-0003", samples=[2, 2], complete=False)

        self.assertEqual(self.store.get_watermark("METR-0001")["水位窗口序号"], 1)
        self.assertEqual(self.store.get_watermark("METR-0003")["水位窗口序号"], 0)

        result, _ = self.store.recover("METR-0003")
        self.assertEqual(len(result["本次恢复窗口"]), 1)
        self.assertEqual(self.store.get_watermark("METR-0001")["水位窗口序号"], 1)
        self.assertEqual(self.store.get_watermark("METR-0003")["水位窗口序号"], 1)

    def test_unknown_collector_rejected(self) -> None:
        self.assertIsNone(self.store.get_watermark("METR-9999"))
        window, message = self.store.ingest("METR-9999", samples=[1])
        self.assertIsNone(window)
        self.assertIn("不存在", message)


class DiagnoseTests(MetricWindowTestBase):
    def test_diagnose_reads_same_window_seq_snapshot(self) -> None:
        """服务健康诊断与采集数据快照读取同一窗口序号。"""
        self.store.ingest("METR-0003", samples=[10, 11, 9], complete=True)
        diagnose_service = DiagnoseService()

        diagnosis, message = diagnose_service.diagnose_window("METR-0003", 1)
        self.assertIsNotNone(diagnosis)
        self.assertIn("同一窗口序号", message)
        self.assertEqual(diagnosis["窗口序号"], 1)
        self.assertEqual(diagnosis["聚合值"], 30)
        self.assertEqual(diagnosis["窗口键"], "win-0001")

    def test_diagnose_rejects_incomplete_window(self) -> None:
        self.store.ingest("METR-0003", samples=[10])
        diagnosis, message = DiagnoseService().diagnose_window("METR-0003", 1)
        self.assertIsNone(diagnosis)
        self.assertIn("尚未完成", message)

    def test_diagnose_rejects_unknown_seq(self) -> None:
        diagnosis, message = DiagnoseService().diagnose_window("METR-0003", 7)
        self.assertIsNone(diagnosis)
        self.assertIn("尚未建立", message)


class AggregateHelperTests(unittest.TestCase):
    def test_formulas(self) -> None:
        samples = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(aggregate(samples, "sum"), 10.0)
        self.assertEqual(aggregate(samples, "avg"), 2.5)
        self.assertEqual(aggregate(samples, "max"), 4.0)
        self.assertEqual(aggregate(samples, "min"), 1.0)
        self.assertEqual(aggregate(samples, "count"), 4)
        self.assertIsNone(aggregate([], "sum"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
