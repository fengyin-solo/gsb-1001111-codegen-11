"""灭灯区域聚合与抢修编排业务规则。

设计要点（对应业务口径）：
- 聚合：把相邻杆号、同一回路、同一供电区的故障灯收拢成一个灭灯区域，合并为一个抢修包；
  只对故障灯做增量重算，已派单的灯具不允许重复派单。
- 主路与匝道同时故障时，抢修包级别按对通行安全影响更高的一侧取（主路 > 匝道）。
- 幂等：并发上报带幂等键，同一键只生效一次，重复上报直接返回首次结果。
- 阶段流转：编制 -> 线路图更新 -> 派单 -> 现场抢修 -> 复电确认；线路图更新未成功时
  回滚整包阶段到编制，不产生任何下游落账。
- 落账：抢修校验结论写到灯具台账、巡查清单、车辆任务汇总三处；历史不亮记录按每次复电
  结果逐次保留，不覆盖。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

LIGHTING_MODULE = "lighting"
PATROL_MODULE = "patrol"
VEHICLE_MODULE = "vehicle"
PACKAGE_MODULE = "repair_pkg"

FAULT_STATUSES = {"不亮", "闪烁"}
RESTORED_STATUS = "已修复"

# 抢修包阶段（有序）：线路图更新失败时整包回滚到第一个阶段
STAGE_DRAFT = "待编制"
STAGE_DIAGRAM = "线路图更新中"
STAGE_DISPATCH = "待派单"
STAGE_REPAIR = "现场抢修中"
STAGE_DONE = "复电归档"
PACKAGE_STAGES = [STAGE_DRAFT, STAGE_DIAGRAM, STAGE_DISPATCH, STAGE_REPAIR, STAGE_DONE]

# 道路位置对通行安全的影响级别：主路与匝道同时故障时取高级别
LOCATION_LEVEL = {"主路": 3, "匝道": 2, "辅道": 1}
DEFAULT_LOCATION_LEVEL = 0

# 相邻杆号判定阈值：同一回路/供电区内杆号差不超过该值即视为相邻
ADJACENT_POLE_GAP = 2


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _location_level(lamp: dict[str, Any]) -> int:
    location = str(lamp.get("道路位置") or "")
    if location in LOCATION_LEVEL:
        return LOCATION_LEVEL[location]
    return DEFAULT_LOCATION_LEVEL


def _level_to_location(level: int) -> str:
    for name, value in LOCATION_LEVEL.items():
        if value == level:
            return name
    return "未标注"


def _pole_number(lamp: dict[str, Any]) -> int | None:
    """取杆号最后一段数字作为序号（如 BD-K012+340-14 -> 14）。"""
    groups = re.findall(r"\d+", str(lamp.get("杆号") or ""))
    return int(groups[-1]) if groups else None


def _same_area(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """同一供电区且（同回路或杆号相邻）即收拢；同一回路的故障同样合并。"""
    if a.get("供电区") and a.get("供电区") == b.get("供电区"):
        if a.get("回路") and a.get("回路") == b.get("回路"):
            return True
        pa, pb = _pole_number(a), _pole_number(b)
        if pa is not None and pb is not None and abs(pa - pb) <= ADJACENT_POLE_GAP:
            return True
    return False


class RepairService:
    """抢修编排：内部全部串行化（_lock），保证并发上报下聚合与幂等的正确性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 幂等键 -> 首次处理结果（故障上报去重）
        self._idempotent_reports: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 读取

    def list_packages(self, *, stage: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PACKAGE_MODULE)
        if stage:
            rows = [row for row in rows if row.get("阶段") == stage]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def get_package(self, package_id: int) -> dict[str, Any] | None:
        return store.find(PACKAGE_MODULE, package_id)

    def _find_package(self, package_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(PACKAGE_MODULE) if row.get("抢修包号") == package_no),
            None,
        )

    def vehicle_tasks(self, vehicle_no: str | None = None) -> list[dict[str, Any]]:
        """车辆任务汇总：一辆车在一个抢修包里只出现一条（按包去重，不按单灯派单）。"""
        tasks = store.rows("repair_vehicle_task")
        if vehicle_no:
            tasks = [task for task in tasks if task.get("车牌号") == vehicle_no]
        return sorted(tasks, key=lambda task: int(task.get("id", 0)), reverse=True)

    # ------------------------------------------------------------ 故障上报

    def report_fault(
        self, values: dict[str, Any], idempotency_key: str | None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """上报一盏（或一组）灯具故障。

        返回 (灯具台账记录, 说明, 是否幂等命中)。并发相同幂等键只会生效一次。
        """
        lamp_no = str(values.get("灯具编号") or "").strip()
        if not lamp_no:
            return None, "缺少必填字段：灯具编号", False
        if not idempotency_key or not idempotency_key.strip():
            return None, "缺少幂等键：并发上报必须携带 idempotency_key", False
        key = idempotency_key.strip()

        with self._lock:
            cached = self._idempotent_reports.get(key)
            if cached is not None:
                return cached["lamp"], f"幂等命中：该故障已受理（抢修包 {cached['package_no']}）", True

            lamps = store.rows(LIGHTING_MODULE)
            lamp = next((row for row in lamps if str(row.get("灯具编号")) == lamp_no), None)
            is_new = lamp is None
            if is_new:
                lamp = {"id": _next_id(lamps), "pending": True, "abnormal": True}
                lamps.append(lamp)

            # 冻结阶段（已派单/抢修中）的灯具不重复受理，从源头杜绝按单灯重复派单；
            # 已归档包的灯具再次故障时允许重新聚合
            existing_no = lamp.get("抢修包号")
            if existing_no:
                existing = self._find_package(existing_no)
                if existing and existing.get("阶段") in (STAGE_DISPATCH, STAGE_REPAIR):
                    self._idempotent_reports[key] = {"lamp": lamp, "package_no": existing_no}
                    return lamp, f"灯具已在抢修包 {existing_no} 抢修中，不重复派单", True
                lamp["抢修包号"] = None

            fault_reason = str(values.get("不亮原因") or "").strip() or "巡查上报不亮"
            reported_at = _now()
            lamp.update({
                "灯具编号": lamp_no,
                "灯具类型": values.get("灯具类型") or lamp.get("灯具类型") or "路灯",
                "功率": values.get("功率") or lamp.get("功率") or "—",
                "所属路段": values.get("所属路段") or lamp.get("所属路段") or "未标注路段",
                "杆号": values.get("杆号") or lamp.get("杆号") or "—",
                "回路": values.get("回路") or lamp.get("回路") or "—",
                "供电区": values.get("供电区") or lamp.get("供电区") or "—",
                "道路位置": values.get("道路位置") or lamp.get("道路位置") or "未标注",
                "不亮原因": fault_reason,
                "设施状态": "故障待聚合",
                "status": "不亮",
                "pending": True,
                "abnormal": True,
            })
            history: list[dict[str, Any]] = list(lamp.get("不亮历史") or [])
            history.append({"时间": reported_at, "原因": fault_reason, "结果": "待复电"})
            lamp["不亮历史"] = history

            package_no = self._rebuild_clusters(int(lamp["id"]))
            self._idempotent_reports[key] = {"lamp": lamp, "package_no": package_no}
            prefix = "登记" if is_new else "更新"
            return lamp, f"故障已{prefix}并收拢进抢修包 {package_no}", False

    # ------------------------------------------------------------ 增量聚合

    def _fault_lamps(self) -> list[dict[str, Any]]:
        """参与聚合的故障灯：当前为故障态、且未挂在冻结阶段（已派单/抢修中）的包上。

        复电归档后再次故障的灯允许脱离旧包重新聚合；历史不亮记录仍留在灯具台账上。
        """
        fault: list[dict[str, Any]] = []
        for lamp in store.rows(LIGHTING_MODULE):
            if lamp.get("status") not in FAULT_STATUSES:
                continue
            package_no = lamp.get("抢修包号")
            if package_no:
                package = self._find_package(package_no)
                if package and package.get("阶段") in (STAGE_DISPATCH, STAGE_REPAIR):
                    continue
            fault.append(lamp)
        return fault

    def _rebuild_clusters(self, focus_lamp_id: int | None = None) -> str:
        """对故障灯做增量重算并查集，返回 focus 灯具最终所属抢修包号。

        编制中（待编制/线路图更新中）的包允许随新增故障合并；下游阶段冻结。
        """
        fault = self._fault_lamps()

        # 并查集：按 同供电区+（同回路或相邻杆号）归并
        parent = {id(lamp): id(lamp) for lamp in fault}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: dict[str, Any], b: dict[str, Any]) -> None:
            parent[find(id(a))] = find(id(b))

        for i, a in enumerate(fault):
            for b in fault[i + 1:]:
                if _same_area(a, b):
                    union(a, b)

        groups: dict[int, list[dict[str, Any]]] = {}
        for lamp in fault:
            groups.setdefault(find(id(lamp)), []).append(lamp)

        packages = store.rows(PACKAGE_MODULE)
        focus_no = ""
        # 复电归档的旧包仅作历史保留：把再次故障的灯从其灯具集合中摘除
        reopen_ids = {int(m["id"]) for m in fault}
        for package in packages:
            if package.get("阶段") == STAGE_DONE:
                left = [lid for lid in package.get("灯具ID集合", []) if int(lid) not in reopen_ids]
                if len(left) != len(package.get("灯具ID集合", [])):
                    package["灯具ID集合"] = left
        for members in groups.values():
            member_ids = {int(m["id"]) for m in members}
            reusable = [
                pkg
                for pkg in packages
                if pkg.get("阶段") in (STAGE_DRAFT, STAGE_DIAGRAM)
                and member_ids & set(pkg.get("灯具ID集合", []))
            ]
            if reusable:
                package = max(reusable, key=lambda pkg: int(pkg.get("id", 0)))
                # 合并被并掉的其他编制中包
                for other in reusable:
                    if other is not package:
                        self._merge_package(other, package)
            else:
                package = self._create_package()
                packages.append(package)
            self._refresh_package(package, members)
            if focus_lamp_id is not None and focus_lamp_id in member_ids:
                focus_no = str(package["抢修包号"])

        return focus_no

    def _create_package(self) -> dict[str, Any]:
        packages = store.rows(PACKAGE_MODULE)
        package_id = _next_id(packages)
        return {
            "id": package_id,
            "抢修包号": f"RPR-{package_id:04d}",
            "阶段": STAGE_DRAFT,
            "阶段序号": 0,
            "供电区": "",
            "回路集合": [],
            "影响级别": "未标注",
            "灯具数量": 0,
            "杆号范围": "",
            "所属路段": "",
            "巡查路线": [],
            "车辆到达顺序": [],
            "车牌号": "",
            "线路图版本": "未更新",
            "阶段记录": [{"时间": _now(), "阶段": STAGE_DRAFT, "说明": "灭灯区域聚合生成抢修包"}],
            "灯具ID集合": [],
            "status": STAGE_DRAFT,
            "pending": True,
            "abnormal": True,
        }

    def _merge_package(self, src: dict[str, Any], dst: dict[str, Any]) -> None:
        """增量重算时把被并掉的编制中包合入目标包。"""
        for lamp_id in src.get("灯具ID集合", []):
            lamp = store.find(LIGHTING_MODULE, int(lamp_id))
            if lamp is not None:
                lamp["抢修包号"] = dst["抢修包号"]
        dst["阶段记录"].append({
            "时间": _now(),
            "阶段": dst["阶段"],
            "说明": f"聚合重算：合并原抢修包 {src['抢修包号']}",
        })
        store.rows(PACKAGE_MODULE).remove(src)

    def _refresh_package(self, package: dict[str, Any], members: list[dict[str, Any]]) -> None:
        """按当前故障灯集合统一写法刷新抢修包口径（不覆盖阶段）。"""
        circuits = sorted({str(m.get("回路")) for m in members if m.get("回路")})
        areas = {str(m.get("供电区")) for m in members if m.get("供电区")}
        roads = sorted({str(m.get("所属路段")) for m in members if m.get("所属路段")})
        poles = sorted((p for p in (_pole_number(m) for m in members) if p is not None))
        level = max((_location_level(m) for m in members), default=DEFAULT_LOCATION_LEVEL)

        package["供电区"] = "、".join(sorted(areas)) if areas else "—"
        package["回路集合"] = circuits
        package["影响级别"] = _level_to_location(level)
        package["灯具数量"] = len(members)
        package["杆号范围"] = f"{poles[0]}~{poles[-1]}号杆" if poles else "—"
        package["所属路段"] = "、".join(roads) if roads else "—"

        # 巡查路线：沿杆号从小到大走；车辆到达顺序按影响级别高的区域优先、同级按杆号
        route = self._patrol_route(members)
        package["巡查路线"] = route
        package["车辆到达顺序"] = self._arrival_order(members)
        package["灯具ID集合"] = [int(m["id"]) for m in members]

        for lamp in members:
            lamp["抢修包号"] = package["抢修包号"]
            if lamp.get("status") in FAULT_STATUSES and not lamp.get("派单锁定"):
                lamp["设施状态"] = f"故障已聚合·{package['抢修包号']}"

    def _patrol_route(self, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(members, key=lambda m: (_pole_number(m) is None, _pole_number(m) or 0))
        route: list[dict[str, Any]] = []
        for index, lamp in enumerate(ordered, start=1):
            route.append({
                "顺序": index,
                "灯具编号": lamp.get("灯具编号"),
                "杆号": lamp.get("杆号"),
                "回路": lamp.get("回路"),
                "道路位置": lamp.get("道路位置"),
            })
        return route

    def _arrival_order(self, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """车辆到达顺序：影响通行安全级别高的故障点先到，同级别按杆号推进。"""
        ordered = sorted(
            members,
            key=lambda m: (-_location_level(m), _pole_number(m) is None, _pole_number(m) or 0),
        )
        seen: set[str] = set()
        arrival: list[dict[str, Any]] = []
        for lamp in ordered:
            key = str(lamp.get("道路位置"))
            if key in seen:
                continue
            seen.add(key)
            arrival.append({
                "顺序": len(arrival) + 1,
                "道路位置": lamp.get("道路位置"),
                "供电区": lamp.get("供电区"),
                "起始杆号": lamp.get("杆号"),
                "级别": _level_to_location(_location_level(lamp)),
            })
        return arrival

    # ------------------------------------------------------------ 阶段流转

    def _set_stage(self, package: dict[str, Any], stage: str, note: str) -> None:
        package["阶段"] = stage
        package["阶段序号"] = PACKAGE_STAGES.index(stage)
        package["status"] = stage
        package["pending"] = stage != STAGE_DONE
        package["阶段记录"].append({"时间": _now(), "阶段": stage, "说明": note})

    def update_diagram(self, package_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """线路图更新：成功才推进阶段；失败回滚整包到待编制，且不产生任何下游落账。"""
        with self._lock:
            package = store.find(PACKAGE_MODULE, package_id)
            if package is None:
                return None, f"抢修包 {package_id} 不存在"
            if package["阶段"] not in (STAGE_DRAFT, STAGE_DIAGRAM):
                return None, f"抢修包当前为「{package['阶段']}」，不能再更新线路图"

            success = bool(values.get("成功", True))
            self._set_stage(package, STAGE_DIAGRAM, "开始更新供电线路图")
            if not success:
                # 回滚整包阶段：回到编制起点，线路图版本作废
                self._set_stage(package, STAGE_DRAFT, "线路图更新未成功，整包阶段回滚")
                package["线路图版本"] = "更新失败已回滚"
                return package, "线路图更新未成功，已回滚整包阶段，未派单"

            version = str(values.get("线路图版本") or f"DWG-{datetime.now().strftime('%Y%m%d%H%M')}")
            package["线路图版本"] = version
            self._set_stage(package, STAGE_DISPATCH, f"线路图 {version} 更新成功，进入派单")
            return package, f"线路图已更新至 {version}，抢修包进入派单阶段"

    def dispatch(self, package_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """派单校验：线路图必须成功；结论落到灯具台账、巡查清单、车辆任务汇总。"""
        with self._lock:
            package = store.find(PACKAGE_MODULE, package_id)
            if package is None:
                return None, f"抢修包 {package_id} 不存在"
            if package["阶段"] != STAGE_DISPATCH:
                return None, f"抢修包当前为「{package['阶段']}」，需先成功更新线路图再派单"

            plate = str(values.get("车牌号") or "").strip()
            vehicle = next(
                (row for row in store.rows(VEHICLE_MODULE) if str(row.get("车牌号")) == plate),
                None,
            )
            if not plate:
                return None, "缺少必填字段：车牌号"
            if vehicle is None:
                return None, f"车辆 {plate} 不在养护车辆台账中，派单校验未通过"
            if vehicle.get("status") == "维修":
                return None, f"车辆 {plate} 处于维修状态，不能承担抢修任务"
            busy = next(
                (task for task in store.rows("repair_vehicle_task")
                 if task.get("车牌号") == plate and task.get("任务状态") == STAGE_REPAIR),
                None,
            )
            if busy is not None:
                return None, f"车辆 {plate} 正在执行抢修包 {busy['抢修包号']}，不能重复派车"

            # 校验结论落灯具台账：整包一次性锁定，禁止按单灯重复派单
            member_lamps = [
                lamp
                for lamp_id in package.get("灯具ID集合", [])
                if (lamp := store.find(LIGHTING_MODULE, int(lamp_id))) is not None
            ]
            for lamp in member_lamps:
                lamp["派单锁定"] = True
                lamp["抢修包号"] = package["抢修包号"]
                lamp["设施状态"] = f"已派单抢修·{package['抢修包号']}"
                lamp["status"] = "不亮"

            # 落巡查清单：一个抢修包一条巡查任务（不复用单灯记录）
            patrol_rows = store.rows(PATROL_MODULE)
            patrol_id = _next_id(patrol_rows)
            first_route = package["巡查路线"][0] if package["巡查路线"] else {}
            patrol_rows.append({
                "id": patrol_id,
                "status": "待巡查",
                "pending": True,
                "abnormal": True,
                "巡查编号": f"PATR-R{package_id:04d}",
                "巡查路段": package["所属路段"],
                "巡查日期": _now()[:10],
                "巡查人员": str(values.get("巡查人员") or "抢修班组"),
                "巡查车辆": plate,
                "发现问题": f"灭灯区域 {package['抢修包号']}：{package['灯具数量']} 盏灯不亮"
                             f"（{package['供电区']} / {package['影响级别']}）",
                "处置措施": "随抢修包统一巡查，路线按杆号递增",
                "巡查状态": "待巡查",
                "来源": "灭灯抢修聚合",
                "抢修包号": package["抢修包号"],
                "巡查路线": package["巡查路线"],
                "起点杆号": first_route.get("杆号"),
            })

            # 落车辆任务汇总：一个包一辆车一条
            tasks = [task for task in store.rows("repair_vehicle_task")
                     if task.get("抢修包号") != package["抢修包号"]]
            store.replace_rows("repair_vehicle_task", tasks)
            tasks.append({
                "id": _next_id(tasks),
                "抢修包号": package["抢修包号"],
                "车牌号": plate,
                "车辆编号": vehicle.get("车辆编号"),
                "驾驶员": vehicle.get("驾驶员"),
                "影响级别": package["影响级别"],
                "灯具数量": package["灯具数量"],
                "到达顺序": package["车辆到达顺序"],
                "任务状态": STAGE_REPAIR,
                "派单时间": _now(),
                "复电时间": None,
            })
            vehicle["status"] = "出车作业"
            vehicle["车辆状态"] = f"抢修出车·{package['抢修包号']}"

            package["车牌号"] = plate
            self._set_stage(package, STAGE_REPAIR, f"派单校验通过，车辆 {plate} 出车抢修")
            return package, f"抢修包已派单给 {plate}，台账/巡查/车辆任务三处已同步"

    def restore(self, package_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """复电确认：逐灯保留本次复电结果到历史不亮记录，整包归档并释放车辆。"""
        with self._lock:
            package = store.find(PACKAGE_MODULE, package_id)
            if package is None:
                return None, f"抢修包 {package_id} 不存在"
            if package["阶段"] != STAGE_REPAIR:
                return None, f"抢修包当前为「{package['阶段']}」，不能复电确认"

            restored_at = _now()
            result = str(values.get("复电结果") or "复电正常")
            restored_lamps: list[dict[str, Any]] = []
            for lamp_id in package.get("灯具ID集合", []):
                lamp = store.find(LIGHTING_MODULE, int(lamp_id))
                if lamp is None:
                    continue
                lamp["status"] = RESTORED_STATUS
                lamp["设施状态"] = f"已复电·{result}"
                lamp["pending"] = False
                lamp["abnormal"] = False
                lamp["派单锁定"] = False
                # 历史不亮记录按每次复电结果保留：只把最近一条“待复电”补成实际结果
                history: list[dict[str, Any]] = list(lamp.get("不亮历史") or [])
                for item in reversed(history):
                    if item.get("结果") == "待复电":
                        item["结果"] = result
                        item["复电时间"] = restored_at
                        item["抢修包号"] = package["抢修包号"]
                        break
                lamp["不亮历史"] = history
                restored_lamps.append(lamp)

            # 巡查清单结论
            for patrol in store.rows(PATROL_MODULE):
                if patrol.get("抢修包号") == package["抢修包号"]:
                    patrol["status"] = "已复核"
                    patrol["pending"] = False
                    patrol["abnormal"] = False
                    patrol["巡查状态"] = "已复核"
                    patrol["处置措施"] = f"复电确认：{result}（{restored_at}）"

            # 车辆任务汇总结论
            for task in store.rows("repair_vehicle_task"):
                if task.get("抢修包号") == package["抢修包号"]:
                    task["任务状态"] = STAGE_DONE
                    task["复电时间"] = restored_at
                    task["复电结果"] = result
            plate = package.get("车牌号")
            if plate:
                vehicle = next(
                    (row for row in store.rows(VEHICLE_MODULE) if str(row.get("车牌号")) == plate),
                    None,
                )
                if vehicle is not None:
                    vehicle["status"] = "在库"
                    vehicle["车辆状态"] = "已归库"

            self._set_stage(package, STAGE_DONE, f"复电确认：{result}，抢修包归档")
            return package, f"抢修包复电归档，{len(restored_lamps)} 盏灯复电结果已逐次记入历史"

    def rebuild(self) -> str:
        """增量重算：按当前故障灯重新聚合并查集，编制中包允许合并，派单后冻结。"""
        with self._lock:
            count = len(self._fault_lamps())
            if not count:
                return "当前没有可聚合的故障灯具"
            self._rebuild_clusters()
            return f"增量重算完成，覆盖 {count} 盏故障灯"

    def reseed_demo(self) -> str:
        """演示辅助：清掉聚合产物并按灯具台账当前故障灯重算一次。"""
        with self._lock:
            store.replace_rows(PACKAGE_MODULE, [])
            store.replace_rows("repair_vehicle_task", [])
            self._idempotent_reports.clear()
            for lamp in store.rows(LIGHTING_MODULE):
                lamp.pop("抢修包号", None)
                lamp.pop("派单锁定", None)
            fault = self._fault_lamps()
            if not fault:
                return "当前没有故障灯具，未生成抢修包"
            self._rebuild_clusters()
            return f"已按 {len(fault)} 盏故障灯重新聚合"


repair_service = RepairService()
