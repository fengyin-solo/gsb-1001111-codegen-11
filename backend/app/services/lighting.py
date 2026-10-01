"""路灯照明业务规则：状态流转、字段校验与灭灯抢修聚合口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.lighting_repair import repair_service
from app.store import store

MODULE = "lighting"
REQUIRED_FIELDS = ["灯具编号", "灯具类型", "功率"]
OPTIONAL_FIELDS = [
    "所属路段",
    "安装日期",
    "杆号",
    "不亮原因",
    "设施状态",
    "供电区",
    "回路",
    "道路部位",
]
STATUS_ORDER = ["正常", "不亮", "闪烁", "抢修中", "已修复"]
ACTION_RULES = {"登记故障": "不亮", "派发修复": "抢修中", "确认修复": "已修复"}
NEGATIVE_ACTIONS = []


class LightingService:
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
            rows = [row for row in rows if keyword in str(row.get("灯具编号", ""))]
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
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry.setdefault("设施状态", STATUS_ORDER[0])
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"路灯设施 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于路灯照明可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "登记故障":
            result = repair_service.submit_report(
                {"lamp_id": entry_id, "reason": str(entry.get("不亮原因") or "台账登记灭灯")},
                idempotency_key=f"LEDGER-REPORT-{entry_id}",
            )
            package = result.get("entry")
            if not result.get("ok"):
                return None, str(result.get("message"))
            entry = store.find(MODULE, entry_id) or entry
            return entry, f"{result['message']}（{package and package.get('package_no')}）"

        if action == "派发修复":
            repair_service.recompute()
            active_package = None
            for report in store.rows("lighting_fault_reports"):
                if int(report.get("lamp_id", 0)) == entry_id and report.get("status") in {
                    "待聚合",
                    "已入包",
                    "已派单",
                    "抢修中",
                    "复电失败",
                }:
                    active_package = store.find("lighting_repair_packages", int(report.get("package_id") or 0))
                    break
            package_no = active_package and active_package.get("package_no")
            if package_no:
                return None, f"该灯具已并入抢修包 {package_no}，请在灭灯抢修编排中整包派单，不能按单灯重复派单"
            return None, "请先登记故障并完成灭灯区域聚合，再按抢修包整包派单"

        if action == "确认修复":
            repair_service.recompute()
            active_package = None
            for report in store.rows("lighting_fault_reports"):
                if int(report.get("lamp_id", 0)) == entry_id and report.get("status") in {
                    "待聚合",
                    "已入包",
                    "已派单",
                    "抢修中",
                    "复电失败",
                }:
                    active_package = store.find("lighting_repair_packages", int(report.get("package_id") or 0))
                    break
            if active_package:
                return None, f"请在抢修包 {active_package.get('package_no')} 登记复电结果，历史不亮记录会按本次结果保留"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["设施状态"] = target
            return entry, "路灯设施已确认修复"

        return None, "路灯照明动作未生效"
