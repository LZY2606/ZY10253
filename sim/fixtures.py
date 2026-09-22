"""固定 fixture: deck 布局、初始试剂与执行脚本。

脚本包含有理微升容量 (100/3 µL)，用于验证容量精确性。
"""
from __future__ import annotations

from fractions import Fraction

from .state import frac

DECK_LAYOUT = [[1, 2, 3], [4, 5, 6]]  # 槽位网格 (行 x 列)

PLATE_WELLS = ["A1", "A2", "A3", "B1", "B2", "B3"]  # 2 行 x 3 列
TIP_POSITIONS = [f"p{i}" for i in range(1, 9)]  # 8 个吸头位


def _well(components=None, capacity="200"):
    return {
        "capacity": frac(capacity),
        "committed": dict(components or {}),
        "physical": dict(components or {}),
        "lineage": [],
    }


def fixture_state() -> dict:
    wells = {
        "src_plate": {
            "A1": _well({"water": frac(120)}),
            "A2": _well({"buffer": frac(90)}),
            "A3": _well({"sample_x": frac(60)}),
            "B1": _well(),
            "B2": _well(),
            "B3": _well(),
        },
        "dst_plate": {w: _well() for w in PLATE_WELLS},
    }
    script = [
        {"op_id": "op1", "type": "transfer",
         "params": {"rack": "tiprack_1", "tip_pos": "p1",
                    "src": "src_plate", "src_well": "A1",
                    "dst": "dst_plate", "dst_well": "A1", "volume": "50"}},
        {"op_id": "op2", "type": "transfer",
         "params": {"rack": "tiprack_1", "tip_pos": "p2",
                    "src": "src_plate", "src_well": "A3",
                    "dst": "dst_plate", "dst_well": "A2", "volume": "100/3"}},
        {"op_id": "op3", "type": "mix",
         "params": {"rack": "tiprack_1", "tip_pos": "p3",
                    "labware": "dst_plate", "well": "A1",
                    "volume": "30", "reps": 2}},
        {"op_id": "op4", "type": "move",
         "params": {"labware": "src_plate", "to_slot": 5}},
        {"op_id": "op5", "type": "transfer",
         "params": {"rack": "tiprack_1", "tip_pos": "p4",
                    "src": "src_plate", "src_well": "A1",
                    "dst": "dst_plate", "dst_well": "A3", "volume": "25"}},
    ]
    return {
        "sim_time": 0,
        "status": "running",
        "deck": {
            "layout": DECK_LAYOUT,
            "slots": {str(s): None for row in DECK_LAYOUT for s in row},
        },
        "labware": {
            "tiprack_1": {"kind": "tiprack", "slot": 1},
            "src_plate": {"kind": "plate", "slot": 2},
            "dst_plate": {"kind": "plate", "slot": 3},
        },
        "wells": wells,
        "tips": {"tiprack_1": {p: "present" for p in TIP_POSITIONS}},
        "pipette": {"tip": None},
        "trash": [],
        "script": script,
        "op_index": 0,
        "step_index": 0,
        "active": None,
        "pending_faults": [],
    }


def initial_total_volume(state: dict) -> Fraction:
    total = Fraction(0)
    for lw in state["wells"].values():
        for w in lw.values():
            total += sum(w["physical"].values(), Fraction(0))
    return total
