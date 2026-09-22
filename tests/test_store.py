import json

from lab.store import IllegalTransition
from lab.state import frac


def run_to_end(store, run_id):
    while store.get_run(run_id)["status"] == "ready":
        store.step(run_id)
    return store.run_detail(run_id)


def test_clean_run_finishes_and_conserves(store):
    run_id = store.create_run(label="clean")["id"]
    detail = run_to_end(store, run_id)
    assert detail["run"]["status"] == "done"
    assert detail["state"]["clock"] == "165"
    assert detail["conservation"]["conserved"] is True
    assert detail["conservation"]["exact_delta"] == "0"
    assert detail["lineage"]["reconciles_with_physical"] is True
    # 每个目标孔都按协议得到精确容量。
    plate = {w["name"]: w for w in detail["state"]["containers"]["PLT"]["wells"]}
    assert plate["A1"]["contents"]["ReagentA"] == "100"
    assert plate["A2"]["contents"]["ReagentB"] == "100"
    assert plate["A3"]["contents"]["ReagentA"] == "75"
    assert plate["A4"]["contents"]["ReagentC"] == "150"
    assert plate["A5"]["contents"]["ReagentB"] == "125"
    assert plate["A6"]["contents"]["ReagentA"] == "60"


def test_step_is_idempotent(store):
    run_id = store.create_run()["id"]
    first = store.step(run_id)
    again = store.step(run_id, replay_key=first["replay_key"])
    assert first["id"] == again["id"]
    detail = store.run_detail(run_id)
    assert len(detail["events"]) == 1
    assert detail["state"]["clock"] == "3"


def test_tip_not_ready_retry_and_recovery(store):
    run_id = store.create_run(
        faults=[{"fault": "tip_not_ready", "op_name": "pick_tip", "step_id": "X1"}]
    )["id"]
    failed_event = None
    while store.get_run(run_id)["status"] == "ready":
        failed_event = store.step(run_id)
    assert failed_event["ok"] is False
    assert failed_event["fault"] == "tip_not_ready"
    detail = store.run_detail(run_id)
    assert detail["run"]["status"] == "failed"
    assert detail["recovery_pending"] is False
    assert [tip for tip in detail["held_tips"] if tip["held_total"] != "0"] == []
    # 恢复从 X1 拾取前开始，不涉及吸头液体。
    child = store.resume(run_id)
    assert child["cursor"] == {"step_index": 0, "op_index": 0, "attempt": 0}
    assert child["resume_event"]["payload"]["physical_tip"] is None
    finished = run_to_end(store, child["id"])
    assert finished["run"]["status"] == "done"


def test_level_fail_at_aspirate_recovers_before_liquid_moves(store):
    run_id = store.create_run(
        faults=[{"fault": "level_fail", "op_name": "aspirate", "step_id": "X1"}]
    )["id"]
    while store.get_run(run_id)["status"] == "ready":
        store.step(run_id)
    detail = store.run_detail(run_id)
    assert detail["run"]["status"] == "failed"
    # 吸头已拾取但没有液体，不算“现实持液待恢复”。
    assert detail["recovery_pending"] is False
    assert detail["held_tips"][0]["held_total"] == "0"
    child = store.resume(run_id)
    finished = run_to_end(store, child["id"])
    assert finished["conservation"]["conserved"] is True


def test_acceptance_mid_compound_failure_keeps_physical_liquid(store):
    # 验收脚本：X3 吸液后、排液前的模块移动注入运动冲突。
    run_id = store.create_run(
        label="acceptance",
        faults=[{
            "fault": "motion_conflict",
            "op_name": "move_head",
            "op_index": 4,
            "step_id": "X3",
        }],
    )["id"]
    while store.get_run(run_id)["status"] == "ready":
        store.step(run_id)
    parent = store.run_detail(run_id)
    assert parent["run"]["status"] == "failed"
    failed = [e for e in parent["events"] if not e["ok"]]
    assert len(failed) == 1
    assert failed[0]["step_id"] == "X3"
    assert failed[0]["op_name"] == "move_head"
    # 现实：T3 已吸入 75 µL，数据库复合转移未提交。
    assert parent["held_tips"] == [{
        "tip": "TIP:T3",
        "state": "picked",
        "held_total": "75",
        "held": {"ReagentA": "75"},
    }]
    assert parent["recovery_pending"] is True

    # 恢复两次必须返回同一分支（不覆盖失败运行）。
    child_a = store.resume(run_id)
    child_b = store.resume(run_id)
    assert child_a["id"] == child_b["id"]
    payload = child_a["resume_event"]["payload"]
    assert payload["basis"] == "physical_post_aspirate"
    assert payload["physical_tip"]["held_total"] == "75"
    assert child_a["cursor"] == {"step_index": 2, "op_index": 4, "attempt": 0}

    child_detail = run_to_end(store, child_a["id"])
    assert child_detail["run"]["status"] == "done"
    # 恢复分支不重复吸液，但会排出现实液体。
    x3 = [e for e in child_detail["events"] if e["step_id"] == "X3"]
    assert not any(e["op_name"] == "aspirate" for e in x3)
    dispense = [e for e in x3 if e["op_name"] == "dispense"]
    assert len(dispense) == 1 and dispense[0]["ok"] is True
    plate_a3 = child_detail["state"]["containers"]["PLT"]["wells"][2]
    assert plate_a3["contents"] == {"ReagentA": "75"}
    # 失败父运行保留现场，不被覆盖。
    assert store.get_run(run_id)["status"] == "recovered"
    assert store.run_detail(run_id)["held_tips"][0]["held_total"] == "75"
    # 同一恢复事件重放两次仍幂等。
    r1 = store.step(child_a["id"], replay_key="resume")
    r2 = store.step(child_a["id"], replay_key="resume")
    assert r1["id"] == r2["id"]
    assert r1["op_name"] == "resume_branch"
    # 守恒与谱系对账。
    assert child_detail["conservation"]["conserved"] is True
    assert child_detail["lineage"]["reconciles_with_physical"] is True


def test_export_reset_import_replays(store):
    run_id = store.create_run(
        faults=[{"fault": "motion_conflict", "op_name": "move_head",
                 "op_index": 4, "step_id": "X3"}]
    )["id"]
    while store.get_run(run_id)["status"] == "ready":
        store.step(run_id)
    child = store.resume(run_id)
    run_to_end(store, child["id"])
    bundle = store.export_data()
    assert store.verify_export(bundle)["verified"] is True

    result = store.import_data(bundle, clear=True)
    assert result["verification"]["verified"] is True
    reloaded = store.run_detail(child["id"])
    assert reloaded["state"]["clock"] == "165"
    assert reloaded["lineage"]["reconciles_with_physical"] is True
    # 清空后导入，原始失败现场同样可以复核。
    failed_parent = store.run_detail(run_id)
    assert failed_parent["held_tips"][0]["held_total"] == "75"


def test_live_inject_level_fail(store):
    run_id = store.create_run()["id"]
    # 游标在第一步 move_head 时注入液面失败，会排到 X1 aspirate 触发。
    store.inject_fault(run_id, "level_fail", step_id="X1", note="live")
    while store.get_run(run_id)["status"] == "ready":
        store.step(run_id)
    detail = store.run_detail(run_id)
    assert detail["run"]["status"] == "failed"
    assert detail["events"][-1]["fault"] == "level_fail"
