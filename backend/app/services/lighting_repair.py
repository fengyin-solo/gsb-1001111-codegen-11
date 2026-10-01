"""灭灯区域聚合与抢修编排。

规则集中在这一层：
- 相邻杆号、同回路、同供电区的灭灯故障只形成一个抢修包；
- 聚合结果随上报增量重算，已派单包保留车辆、巡查与复电历史；
- 并发上报、派单和复电通过幂等键去重；
- 线路图更新失败时，用仓库快照回滚整包阶段；
- 校验结论同步到灯具台账、巡查清单和车辆任务汇总，禁止单灯重复派单。
"""
from __future__ import annotations

import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

LIGHTING = "lighting"
PATROL = "patrol"
VEHICLE = "vehicle"
PACKAGE_TABLE = "lighting_repair_packages"
REPORT_TABLE = "lighting_fault_reports"
TASK_TABLE = "lighting_vehicle_tasks"
HISTORY_TABLE = "lighting_outage_history"
LINE_TABLE = "lighting_line_diagram_updates"
META_TABLE = "lighting_repair_meta"

ACTIVE_REPORT_STATUSES = {"待聚合", "已入包", "已派单", "抢修中", "复电失败"}
ACTIVE_PACKAGE_STAGES = {"待编排", "已派单", "抢修中", "待补派", "复电失败"}
TERMINAL_PACKAGE_STAGES = {"已复电", "部分复电", "已取消"}

SAFETY_RANK = {"匝道": 3, "主路": 2, "辅路": 1}
SAFETY_LABEL = {3: "一级通行安全风险", 2: "二级通行安全风险", 1: "三级通行安全风险", 0: "常规抢修风险"}
DISPATCHED_STATUSES = {"已派单", "抢修中", "复电失败"}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def text(value: Any) -> str:
    return str(value or "").strip()


def stake_position(value: Any) -> float | None:
    """识别 K2+130、k2+130.5 这类桩号，统一换算成米。"""
    match = re.search(r"[Kk]\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)", text(value))
    if not match:
        return None
    return int(match.group(1)) * 1000 + float(match.group(2))


def pole_number(value: Any) -> int | None:
    match = re.search(r"(\d+)\s*号?$", text(value))
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\d+", text(value))
    return int(numbers[-1]) if numbers else None


def position_key(value: Any) -> tuple[float, int]:
    stake = stake_position(value)
    if stake is not None:
        return stake, 0
    number = pole_number(value)
    return float(number or 0), 1 if number is None else 0


def normalize_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = re.split(r"[,，、\s]+", text(value))
    ids: list[int] = []
    for item in raw_values:
        item = text(item)
        if item:
            ids.append(int(item))
    return ids


