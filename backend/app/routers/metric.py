"""服务指标采集接口：维护指标采集器，覆盖启动采集、暂停采集、关闭采集，
以及采集窗口的完成标记、断点恢复、公式切换与持久水位查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metric import MetricService

router = APIRouter(prefix="/api/metric", tags=["服务指标采集"])

service = MetricService()

LIST_FIELDS = ["采集器编号", "监控对象", "指标名称", "采集间隔", "聚合方式", "保留天数", "最近采集", "采集器状态"]
STATUSES = ["草稿", "采集中", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按采集器编号检索"),
    status: str | None = Query(default=None, description="草稿、采集中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按采集器编号与状态过滤服务指标采集列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/formulas", response_model=dict)
def list_formulas() -> dict[str, Any]:
    """返回当前支持的聚合公式代码与中文口径，供切换公式时选择。"""
    return {"module": "metric", "formulas": service.supported_formulas()}


@router.get("/collectors/{collector_no}/watermark", response_model=dict)
def get_watermark(collector_no: str) -> dict[str, Any]:
    """读取采集器的持久水位：刷新页面/重新打开后仍是同一份水位。"""
    watermark = service.watermark(collector_no)
    if watermark is None:
        raise HTTPException(status_code=404, detail=f"指标采集器 {collector_no} 不存在或已归档")
    return watermark


@router.get("/collectors/{collector_no}/windows", response_model=dict)
def list_windows(collector_no: str) -> dict[str, Any]:
    """关联清单：查看该采集器全部窗口的完成标记与断点，水位与列表同源。"""
    windows = service.windows(collector_no)
    if windows is None:
        raise HTTPException(status_code=404, detail=f"指标采集器 {collector_no} 不存在或已归档")
    return windows


@router.get("/collectors/{collector_no}/batches", response_model=dict)
def list_batches(collector_no: str) -> dict[str, Any]:
    """公式批次历史：旧口径批次归档在此，新窗口按生效批次取公式。"""
    batches = service.batches(collector_no)
    if batches is None:
        raise HTTPException(status_code=404, detail=f"指标采集器 {collector_no} 不存在或已归档")
    return batches


@router.post("/collectors/{collector_no}/windows/ingest", response_model=ActionResult)
def ingest_window(collector_no: str, payload: EntryPayload) -> ActionResult:
    """提交一个采集窗口的样本。

    - complete=false：失败/部分批次只写断点，窗口保持未完成，不推进水位；
    - complete=true：打完成标记并冻结快照，水位才推进；
    - 同一窗口键重复到达且已完成：直接返回已有标记，不再累加。
    """
    values = payload.values
    window, message = service.submit_window(
        collector_no,
        window_key=str(values.get("window_key") or "").strip() or None,
        samples=values.get("samples"),
        complete=bool(values.get("complete", False)),
        formula=str(values.get("formula") or "").strip() or None,
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.post("/collectors/{collector_no}/windows/recover", response_model=ActionResult)
def recover_windows(collector_no: str) -> ActionResult:
    """故障恢复：只处理未完成窗口；已完成窗口（含第一窗口）不会被再次计入。"""
    result, message = service.recover_windows(collector_no)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/collectors/{collector_no}/formula/switch", response_model=ActionResult)
def switch_formula(collector_no: str, payload: EntryPayload) -> ActionResult:
    """切换聚合公式：旧采集数据快照保留旧公式，新窗口使用新公式并记录切换批次。"""
    formula = str(payload.values.get("formula") or "").strip()
    if not formula:
        return ActionResult(ok=False, message="缺少必填字段：formula")
    batch, message = service.switch_formula(collector_no, formula)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条指标采集器明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标采集器 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条指标采集器，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="指标采集器已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条指标采集器执行启动采集、暂停采集、关闭采集；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出服务指标采集清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metric", "total": total, "items": items}
