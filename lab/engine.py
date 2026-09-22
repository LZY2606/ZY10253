"""离散事件模拟引擎：微操作转移、故障、状态摘要与成分谱系。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .fixtures import FAILURE_LATENCY, OP_DURATION
from .state import LabState, ZERO, exact_text, frac, human_text


class EngineError(Exception):
    """物理规则被违反（容量、容器位置、吸头状态）。"""


@dataclass(frozen=True)
class Fault:
    fault: str  # tip_not_ready | level_fail | motion_conflict
    op_name: str  # 目标微操作
    note: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Fault":
        return cls(fault=data["fault"], op_name=data["op_name"], note=data.get("note", ""))

    def to_json(self) -> Dict[str, Any]:
        return {"fault": self.fault, "op_name": self.op_name, "note": self.note}


@dataclass(frozen=True)
class OpResult:
    op_name: str
    payload: Dict[str, Any]
    ok: bool
    fault: Optional[str]
    error: Optional[str]
    before: Dict[str, Any]
    after: Dict[str, Any]
    delta_clock: str
    lineage: Optional[Dict[str, Any]]
    before_state: LabState = field(repr=False, default=None)
    after_state: LabState = field(repr=False, default=None)


def _head_summary(state: LabState) -> Dict[str, Any]:
    picked = None
    if state.picked_tip:
        tip = state.tip(state.picked_tip)
        picked = {
            "tip": tip.rack_slot + ":" + tip.position,
            "state": tip.state,
            "held_total": exact_text(tip.held_total),
        }
    return {"head_slot": state.head_slot, "picked_tip": picked}


def physical_summary(state: LabState) -> Dict[str, Any]:
    """操作前后状态摘要：时钟 / 机械臂 / 关键孔 / 吸头。"""
    return {
        "clock": exact_text(state.clock),
        "head": _head_summary(state),
        "wells": {
            ref: {"total": exact_text(well.total), "free": exact_text(well.free)}
            for ref, well in state.wells()
        },
    }


def _split_proportional(
    contents: Dict[str, Fraction], amount: Fraction
) -> Tuple[Dict[str, Fraction], Fraction]:
    """按现有比例从孔中抽取 amount，返回（抽走成分, 未满足差额）。"""
    total = sum(contents.values(), ZERO)
    if amount > total:
        raise EngineError(f"液面检测失败：可用 {exact_text(total)} < 请求 {exact_text(amount)}")
    remaining = Fraction(amount)
    withdrawn: Dict[str, Fraction] = {}
    keys = sorted(contents)
    for component in keys[:-1]:
        share = contents[component] * amount / total
        withdrawn[component] = share
        remaining -= share
    withdrawn[keys[-1]] = remaining  # 尾差并入最后一种成分，保证分数级守恒。
    return withdrawn, ZERO


def apply_op(
    state: LabState, op_name: str, payload: Dict[str, Any], fault: Optional[Fault] = None
) -> Tuple[LabState, Optional[Dict[str, Any]], Optional[str]]:
    """纯函数式施加微操作；失败时返回错误且不得改变状态。"""
    if fault is not None:
        return state, None, f"故障注入：{fault.fault}（{fault.note or op_name}）"

    if op_name == "move_head":
        target = payload["target"]
        if target not in state.containers and target not in state.tips:
            raise EngineError(f"运动冲突：未知 deck 位置 {target}")
        return replace(state, head_slot=target), None, None

    if op_name == "pick_tip":
        tip_id = payload["tip"]
        tip = state.tip(tip_id)
        if tip.state != "rack":
            raise EngineError(f"吸头未就绪：{tip_id} 状态={tip.state}")
        if state.picked_tip is not None:
            raise EngineError("机械臂已持有吸头，不能重复拾取")
        if state.head_slot != tip.rack_slot:
            raise EngineError(
                f"容器位置不符：机械臂在 {state.head_slot}，吸头架在 {tip.rack_slot}"
            )
        picked = state.set_tip(tip.as_state("picked"))
        return replace(picked, picked_tip=tip_id), None, None

    if op_name == "aspirate":
        ref = payload["well"]
        amount = frac(payload["volume"])
        if state.picked_tip is None:
            raise EngineError("吸液前必须拾取吸头")
        tip = state.tip(state.picked_tip)
        slot, well_name = ref.split(":", 1)
        if state.head_slot != slot:
            raise EngineError(f"容器位置不符：机械臂在 {state.head_slot}，源在 {slot}")
        if tip.state != "picked" or tip.held_total != ZERO:
            raise EngineError(f"吸头状态不允许吸液：{tip.state}")
        if amount > state.tip_capacity:
            raise EngineError(
                f"超过吸头容量：{exact_text(amount)} > {exact_text(state.tip_capacity)}"
            )
        well = state.well(ref)
        if well.total < amount:
            raise EngineError(
                f"液面检测失败：{ref} 仅有 {exact_text(well.total)}，请求 {exact_text(amount)}"
            )
        withdrawn, _ = _split_proportional(well.contents, amount)
        new_contents = {
            component: well.contents.get(component, ZERO) - withdrawn.get(component, ZERO)
            for component in set(well.contents) | set(withdrawn)
        }
        new_contents = {k: v for k, v in new_contents.items() if v > ZERO}
        state = state.set_well(well.with_contents(new_contents))
        state = state.set_tip(
            tip.as_state(
                "picked",
                {
                    component: tip.held.get(component, ZERO) + withdrawn.get(component, ZERO)
                    for component in set(tip.held) | set(withdrawn)
                },
            )
        )
        lineage = {
            "kind": "aspirate",
            "step_id": payload.get("step_id"),
            "well": ref,
            "amount": exact_text(amount),
            "components": {k: exact_text(v) for k, v in sorted(withdrawn.items())},
        }
        return state, lineage, None

    if op_name == "dispense":
        ref = payload["well"]
        amount = frac(payload["volume"])
        if state.picked_tip is None:
            raise EngineError("排液前必须持有吸头")
        tip = state.tip(state.picked_tip)
        slot, well_name = ref.split(":", 1)
        if state.head_slot != slot:
            raise EngineError(f"容器位置不符：机械臂在 {state.head_slot}，目标在 {slot}")
        if tip.state != "picked" or tip.held_total == ZERO:
            raise EngineError("吸头中没有液体，无法排液")
        if tip.held_total < amount:
            raise EngineError(
                f"排液量超过吸头持液：{exact_text(amount)} > {exact_text(tip.held_total)}"
            )
        well = state.well(ref)
        if well.free < amount:
            raise EngineError(
                f"目标孔容量不足：{ref} 剩余 {exact_text(well.free)}，需要 {exact_text(amount)}"
            )
        delivered, _ = _split_proportional(tip.held, amount)
        new_contents = {
            component: well.contents.get(component, ZERO) + delivered.get(component, ZERO)
            for component in set(well.contents) | set(delivered)
        }
        state = state.set_well(well.with_contents(new_contents))
        held_left = {
            component: tip.held.get(component, ZERO) - delivered.get(component, ZERO)
            for component in set(tip.held) | set(delivered)
        }
        held_left = {k: v for k, v in held_left.items() if v > ZERO}
        state = state.set_tip(tip.as_state("empty" if not held_left else "picked", held_left))
        lineage = {
            "kind": "dispense",
            "step_id": payload.get("step_id"),
            "well": ref,
            "amount": exact_text(amount),
            "components": {k: exact_text(v) for k, v in sorted(delivered.items())},
        }
        return state, lineage, None

    if op_name == "eject_tip":
        tip_id = payload["tip"]
        tip = state.tip(tip_id)
        if state.picked_tip != tip_id:
            raise EngineError(f"吸头状态不符：机械臂未持有 {tip_id}")
        if state.head_slot != tip.rack_slot and state.head_slot != "BIN":
            raise EngineError(f"容器位置不符：只能在弃吸头箱退吸头，当前 {state.head_slot}")
        if tip.held_total != ZERO:
            raise EngineError("禁止丢弃仍含液体的吸头")
        state = state.set_tip(tip.as_state("waste"))
        return replace(state, picked_tip=None), None, None

    raise EngineError(f"未知微操作: {op_name}")


def perform_op(
    state: LabState, op_name: str, payload: Dict[str, Any], fault: Optional[Fault]
) -> OpResult:
    """施加微操作并保留前后摘要；故障消耗时间但不改变物理状态。"""
    before = physical_summary(state)

    def _failure_state() -> LabState:
        return LabState(
            clock=state.clock + FAILURE_LATENCY,
            containers=state.containers,
            tips=state.tips,
            head_slot=state.head_slot,
            picked_tip=state.picked_tip,
            tip_capacity=state.tip_capacity,
            home_slot=state.home_slot,
        )

    try:
        new_state, lineage, error = apply_op(state, op_name, payload, fault)
    except EngineError as exc:
        after_state = _failure_state()
        return OpResult(
            op_name=op_name, payload=payload, ok=False, fault=None, error=str(exc),
            before=before, after=physical_summary(after_state),
            delta_clock=exact_text(FAILURE_LATENCY), lineage=None,
            before_state=state, after_state=after_state,
        )
    if error is not None:
        after_state = _failure_state()
        return OpResult(
            op_name=op_name, payload=payload, ok=False, fault=fault.fault, error=error,
            before=before, after=physical_summary(after_state),
            delta_clock=exact_text(FAILURE_LATENCY), lineage=None,
            before_state=state, after_state=after_state,
        )
    duration = OP_DURATION[op_name]
    after_state = LabState(
        clock=new_state.clock + duration,
        containers=new_state.containers,
        tips=new_state.tips,
        head_slot=new_state.head_slot,
        picked_tip=new_state.picked_tip,
        tip_capacity=new_state.tip_capacity,
        home_slot=new_state.home_slot,
    )
    return OpResult(
        op_name=op_name, payload=payload, ok=True, fault=None, error=None,
        before=before, after=physical_summary(after_state),
        delta_clock=exact_text(duration), lineage=lineage,
        before_state=state, after_state=after_state,
    )


def composition_matrix(state: LabState) -> Dict[str, Any]:
    """用 NumPy 构建 deck 成分矩阵（float 仅用于展示/守恒核对，不入账）。"""
    components = state.all_components()
    wells = list(state.wells())
    rows = []
    matrix = np.zeros((len(wells), len(components)), dtype=np.float64)
    for row_index, (ref, well) in enumerate(wells):
        for component, amount in well.contents.items():
            matrix[row_index, components.index(component)] = float(amount)
        rows.append(ref)
    for rack in state.tips.values():
        for tip in rack:
            if tip.held:
                tip_row = np.fromiter(
                    (float(tip.held.get(component, ZERO)) for component in components),
                    dtype=np.float64,
                    count=len(components),
                )
                matrix = np.vstack([matrix, tip_row])
                rows.append(f"tip:{tip.rack_slot}:{tip.position}")
    column_totals = matrix.sum(axis=0)
    return {
        "components": components,
        "rows": rows,
        "matrix": matrix.round(9).tolist(),
        "numpy_total": float(column_totals.sum()),
    }


def exact_total(state: LabState) -> Fraction:
    """精确（Fraction）全局液体总量：孔内 + 吸头持液。"""
    total = ZERO
    for _, well in state.wells():
        total += well.total
    for rack in state.tips.values():
        for tip in rack:
            total += tip.held_total
    return total


def conservation_report(before: LabState, after: LabState) -> Dict[str, Any]:
    exact_before = exact_total(before)
    exact_after = exact_total(after)
    matrix = composition_matrix(after)
    return {
        "exact_before": exact_text(exact_before),
        "exact_after": exact_text(exact_after),
        "exact_delta": exact_text(exact_after - exact_before),
        "conserved": exact_before == exact_after,
        "numpy_after_total": round(matrix["numpy_total"], 9),
        "components": matrix["components"],
        "matrix": matrix["matrix"],
        "rows": matrix["rows"],
    }


def op_human(op_name: str) -> str:
    return {
        "move_head": "模块移动",
        "pick_tip": "拾取吸头",
        "aspirate": "吸液",
        "dispense": "排液",
        "eject_tip": "退出吸头",
    }.get(op_name, op_name)
