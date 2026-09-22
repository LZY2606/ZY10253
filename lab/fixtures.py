"""固定 fixture：deck 布局与实验协议（容器 / 孔板 / 吸头架 / 转移 / 混匀）。"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Tuple

from .state import Container, LabState, Tip, Well, ZERO

FIXTURE_ID = "deck-alpha-1"
TIP_CAPACITY = Fraction(200)
HOME_SLOT = "HOM"
BIN_SLOT = "BIN"
WASTE_SLOT = "WST"
RACK_SLOT = "TIP"

# 每个微操作消耗的仿真秒数（有理数，确定时钟）。
OP_DURATION = {
    "move_head": Fraction(3),
    "pick_tip": Fraction(2),
    "aspirate": Fraction(4),
    "dispense": Fraction(4),
    "eject_tip": Fraction(2),
}
FAILURE_LATENCY = Fraction(2)  # 故障检测也要花时间，但不改变物理状态。

# 可注入故障 -> 适用微操作。
FAULT_OPS = {
    "tip_not_ready": {"pick_tip"},
    "level_fail": {"aspirate"},
    "motion_conflict": {"move_head"},
}

DECK_SLOT_ORDER = ["HOM", "SRC", "PLT", "TIP", "WST", "BIN"]


def initial_state() -> LabState:
    def plate_wells(slot: str, names: List[str], capacity: str, fill: Dict[str, str]):
        wells = []
        for name in names:
            contents = dict(fill.get(name, {}))
            wells.append(
                Well(
                    slot=slot,
                    name=name,
                    capacity=Fraction(capacity),
                    contents={k: Fraction(v) for k, v in contents.items()},
                )
            )
        return tuple(wells)

    source = Container(
        slot="SRC",
        kind="reservoir",
        label="试剂槽",
        wells=plate_wells(
            "SRC",
            ["A1", "A2", "A3"],
            "2000",
            {
                "A1": {"ReagentA": "1500"},
                "A2": {"ReagentB": "1200"},
                "A3": {"ReagentC": "900"},
            },
        ),
    )
    plate = Container(
        slot="PLT",
        kind="plate",
        label="目标孔板",
        wells=plate_wells("PLT", [f"A{i}" for i in range(1, 7)], "400", {}),
    )
    waste = Container(
        slot="WST",
        kind="waste",
        label="废液槽",
        wells=(Well("WST", "L", Fraction(50000), {}),),
    )
    bin_container = Container(
        slot="BIN",
        kind="bin",
        label="弃吸头箱",
        wells=(Well("BIN", "B", Fraction(1), {}),),
    )
    home = Container(
        slot="HOM",
        kind="home",
        label="机械臂原点",
        wells=(),
    )
    tips = {
        RACK_SLOT: tuple(
            Tip(rack_slot=RACK_SLOT, position=f"T{i}", state="rack", held={})
            for i in range(1, 9)
        )
    }
    return LabState(
        clock=ZERO,
        containers={"SRC": source, "PLT": plate, "WST": waste, "BIN": bin_container, "HOM": home},
        tips=tips,
        head_slot=HOME_SLOT,
        picked_tip=None,
        tip_capacity=TIP_CAPACITY,
        home_slot=HOME_SLOT,
    )


# kind: transfer | mix
# transfer: 从 src 取 volume 微升放入 dst；mix: 在 well 内吸排 volume 微升混匀。
FIXTURE_PROTOCOL: List[Dict[str, Any]] = [
    {"id": "X1", "kind": "transfer", "tip": "T1", "src": "SRC:A1", "dst": "PLT:A1", "volume": "100"},
    {"id": "X2", "kind": "transfer", "tip": "T2", "src": "SRC:A2", "dst": "PLT:A2", "volume": "100"},
    {"id": "X3", "kind": "transfer", "tip": "T3", "src": "SRC:A1", "dst": "PLT:A3", "volume": "75"},
    {"id": "X4", "kind": "transfer", "tip": "T4", "src": "SRC:A3", "dst": "PLT:A4", "volume": "150"},
    {"id": "X5", "kind": "transfer", "tip": "T5", "src": "SRC:A2", "dst": "PLT:A5", "volume": "125"},
    {"id": "X6", "kind": "transfer", "tip": "T6", "src": "SRC:A1", "dst": "PLT:A6", "volume": "60"},
    {"id": "X7", "kind": "mix", "tip": "T7", "well": "PLT:A1", "volume": "50"},
]


def step_ops(step: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """展开复合步骤为确定的微操作序列。"""
    tip_id = f"{RACK_SLOT}:{step['tip']}"
    ops: List[Tuple[str, Dict[str, Any]]] = [
        ("move_head", {"target": RACK_SLOT}),
        ("pick_tip", {"tip": tip_id}),
    ]
    if step["kind"] == "transfer":
        src_slot, src_well = step["src"].split(":", 1)
        dst_slot, dst_well = step["dst"].split(":", 1)
        ops.extend(
            [
                ("move_head", {"target": src_slot}),
                ("aspirate", {"well": step["src"], "volume": step["volume"]}),
                ("move_head", {"target": dst_slot}),
                ("dispense", {"well": step["dst"], "volume": step["volume"]}),
            ]
        )
        commit_op = len(ops) - 1  # 排液成功即复合转移可提交
    elif step["kind"] == "mix":
        slot, well_name = step["well"].split(":", 1)
        ops.extend(
            [
                ("move_head", {"target": slot}),
                ("aspirate", {"well": step["well"], "volume": step["volume"]}),
                ("dispense", {"well": step["well"], "volume": step["volume"]}),
            ]
        )
        commit_op = len(ops) - 1
    else:
        raise ValueError(f"未知步骤类型: {step['kind']}")
    ops.extend(
        [
            ("move_head", {"target": BIN_SLOT}),
            ("eject_tip", {"tip": tip_id}),
        ]
    )
    for index, (_, payload) in enumerate(ops):
        payload["step_id"] = step["id"]
        payload["step_index"] = step["index"]
        payload["op_index"] = index
        payload["compound_commit"] = index == commit_op
    return ops


def validate_protocol(protocol: List[Dict[str, Any]], state: LabState) -> None:
    """建运行前做容量、容器位置与吸头静态核对。"""
    if not protocol:
        raise ValueError("协议至少包含一个步骤")
    seen = set()
    for raw_index, step in enumerate(protocol):
        step["index"] = raw_index
        if step["id"] in seen:
            raise ValueError(f"步骤 id 重复: {step['id']}")
        seen.add(step["id"])
        if step["kind"] not in {"transfer", "mix"}:
            raise ValueError(f"步骤 {step['id']} 类型非法")
        volume = Fraction(step["volume"])
        if volume <= 0:
            raise ValueError(f"步骤 {step['id']} 容量必须为正")
        if volume > state.tip_capacity:
            raise ValueError(f"步骤 {step['id']} 超过吸头容量 {state.tip_capacity}")
        tip = state.tip(f"{RACK_SLOT}:{step['tip']}")
        if tip.state != "rack":
            raise ValueError(f"步骤 {step['id']} 吸头 {tip.position} 不在架上")
        refs = [step["src"], step["dst"]] if step["kind"] == "transfer" else [step["well"]]
        for ref in refs:
            state.well(ref)
        if step["kind"] == "transfer":
            dst = state.well(step["dst"])
            if dst.total + volume > dst.capacity:
                raise ValueError(
                    f"步骤 {step['id']} 目标孔 {step['dst']} 容量不足"
                )
