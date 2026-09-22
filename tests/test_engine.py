from fractions import Fraction

import pytest

from lab.engine import EngineError, Fault, apply_op, exact_total, perform_op
from lab.fixtures import initial_state, step_ops, validate_protocol
from lab.state import ZERO, frac


def test_fraction_volume_never_float():
    amount = frac("1/3")
    assert isinstance(amount, Fraction)
    total = sum((amount for _ in range(300)), ZERO)
    assert total == 100
    with pytest.raises(ValueError):
        frac(0.1)


def test_proportional_split_conserves_fraction():
    state = initial_state()
    ops = step_ops({"id": "X1", "kind": "transfer", "tip": "T1",
                    "src": "SRC:A1", "dst": "PLT:A1", "volume": "100", "index": 0})
    for name, payload in ops[:4]:
        state, _, _ = apply_op(state, name, payload)
    well = state.well("SRC:A1")
    tip = state.tip("TIP:T1")
    assert well.total == 1400
    assert tip.held_total == 100
    assert exact_total(state) == 3600


def test_capacity_overrun_rejected():
    state = initial_state()
    with pytest.raises(ValueError):
        validate_protocol([
            {"id": "BIG", "kind": "transfer", "tip": "T1", "src": "SRC:A1",
             "dst": "PLT:A1", "volume": "201"}
        ], state)


def test_aspirate_beyond_liquid_is_engine_error():
    state = initial_state()
    # 直接把 PLT:A1 排空后再尝试吸液会触发液面检测失败。
    with pytest.raises(EngineError):
        apply_op(
            state,
            "aspirate",
            {"well": "PLT:A1", "volume": "10", "step_id": "X", "compound_commit": False},
            None,
        )


def test_fault_consumes_time_but_keeps_physical_state():
    state = initial_state()
    move = step_ops({"id": "X1", "kind": "transfer", "tip": "T1",
                     "src": "SRC:A1", "dst": "PLT:A1", "volume": "100", "index": 0})[0]
    before = state.to_json()
    result = perform_op(state, move[0], move[1], Fault("motion_conflict", "move_head"))
    assert result.ok is False
    assert result.fault == "motion_conflict"
    assert result.after_state.head_slot == "HOM"
    assert result.after_state.clock == state.clock + 2
    assert result.after_state.to_json()["containers"] == before["containers"]


def test_tip_not_ready_failure_preserves_state():
    state = initial_state()
    _, pick_payload = step_ops({"id": "X1", "kind": "transfer", "tip": "T1",
                                "src": "SRC:A1", "dst": "PLT:A1", "volume": "100",
                                "index": 0})[1]
    moved, _, _ = apply_op(state, "move_head", {"target": "TIP", **pick_payload})
    result = perform_op(moved, "pick_tip", pick_payload, Fault("tip_not_ready", "pick_tip"))
    assert result.ok is False
    assert result.after_state.picked_tip is None
