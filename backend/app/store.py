"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS

FAULT_STATUSES = {"不亮", "闪烁"}
LOCATION_LEVEL = {"主路": 3, "匝道": 2, "辅道": 1}
ADJACENT_POLE_GAP = 2


def _pole_number(lamp: dict[str, Any]) -> int | None:
    groups = re.findall(r"\d+", str(lamp.get("杆号") or ""))
    return int(groups[-1]) if groups else None


def _same_area(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("供电区") and a.get("供电区") == b.get("供电区"):
        if a.get("回路") and a.get("回路") == b.get("回路"):
            return True
        pa, pb = _pole_number(a), _pole_number(b)
        if pa is not None and pb is not None and abs(pa - pb) <= ADJACENT_POLE_GAP:
            return True
    return False


def _bootstrap_repair_packages(tables: dict[str, list[dict[str, Any]]]) -> None:
    """按种子故障灯构建初始抢修包：同供电区+（同回路/相邻杆号）收拢。"""
    fault = [
        row for row in tables.get("lighting", [])
        if row.get("status") in FAULT_STATUSES
    ]
    parent = {id(lamp): id(lamp) for lamp in fault}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(fault):
        for b in fault[i + 1:]:
            if _same_area(a, b):
                parent[find(id(a))] = find(id(b))

    groups: dict[int, list[dict[str, Any]]] = {}
    for lamp in fault:
        groups.setdefault(find(id(lamp)), []).append(lamp)

    packages: list[dict[str, Any]] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for index, members in enumerate(sorted(groups.values(), key=lambda ms: min(_pole_number(m) or 0 for m in ms)), start=1):
        circuits = sorted({str(m.get("回路")) for m in members if m.get("回路")})
        areas = {str(m.get("供电区")) for m in members if m.get("供电区")}
        roads = sorted({str(m.get("所属路段")) for m in members if m.get("所属路段")})
        poles = sorted(p for p in (_pole_number(m) for m in members) if p is not None)
        level = max((LOCATION_LEVEL.get(str(m.get("道路位置")), 0) for m in members), default=0)
        level_name = next((name for name, value in LOCATION_LEVEL.items() if value == level), "未标注")
        ordered = sorted(members, key=lambda m: (_pole_number(m) is None, _pole_number(m) or 0))
        route = [
            {"顺序": i, "灯具编号": m.get("灯具编号"), "杆号": m.get("杆号"),
             "回路": m.get("回路"), "道路位置": m.get("道路位置")}
            for i, m in enumerate(ordered, start=1)
        ]
        arrival_order = sorted(members, key=lambda m: (-LOCATION_LEVEL.get(str(m.get("道路位置")), 0), _pole_number(m) or 0))
        seen: set[str] = set()
        arrival: list[dict[str, Any]] = []
        for m in arrival_order:
            key = str(m.get("道路位置"))
            if key in seen:
                continue
            seen.add(key)
            arrival.append({"顺序": len(arrival) + 1, "道路位置": m.get("道路位置"),
                            "供电区": m.get("供电区"), "起始杆号": m.get("杆号"), "级别": key})
        package_no = f"RPR-{index:04d}"
        packages.append({
            "id": index,
            "抢修包号": package_no,
            "阶段": "待编制",
            "阶段序号": 0,
            "供电区": "、".join(sorted(areas)) if areas else "—",
            "回路集合": circuits,
            "影响级别": level_name,
            "灯具数量": len(members),
            "杆号范围": f"{poles[0]}~{poles[-1]}号杆" if poles else "—",
            "所属路段": "、".join(roads) if roads else "—",
            "巡查路线": route,
            "车辆到达顺序": arrival,
            "车牌号": "",
            "线路图版本": "未更新",
            "阶段记录": [{"时间": now, "阶段": "待编制", "说明": "灭灯区域聚合生成抢修包"}],
            "灯具ID集合": [int(m["id"]) for m in members],
            "status": "待编制",
            "pending": True,
            "abnormal": True,
        })
        for lamp in members:
            lamp["抢修包号"] = package_no
            lamp["设施状态"] = f"故障已聚合·{package_no}"
            lamp.setdefault("不亮历史", []).append(
                {"时间": now, "原因": lamp.get("不亮原因") or "故障上报", "结果": "待复电"}
            )
    tables["repair_pkg"] = packages
    tables.setdefault("repair_vehicle_task", [])


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        _bootstrap_repair_packages(self._tables)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def replace_rows(self, module: str, rows: list[dict[str, Any]]) -> None:
        """整表替换：抢修包重算/演示重置等场景使用。"""
        self._tables[module] = rows

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
