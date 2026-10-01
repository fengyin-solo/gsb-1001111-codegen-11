"""灭灯区域聚合与抢修编排接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query

from app.schemas import ActionResult, EntryPayload
from app.services.lighting_repair import repair_service

router = APIRouter(prefix="/api/lighting/repair", tags=["灭灯抢修编排"])


@router.get("/packages", response_model=dict)
def list_packages(
    include_closed: bool = Query(default=True, description="是否包含已复电、部分复电、已取消包"),
) -> dict[str, Any]:
    """读取增量聚合后的抢修包及巡查路线、车辆到达顺序。"""
    items = repair_service.list_packages(include_closed=include_closed)
    return {"items": items, "total": len(items)}


@router.post("/recompute", response_model=ActionResult)
def recompute_packages() -> ActionResult:
    """按灯具台账和活动故障增量重算抢修包。"""
    result = repair_service.recompute()
    return ActionResult(
        ok=bool(result["ok"]),
        message=str(result["message"]),
        entry=result.get("entry"),
    )


@router.post("/reports", response_model=ActionResult)
def submit_report(
    payload: EntryPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """上报单灯灭灯；服务端按相邻杆、回路、供电区增量聚合并用幂等键去重。"""
    result = repair_service.submit_report(payload.values, idempotency_key=idempotency_key)
    return ActionResult(
        ok=bool(result["ok"]),
        message=str(result["message"]),
        entry=result.get("entry"),
    )


@router.post("/packages/{package_id}/dispatch", response_model=ActionResult)
def dispatch_package(
    package_id: int,
    payload: EntryPayload | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """整包派单并更新线路图；线路图失败时回滚灯具、巡查、车辆和抢修包阶段。"""
    values = payload.values if payload else {}
    result = repair_service.dispatch_package(package_id, values, idempotency_key=idempotency_key)
    return ActionResult(
        ok=bool(result["ok"]),
        message=str(result["message"]),
        entry=result.get("entry"),
    )


@router.post("/packages/{package_id}/restoration", response_model=ActionResult)
def record_restoration(
    package_id: int,
    payload: EntryPayload | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ActionResult:
    """登记每次复电结果，并按结果保留历史不亮记录。"""
    values = payload.values if payload else {}
    result = repair_service.record_restoration(package_id, values, idempotency_key=idempotency_key)
    return ActionResult(
        ok=bool(result["ok"]),
        message=str(result["message"]),
        entry=result.get("entry"),
    )


@router.get("/history")
def list_history() -> dict[str, Any]:
    """读取每次复电结果形成的历史不亮记录。"""
    items = repair_service.list_history()
    return {"items": items, "total": len(items)}