class LightingRepairService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._idempotent: dict[str, dict[str, Any]] = {}
        self._booted = False

    def _rows(self, table: str) -> list[dict[str, Any]]:
        return store.rows(table)

    def _find(self, table: str, row_id: int) -> dict[str, Any] | None:
        return store.find(table, row_id)

    def _meta(self, key: str) -> str | None:
        for row in self._rows(META_TABLE):
            if row.get("key") == key:
                return text(row.get("value")) or None
        return None

    def _set_meta(self, key: str, value: str) -> None:
        for row in self._rows(META_TABLE):
            if row.get("key") == key:
                row["value"] = value
                return
        self._rows(META_TABLE).append({"id": store.next_id(META_TABLE), "key": key, "value": value})

    def _bootstrap(self) -> None:
        if self._booted:
            return
        if self._meta("bootstrapped") == "true":
            self._booted = True
            return
        for lamp in self._rows(LIGHTING):
            if text(lamp.get("status")) == "不亮" and not self._active_report(int(lamp.get("id", 0))):
                self._create_report_from_lamp(lamp, f"BOOT-{lamp.get('id')}-{uuid.uuid4().hex[:8]}", "启动聚合")
        self._recompute_locked()
        self._set_meta("bootstrapped", "true")
        self._booted = True

    def _active_report(self, lamp_id: int) -> dict[str, Any] | None:
        for report in self._rows(REPORT_TABLE):
            if int(report.get("lamp_id", 0)) == lamp_id and report.get("status") in ACTIVE_REPORT_STATUSES:
                return report
        return None

    def _lamp_road_part(self, lamp: dict[str, Any], report: dict[str, Any] | None = None) -> str:
        part = text((report or {}).get("road_part")) or text(lamp.get("道路部位"))
        road = text(lamp.get("所属路段"))
        if "匝道" in part or "匝道" in road:
            return "匝道"
        if "主路" in part or "主路" in road:
            return "主路"
        if "辅路" in part or "辅路" in road:
            return "辅路"
        return text(part) or "未标明部位"

    def _report_level(self, report: dict[str, Any]) -> int:
        explicit = text(report.get("safety_level"))
        if explicit.isdigit():
            return max(0, min(3, int(explicit)))
        lamp = self._find(LIGHTING, int(report.get("lamp_id", 0))) or {}
        return SAFETY_RANK.get(self._lamp_road_part(lamp, report), 0)

    def _create_report_from_lamp(self, lamp: dict[str, Any], key: str, source: str) -> dict[str, Any]:
        timestamp = now_text()
        report = {
            "id": store.next_id(REPORT_TABLE),
            "report_no": f"LOUT-{len(self._rows(REPORT_TABLE)) + 1:04d}",
            "idempotency_key": key,
            "lamp_id": int(lamp["id"]),
            "lamp_no": text(lamp.get("灯具编号")),
            "pole_no": text(lamp.get("杆号")),
            "road": text(lamp.get("所属路段")),
            "road_part": self._lamp_road_part(lamp),
            "supply_zone": text(lamp.get("供电区")) or "未标明供电区",
            "circuit": text(lamp.get("回路")) or "未标明回路",
            "reason": text(lamp.get("不亮原因")) or "巡查上报灭灯",
            "status": "待聚合",
            "source": source,
            "reported_at": timestamp,
            "package_id": None,
            "package_no": None,
        }
        self._rows(REPORT_TABLE).append(report)
        return report

    def submit_report(self, values: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        key = text(idempotency_key) or text(values.get("idempotency_key")) or f"REP-{uuid.uuid4().hex}"
        with self._lock:
            cached = self._idempotent.get(key)
            if cached:
                return deepcopy(cached)
            for report in self._rows(REPORT_TABLE):
                if text(report.get("idempotency_key")) == key:
                    package = self._find(PACKAGE_TABLE, int(report.get("package_id") or 0))
                    result = {
                        "ok": True,
                        "message": "重复上报已按幂等键去重",
                        "entry": package,
                        "report": report,
                        "deduped": True,
                    }
                    self._idempotent[key] = result
                    return deepcopy(result)

            self._bootstrap()
            lamp_id = int(values.get("lamp_id") or 0)
            lamp = self._find(LIGHTING, lamp_id)
            if lamp is None:
                return {"ok": False, "message": f"灯具 {lamp_id} 不存在，不能上报灭灯", "entry": None}
            active = self._active_report(lamp_id)
            if active:
                package = self._find(PACKAGE_TABLE, int(active.get("package_id") or 0))
                return {
                    "ok": False,
                    "message": f"灯具已在抢修包 {package and package.get('package_no')} 中，不能重复派单",
                    "entry": package,
                    "report": active,
                }

            report = self._create_report_from_lamp(lamp, key, text(values.get("source")) or "并发上报")
            if values.get("road_part"):
                report["road_part"] = text(values.get("road_part"))
            if values.get("supply_zone"):
                report["supply_zone"] = text(values.get("supply_zone"))
            if values.get("circuit"):
                report["circuit"] = text(values.get("circuit"))
            if values.get("reason"):
                report["reason"] = text(values.get("reason"))
            if values.get("safety_level") is not None:
                report["safety_level"] = values.get("safety_level")

            lamp["status"] = "不亮"
            lamp["pending"] = True
            lamp["abnormal"] = True
            lamp["设施状态"] = "不亮"
            lamp["不亮原因"] = report["reason"]
            lamp["供电区"] = report["supply_zone"]
            lamp["回路"] = report["circuit"]
            lamp["道路部位"] = report["road_part"]
            lamp["最近上报时间"] = report["reported_at"]

            package = self._recompute_locked(trigger_report_id=report["id"])
            if isinstance(package, list):
                package = package[0] if package else None
            result = {
                "ok": True,
                "message": f"灭灯上报已增量聚合到抢修包 {package and package.get('package_no')}",
                "entry": package,
                "report": report,
                "deduped": False,
            }
            self._idempotent[key] = result
            return deepcopy(result)

    def list_packages(self, *, include_closed: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._bootstrap()
            rows = self._rows(PACKAGE_TABLE)
            if not include_closed:
                rows = [row for row in rows if row.get("stage") in ACTIVE_PACKAGE_STAGES]
            return deepcopy(sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True))

    def get_package(self, package_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._bootstrap()
            row = self._find(PACKAGE_TABLE, package_id)
            return deepcopy(row) if row else None

    def list_history(self) -> list[dict[str, Any]]:
        with self._lock:
            self._bootstrap()
            return deepcopy(sorted(self._rows(HISTORY_TABLE), key=lambda row: int(row.get("id", 0)), reverse=True))

    def recompute(self) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            packages = self._recompute_locked()
            return {
                "ok": True,
                "message": "灭灯聚合任务已按当前台账增量重算",
                "entry": packages[0] if packages else None,
                "packages": deepcopy(packages),
            }

    def _adjacent(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left["supply_zone"] == right["supply_zone"]:
            return True
        if left["circuit"] == right["circuit"]:
            return True
        if left.get("road") != right.get("road"):
            return False
        left_stake, left_fallback = position_key(left.get("pole_no"))
        right_stake, right_fallback = position_key(right.get("pole_no"))
        if left_fallback == right_fallback == 1:
            left_pole = pole_number(left.get("pole_no"))
            right_pole = pole_number(right.get("pole_no"))
            return left_pole is not None and right_pole is not None and abs(left_pole - right_pole) == 1
        return abs(left_stake - right_stake) <= 35

    def _clusters(self, reports: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        remaining = set(range(len(reports)))
        clusters: list[list[dict[str, Any]]] = []
        while remaining:
            index = remaining.pop()
            stack = [index]
            cluster: list[dict[str, Any]] = []
            while stack:
                current = stack.pop()
                current_report = reports[current]
                cluster.append(current_report)
                for candidate in list(remaining):
                    if self._adjacent(current_report, reports[candidate]):
                        remaining.remove(candidate)
                        stack.append(candidate)
            clusters.append(cluster)
        return clusters

    def _unified_fault_text(
        self,
        zone: str,
        circuits: list[str],
        roads: list[str],
        road_parts: list[str],
        poles: list[str],
        lamp_count: int,
        level: int,
    ) -> str:
        return (
            f"{zone}供电区｜回路{'、'.join(circuits)}｜{'、'.join(roads)}｜"
            f"{'、'.join(road_parts)}｜杆号{'、'.join(poles)}｜{lamp_count}盏灭灯｜{SAFETY_LABEL[level]}"
        )

    def _route_for(self, reports: list[dict[str, Any]]) -> list[str]:
        poles = sorted({text(report.get("pole_no")) for report in reports if text(report.get("pole_no"))}, key=position_key)
        return ["车辆驻地", *poles, "复电核验点"]

    def _cancel_package(self, package: dict[str, Any], reason: str) -> None:
        package["stage"] = "已取消"
        package["line_diagram_status"] = "无需更新"
        package["validation_conclusion"] = f"已取消：{reason}"
        package["updated_at"] = now_text()
        for report_id in package.get("report_ids", []):
            report = self._find(REPORT_TABLE, int(report_id))
            if report and report.get("status") in ACTIVE_REPORT_STATUSES:
                report["package_id"] = None
                report["package_no"] = None
                report["status"] = "待聚合"

    def _reconcile_lamps(self) -> None:
        """把外部直接登记到台账的灭灯增量纳入，已在抢修中的灯具不重复生成。"""
        for lamp in self._rows(LIGHTING):
            if text(lamp.get("status")) == "不亮" and not self._active_report(int(lamp.get("id", 0))):
                key = f"AUTO-{lamp.get('id')}-{len(self._rows(HISTORY_TABLE)) + 1}-{uuid.uuid4().hex[:6]}"
                self._create_report_from_lamp(lamp, key, "台账增量重算")

    def _recompute_locked(self, trigger_report_id: int | None = None) -> list[dict[str, Any]]:
        self._reconcile_lamps()
        reports = [
            row for row in self._rows(REPORT_TABLE)
            if row.get("status") in ACTIVE_REPORT_STATUSES
        ]
        clusters = self._clusters(reports)
        active_packages = [row for row in self._rows(PACKAGE_TABLE) if row.get("stage") in ACTIVE_PACKAGE_STAGES]
        used_package_ids: set[int] = set()
        resulting: list[dict[str, Any]] = []
        timestamp = now_text()

        for cluster in clusters:
            cluster_ids = {int(report["id"]) for report in cluster}
            package = max(
                [candidate for candidate in active_packages if int(candidate["id"]) not in used_package_ids],
                key=lambda candidate: len(cluster_ids & {int(item) for item in candidate.get("report_ids", [])}),
                default=None,
            )
            overlap = 0
            if package is not None:
                overlap = len(cluster_ids & {int(item) for item in package.get("report_ids", [])})
            if overlap == 0:
                package = None
            else:
                used_package_ids.add(int(package["id"]))

            zone = sorted({text(report["supply_zone"]) for report in cluster})[0]
            circuits = sorted({text(report["circuit"]) for report in cluster})
            roads = sorted({text(report["road"]) for report in cluster if text(report["road"])})
            road_parts = sorted({self._lamp_road_part(self._find(LIGHTING, int(report["lamp_id"])) or {}, report) for report in cluster})
            poles = sorted({text(report["pole_no"]) for report in cluster}, key=position_key)
            level = max((self._report_level(report) for report in cluster), default=0)
            route = self._route_for(cluster)
            lamp_ids = sorted({int(report["lamp_id"]) for report in cluster})
            unified_text = self._unified_fault_text(zone, circuits, roads, road_parts, poles, len(lamp_ids), level)

            if package is None:
                package = {
                    "id": store.next_id(PACKAGE_TABLE),
                    "package_no": f"RP-{len(self._rows(PACKAGE_TABLE)) + 1:04d}",
                    "stage": "待编排",
                    "supply_zone": zone,
                    "circuits": circuits,
                    "roads": roads,
                    "road_parts": road_parts,
                    "pole_range": f"{poles[0]}~{poles[-1]}" if poles else "",
                    "poles": poles,
                    "lamp_ids": lamp_ids,
                    "report_ids": sorted(cluster_ids),
                    "unified_fault": unified_text,
                    "safety_level": SAFETY_LABEL[level],
                    "safety_rank": level,
                    "includes_main": level >= 2 or any("主路" in self._lamp_road_part(self._find(LIGHTING, int(r["lamp_id"])) or {}, r) for r in cluster),
                    "includes_ramp": any("匝道" in self._lamp_road_part(self._find(LIGHTING, int(r["lamp_id"])) or {}, r) for r in cluster),
                    "patrol_route": route,
                    "vehicle_ids": [],
                    "arrival_order": [],
                    "patrol_task_ids": [],
                    "line_diagram_status": "待更新",
                    "validation_conclusion": "待校验：已完成聚合，派单前将校验车辆、台账与线路图",
                    "restore_history": [],
                    "aggregate_version": 1,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "last_trigger_report_id": trigger_report_id,
                }
                self._rows(PACKAGE_TABLE).append(package)
            else:
                old_lamp_ids = set(int(item) for item in package.get("lamp_ids", []))
                package.update({
                    "supply_zone": zone,
                    "circuits": circuits,
                    "roads": roads,
                    "road_parts": road_parts,
                    "pole_range": f"{poles[0]}~{poles[-1]}" if poles else "",
                    "poles": poles,
                    "lamp_ids": lamp_ids,
                    "report_ids": sorted(cluster_ids),
                    "unified_fault": unified_text,
                    "safety_level": SAFETY_LABEL[level],
                    "safety_rank": level,
                    "includes_main": level >= 2 or any("主路" in self._lamp_road_part(self._find(LIGHTING, int(r["lamp_id"])) or {}, r) for r in cluster),
                    "includes_ramp": any("匝道" in self._lamp_road_part(self._find(LIGHTING, int(r["lamp_id"])) or {}, r) for r in cluster),
                    "patrol_route": route,
                    "last_trigger_report_id": trigger_report_id,
                })
                package["aggregate_version"] = int(package.get("aggregate_version", 1)) + 1
                package["updated_at"] = timestamp
                if package.get("stage") in DISPATCHED_STATUSES and set(lamp_ids) != old_lamp_ids:
                    package["stage"] = "待补派"
                    package["validation_conclusion"] = "增量重算发现新灭灯，需对新增灯具补派；既有车辆任务不重复生成"

            for report in cluster:
                report["package_id"] = package["id"]
                report["package_no"] = package["package_no"]
                if report.get("status") == "待聚合":
                    report["status"] = "已入包"
            for lamp_id in lamp_ids:
                lamp = self._find(LIGHTING, lamp_id)
                if lamp and text(lamp.get("status")) == "不亮":
                    lamp["抢修包编号"] = package["package_no"]
                    lamp["抢修校验结论"] = "聚合校验通过：已并入整包，等待统一派单"
                    lamp["统一故障写法"] = unified_text
            resulting.append(package)

        for package in active_packages:
            if int(package["id"]) not in used_package_ids:
                self._cancel_package(package, "当前已无活动灭灯报告")

        if trigger_report_id is not None:
            report = self._find(REPORT_TABLE, trigger_report_id)
            if report and report.get("package_id"):
                target = self._find(PACKAGE_TABLE, int(report["package_id"]))
                if target:
                    return [target]
        return sorted(resulting, key=lambda row: int(row.get("id", 0)))

    def _dispatch_conclusion(self, package: dict[str, Any], vehicle_count: int, lamp_count: int) -> str:
        return (
            f"校验通过：按{package['safety_level']}管控，主路与匝道同时故障已取最高级别；"
            f"{lamp_count}盏灯合并为{package['package_no']}整包派单，{vehicle_count}辆车按到达顺序执行，"
            "未按单灯重复派单"
        )

    def dispatch_package(
        self,
        package_id: int,
        values: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = text(idempotency_key) or text(values.get("idempotency_key")) or f"DISPATCH-{package_id}-{uuid.uuid4().hex}"
        with self._lock:
            self._bootstrap()
            cached = self._idempotent.get(key)
            if cached:
                return deepcopy(cached)
            package = self._find(PACKAGE_TABLE, package_id)
            if package is None:
                return {"ok": False, "message": "抢修包不存在", "entry": None}
            if package.get("stage") not in ACTIVE_PACKAGE_STAGES:
                return {"ok": False, "message": f"抢修包处于{package.get('stage')}，不能派单", "entry": package}

            requested_vehicle_ids = normalize_ids(values.get("vehicle_ids"))
            existing_vehicle_ids = [int(item) for item in package.get("vehicle_ids", [])]
            new_vehicle_ids = [vid for vid in requested_vehicle_ids if vid not in existing_vehicle_ids]
            if not existing_vehicle_ids and not requested_vehicle_ids:
                return {"ok": False, "message": "至少选择一辆抢修车辆", "entry": package}

            vehicles: dict[int, dict[str, Any]] = {}
            for vid in requested_vehicle_ids:
                vehicle = self._find(VEHICLE, vid)
                if vehicle is None:
                    return {"ok": False, "message": f"车辆 {vid} 不存在", "entry": package}
                if text(vehicle.get("status")) in {"报废", "维修"}:
                    return {"ok": False, "message": f"车辆 {vehicle.get('车辆编号')} 当前为{vehicle.get('status')}，不能出车", "entry": package}
                active_package_id = vehicle.get("active_package_id")
                if (
                    active_package_id
                    and int(active_package_id) != package_id
                    and text(vehicle.get("status")) != "在库"
                ):
                    vehicle.pop("active_package_id", None)
                    vehicle.pop("active_package_no", None)
                    return {"ok": False, "message": f"车辆 {vehicle.get('车辆编号')} 已有抢修任务，不能重复派单", "entry": package}
                if text(vehicle.get("status")) == "在库":
                    vehicle.pop("active_package_id", None)
                    vehicle.pop("active_package_no", None)
                vehicles[vid] = vehicle

            snapshot = store.snapshot()
            try:
                timestamp = now_text()
                route = package.get("patrol_route") or []
                for vid, vehicle in vehicles.items():
                    task = {
                        "id": store.next_id(TASK_TABLE),
                        "task_no": f"LTV-{len(self._rows(TASK_TABLE)) + 1:04d}",
                        "package_id": package_id,
                        "package_no": package["package_no"],
                        "vehicle_id": vid,
                        "vehicle_no": vehicle.get("车辆编号"),
                        "plate_no": vehicle.get("车牌号"),
                        "driver": vehicle.get("驾驶员"),
                        "arrival_order": 0,
                        "estimated_arrival": "",
                        "route": " → ".join(route),
                        "task_summary": package["unified_fault"],
                        "status": "赶赴现场",
                        "validation_conclusion": "",
                        "created_at": timestamp,
                    }
                    self._rows(TASK_TABLE).append(task)

                tasks = [
                    row for row in self._rows(TASK_TABLE)
                    if int(row.get("package_id", 0)) == package_id and row.get("status") != "已取消"
                ]
                tasks.sort(key=lambda row: int(row.get("id", 0)))
                arrival_order: list[dict[str, Any]] = []
                for order, task in enumerate(tasks, start=1):
                    task["arrival_order"] = order
                    task["estimated_arrival"] = f"预计第 {order} 个到达（约 {order * 8} 分钟）"
                    arrival_order.append({
                        "order": order,
                        "vehicle_id": task["vehicle_id"],
                        "vehicle_no": task["vehicle_no"],
                        "plate_no": task["plate_no"],
                        "estimated_arrival": task["estimated_arrival"],
                    })
                    vehicle = self._find(VEHICLE, int(task["vehicle_id"]))
                    if vehicle:
                        vehicle["status"] = "出车作业"
                        vehicle["车辆状态"] = "出车作业"
                        vehicle["active_package_id"] = package_id
                        vehicle["active_package_no"] = package["package_no"]
                        vehicle["当前抢修包"] = package["package_no"]
                        vehicle["到达顺序"] = f"第 {order} 到达"
                        vehicle["巡查路线"] = " → ".join(route)

                conclusion = self._dispatch_conclusion(package, len(tasks), len(package["lamp_ids"]))
                for task in tasks:
                    task["validation_conclusion"] = conclusion
                    vehicle = self._find(VEHICLE, int(task["vehicle_id"]))
                    if vehicle:
                        vehicle["抢修任务汇总"] = f"{package['package_no']}｜第 {task['arrival_order']} 到达｜{task['route']}"
                        vehicle["抢修校验结论"] = conclusion

                package["vehicle_ids"] = [item["vehicle_id"] for item in arrival_order]
                package["arrival_order"] = arrival_order
                package["stage"] = "抢修中"
                package["line_diagram_status"] = "更新中"
                package["validation_conclusion"] = conclusion
                package["dispatched_at"] = timestamp
                package["updated_at"] = timestamp

                for report_id in package["report_ids"]:
                    report = self._find(REPORT_TABLE, int(report_id))
                    if report:
                        report["status"] = "已派单"
                for lamp_id in package["lamp_ids"]:
                    lamp = self._find(LIGHTING, int(lamp_id))
                    if lamp:
                        lamp["status"] = "抢修中"
                        lamp["pending"] = True
                        lamp["abnormal"] = True
                        lamp["设施状态"] = "抢修中"
                        lamp["抢修包编号"] = package["package_no"]
                        lamp["抢修校验结论"] = conclusion
                        lamp["派单方式"] = "整包派单"
                        lamp["到达顺序"] = "、".join(f"{item['vehicle_no']}第{item['order']}" for item in arrival_order)

                patrol_row = None
                for row in self._rows(PATROL):
                    if int(row.get("package_id") or 0) == package_id:
                        patrol_row = row
                        break
                patrol_values = {
                    "巡查路段": "、".join(package["roads"]),
                    "巡查日期": timestamp[:10],
                    "巡查人员": "照明抢修巡查班",
                    "巡查车辆": "、".join(str(item["vehicle_no"]) for item in arrival_order),
                    "发现问题": package["unified_fault"],
                    "处置措施": "按巡查路线逐杆复核并配合整包抢修",
                    "巡查状态": "巡查中",
                    "status": "巡查中",
                    "pending": True,
                    "abnormal": True,
                    "package_id": package_id,
                    "package_no": package["package_no"],
                    "lamp_ids": package["lamp_ids"],
                    "巡查路线": " → ".join(route),
                    "车辆到达顺序": "；".join(f"{item['vehicle_no']}第{item['order']}" for item in arrival_order),
                    "抢修校验结论": conclusion,
                    "updated_at": timestamp,
                }
                if patrol_row is None:
                    patrol_row = {"id": store.next_id(PATROL), "巡查编号": f"PATR-{package['package_no']}"}
                    patrol_row.update(patrol_values)
                    self._rows(PATROL).append(patrol_row)
                else:
                    patrol_row.update(patrol_values)
                package["patrol_task_ids"] = sorted({int(item) for item in package.get("patrol_task_ids", [])} | {int(patrol_row["id"])})

                if not bool(values.get("line_diagram_ok", True)):
                    raise RuntimeError("线路图更新未成功")
                self._rows(LINE_TABLE).append({
                    "id": store.next_id(LINE_TABLE),
                    "package_id": package_id,
                    "package_no": package["package_no"],
                    "status": "更新成功",
                    "route": " → ".join(route),
                    "updated_at": timestamp,
                })
                package["line_diagram_status"] = "更新成功"
                result = {"ok": True, "message": f"{package['package_no']} 已整包派单，线路图更新成功", "entry": package}
                self._idempotent[key] = deepcopy(result)
                return deepcopy(result)
            except Exception as exc:
                store.restore(snapshot)
                restored_package = self._find(PACKAGE_TABLE, package_id)
                return deepcopy({
                    "ok": False,
                    "message": f"线路图更新未成功，已回滚整包阶段：{exc}",
                    "entry": restored_package,
                    "rolled_back": True,
                })

    def record_restoration(
        self,
        package_id: int,
        values: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = text(idempotency_key) or text(values.get("idempotency_key")) or f"RESTORE-{package_id}-{uuid.uuid4().hex}"
        with self._lock:
            self._bootstrap()
            cached = self._idempotent.get(key)
            if cached:
                return deepcopy(cached)
            package = self._find(PACKAGE_TABLE, package_id)
            if package is None:
                return {"ok": False, "message": "抢修包不存在", "entry": None}
            if package.get("stage") in TERMINAL_PACKAGE_STAGES - {"部分复电"}:
                return {"ok": False, "message": f"抢修包已{package.get('stage')}，无需重复登记", "entry": package}

            active_reports = [
                self._find(REPORT_TABLE, int(report_id))
                for report_id in package.get("report_ids", [])
            ]
            active_reports = [report for report in active_reports if report and report.get("status") in ACTIVE_REPORT_STATUSES]
            if not active_reports:
                return {"ok": False, "message": "抢修包没有可登记复电结果的活动故障", "entry": package}

            requested = set(normalize_ids(values.get("restored_lamp_ids")))
            active_lamp_ids = {int(report["lamp_id"]) for report in active_reports}
            if requested:
                invalid = requested - active_lamp_ids
                if invalid:
                    return {"ok": False, "message": f"灯具 {sorted(invalid)} 不属于当前抢修包", "entry": package}
            else:
                requested = set(active_lamp_ids)
            success = bool(values.get("success", True))
            if not success:
                requested = set()

            snapshot = store.snapshot()
            try:
                timestamp = now_text()
                restored_reports = [report for report in active_reports if int(report["lamp_id"]) in requested]
                failed_reports = [report for report in active_reports if report not in restored_reports]
                for report in active_reports:
                    succeeded = report in restored_reports
                    result = "复电成功" if succeeded else "复电失败"
                    report["status"] = "已复电" if succeeded else "复电失败"
                    report["restore_result"] = result
                    report["restored_at"] = timestamp if succeeded else ""
                    lamp = self._find(LIGHTING, int(report["lamp_id"]))
                    history = {
                        "id": store.next_id(HISTORY_TABLE),
                        "lamp_id": report["lamp_id"],
                        "lamp_no": report["lamp_no"],
                        "package_id": package_id,
                        "package_no": package["package_no"],
                        "reported_at": report.get("reported_at"),
                        "restored_at": timestamp,
                        "result": result,
                        "remark": text(values.get("remark")) or "整包复电核验",
                    }
                    self._rows(HISTORY_TABLE).append(history)
                    if lamp:
                        lamp["历史不亮记录"] = lamp.get("历史不亮记录", [])
                        lamp["历史不亮记录"].append(f"{report.get('reported_at')} 上报 / {timestamp} {result}")
                        if succeeded:
                            lamp["status"] = "已修复"
                            lamp["pending"] = False
                            lamp["abnormal"] = False
                            lamp["设施状态"] = "已修复"
                            lamp["复电时间"] = timestamp
                            lamp["抢修校验结论"] = f"复电校验通过：{package['package_no']} 本次复电成功"
                        else:
                            lamp["status"] = "不亮"
                            lamp["pending"] = True
                            lamp["abnormal"] = True
                            lamp["设施状态"] = "不亮"
                            lamp["抢修校验结论"] = f"复电校验未通过：{package['package_no']} 本次复电失败，历史结果已保留"

                all_restored = not failed_reports
                if all_restored:
                    stage = "已复电"
                elif restored_reports:
                    stage = "部分复电"
                else:
                    stage = "复电失败"
                conclusion = (
                    f"复电校验完成：成功 {len(restored_reports)} 盏、失败 {len(failed_reports)} 盏；"
                    f"历史不亮记录按本次结果保留，抢修包状态为{stage}"
                )
                package["stage"] = stage
                package["validation_conclusion"] = conclusion
                package["restored_at"] = timestamp
                package["updated_at"] = timestamp
                package.setdefault("restore_history", []).append({
                    "restored_at": timestamp,
                    "success_count": len(restored_reports),
                    "failed_count": len(failed_reports),
                    "stage": stage,
                    "remark": text(values.get("remark")) or "整包复电核验",
                })

                for patrol_id in package.get("patrol_task_ids", []):
                    patrol_row = self._find(PATROL, int(patrol_id))
                    if patrol_row:
                        patrol_row["巡查状态"] = "已完成" if all_restored else "巡查中"
                        patrol_row["status"] = patrol_row["巡查状态"]
                        patrol_row["pending"] = not all_restored
                        patrol_row["abnormal"] = not all_restored
                        patrol_row["抢修校验结论"] = conclusion
                tasks = [row for row in self._rows(TASK_TABLE) if int(row.get("package_id", 0)) == package_id]
                for task in tasks:
                    task["status"] = "已归库" if all_restored else ("现场待命" if restored_reports else "抢修中")
                    task["validation_conclusion"] = conclusion
                for vehicle_id in package.get("vehicle_ids", []):
                    vehicle = self._find(VEHICLE, int(vehicle_id))
                    if vehicle:
                        if all_restored:
                            vehicle["status"] = "在库"
                            vehicle["车辆状态"] = "在库"
                            vehicle.pop("active_package_id", None)
                            vehicle.pop("active_package_no", None)
                        vehicle["抢修校验结论"] = conclusion
                        vehicle["抢修任务汇总"] = f"{package['package_no']}｜{stage}｜{conclusion}"

                if not bool(values.get("line_diagram_ok", True)):
                    raise RuntimeError("复电后线路图更新未成功")
                self._rows(LINE_TABLE).append({
                    "id": store.next_id(LINE_TABLE),
                    "package_id": package_id,
                    "package_no": package["package_no"],
                    "status": "复电更新成功" if all_restored else "复电部分更新",
                    "updated_at": timestamp,
                })
                package["line_diagram_status"] = "更新成功"
                result = {"ok": True, "message": f"{package['package_no']} 复电结果已登记：{stage}", "entry": package}
                self._idempotent[key] = deepcopy(result)
                return deepcopy(result)
            except Exception as exc:
                store.restore(snapshot)
                restored_package = self._find(PACKAGE_TABLE, package_id)
                return deepcopy({
                    "ok": False,
                    "message": f"线路图更新未成功，已回滚整包复电阶段：{exc}",
                    "entry": restored_package,
                    "rolled_back": True,
                })

    def patrol_checklist(self) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            rows = [row for row in self._rows(PATROL) if row.get("package_id")]
            return {
                "total": len(rows),
                "items": deepcopy(sorted(rows, key=lambda row: int(row.get("package_id", 0)), reverse=True)),
            }

    def vehicle_task_summary(self) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            tasks = sorted(self._rows(TASK_TABLE), key=lambda row: (int(row.get("package_id", 0)), int(row.get("arrival_order", 0))))
            packages = {int(row["id"]): row for row in self._rows(PACKAGE_TABLE)}
            items = []
            for task in tasks:
                package = packages.get(int(task.get("package_id", 0)))
                item = deepcopy(task)
                item["package_stage"] = package and package.get("stage")
                item["unified_fault"] = package and package.get("unified_fault")
                item["safety_level"] = package and package.get("safety_level")
                items.append(item)
            active_vehicles = {int(row.get("vehicle_id")) for row in tasks if row.get("status") != "已归库"}
            return {
                "total": len(items),
                "active_vehicle_count": len(active_vehicles),
                "items": deepcopy(items),
                "packages": deepcopy(list(packages.values())),
            }


repair_service = LightingRepairService()
