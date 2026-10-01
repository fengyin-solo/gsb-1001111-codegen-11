"""灭灯区域聚合与抢修编排接口。

- POST /api/lighting-repair/faults        故障上报（并发用 Idempotency-Key 幂等去重）
- GET  /api/lighting-repair/packages      抢修包列表（含巡查路线、车辆到达顺序）
- POST /api/lighting-repair/packages/{id}/diagram   线路图更新（失败回滚整包阶段）
- POST /api/lighting-repair/packages/{id}/dispatch  派单校验，结论三处落账
- POST /api/lighting-repair/packages/{id}/restore   复电确认，历史逐次保留
- GET  /api/lighting-repair/vehicle-tasks 车辆任务汇总
- POST /api/lighting-repair/rebuild       增量重算（新故障进入后手动触发合并）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.repair import PACKAGE_STAGES, repair_service

router = APIRouter(prefix="/api/lighting-repair", tags=["灭灯抢修编排"])


class FaultReport(BaseModel):
    """故障上报报文：values 为灯具与故障信息，幂等键可走请求头 Idempotency-Key 或报文字段。"""

    values: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


@router.post("/faults", response_model=ActionResult)
def report_fault(
    payload: FaultReport,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """上报灭灯故障：相同幂等键的并发上报只受理一次，自动聚合进抢修包。"""
    key = payload.idempotency_key or idempotency_key
    lamp, message, hit = repair_service.report_fault(payload.values, key)
    if lamp is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"幂等命中": hit, **lamp})


@router.get("/packages")
def list_packages(stage: str | None = Query(default=None, description="按抢修包阶段过滤")) -> dict[str, Any]:
    """抢修包列表：每个包带统一写法的灭灯区域口径、巡查路线与车辆到达顺序。"""
    if stage and stage not in PACKAGE_STAGES:
        raise HTTPException(status_code=400, detail=f"阶段需为：{'、'.join(PACKAGE_STAGES)}")
    items = repair_service.list_packages(stage=stage)
    return {"total": len(items), "stages": PACKAGE_STAGES, "items": items}


@router.get("/packages/{package_id}")
def get_package(package_id: int) -> dict[str, Any]:
    package = repair_service.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"抢修包 {package_id} 不存在")
    return package


@router.post("/packages/{package_id}/diagram", response_model=ActionResult)
def update_diagram(package_id: int, payload: FaultReport) -> ActionResult:
    """更新供电线路图：成功推进派单；未成功回滚整包阶段，不产生任何落账。"""
    package, message = repair_service.update_diagram(package_id, payload.values)
    if package is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok="回滚" not in message and "失败" not in message, message=message, entry=package)


@router.post("/packages/{package_id}/dispatch", response_model=ActionResult)
def dispatch_package(package_id: int, payload: FaultReport) -> ActionResult:
    """派单抢修：校验线路图与车辆状态，结论落灯具台账、巡查清单、车辆任务汇总。"""
    package, message = repair_service.dispatch(package_id, payload.values)
    if package is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=package)


@router.post("/packages/{package_id}/restore", response_model=ActionResult)
def restore_package(package_id: int, payload: FaultReport) -> ActionResult:
    """复电确认：本次复电结果逐灯记入历史不亮记录，整包归档并释放车辆。"""
    package, message = repair_service.restore(package_id, payload.values)
    if package is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=package)


@router.get("/vehicle-tasks")
def vehicle_tasks(
    vehicle_no: str | None = Query(default=None, description="按车牌号过滤"),
) -> dict[str, Any]:
    """车辆抢修任务汇总：按抢修包维度统计，不按单灯重复派单。"""
    items = repair_service.vehicle_tasks(vehicle_no)
    return {"total": len(items), "items": items}


@router.post("/rebuild", response_model=ActionResult)
def rebuild_clusters() -> ActionResult:
    """增量重算灭灯区域：把新上报故障并入既有抢修包，已派单阶段的包保持冻结。"""
    message = repair_service.rebuild()
    return ActionResult(ok=True, message=message)
