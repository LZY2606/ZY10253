"""移液执行证言台 — 自动化测试。

覆盖: 有理容量精确性、复合转移提交/回滚、故障注入、检查点恢复、
恢复分支不覆盖原运行、事件重放幂等、导出/清空/导入复核、API。
"""
from __future__ import annotations

import importlib
import os
import sys
from fractions import Fraction

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.engine import Engine  # noqa: E402
from sim.state import state_from_json, volume  # noqa: E402


@pytest.fixture()
def eng(tmp_path):
    engine = Engine(str(tmp_path / "t.db"))
    engine.reset()
    yield engine
    engine.store.close()


def get_state(eng, run_id):
    return state_from_json(eng.store.get_run(run_id)["state_json"])


def well(st, lw, wk):
    return st["wells"][lw][wk]


def total_physical(st):
    total = Fraction(0)
    for plate in st["wells"].values():
        for w in plate.values():
            total += volume(w["physical"])
    tip = st["pipette"]["tip"]
    if tip:
        total += volume(tip["contents"])
    for t in st["trash"]:
        total += sum(t["contents"].values(), Fraction(0))
    return total


def test_rational_volume_exactness(eng):
    """100/3 µL 转移精确到账, 总量守恒, 无浮点漂移。"""
    r = eng.run_all("run-1")
    assert r["ok"] and r["stopped"] == "done"
    st = get_state(eng, "run-1")
    dst_a2 = well(st, "dst_plate", "A2")
    assert volume(dst_a2["physical"]) == Fraction(100, 3)
    assert dst_a2["physical"] == {"sample_x": Fraction(100, 3)}
    # 三次 100/3 等于 100, 而不是 99.999...
    assert Fraction(100, 3) * 3 == 100
    # 全系统物理总量守恒: 120 + 90 + 60 = 270
    assert total_physical(st) == Fraction(270)
    # 提交视图与物理视图最终一致
    for plate in st["wells"].values():
        for w in plate.values():
            assert w["committed"] == w["physical"]


def test_compound_transfer_commits_atomically(eng):
    """复合转移全部成功才提交; 中途 committed 视图不变。"""
    eng.step("run-1")  # pickup
    eng.step("run-1")  # aspirate: physical 已变, committed 未变
    st = get_state(eng, "run-1")
    assert volume(well(st, "src_plate", "A1")["physical"]) == Fraction(70)
    assert volume(well(st, "src_plate", "A1")["committed"]) == Fraction(120)
    assert volume(st["pipette"]["tip"]["contents"]) == Fraction(50)
    eng.step("run-1")  # dispense
    eng.step("run-1")  # drop -> commit
    st = get_state(eng, "run-1")
    assert volume(well(st, "src_plate", "A1")["committed"]) == Fraction(70)
    assert volume(well(st, "dst_plate", "A1")["committed"]) == Fraction(50)


def test_fault_after_aspirate_then_recover(eng):
    """验收场景: 吸液后、排液前注入故障。

    - 现实吸头有液体, 数据库事务未完成 (committed 回滚)。
    - 从检查点恢复: 不重复吸液, 物理状态不丢失。
    - 恢复分支不覆盖原失败运行。
    """
    eng.step("run-1")  # pickup
    eng.step("run-1")  # aspirate
    r = eng.inject_fault("run-1", "lld_failure")
    assert r["ok"]
    r = eng.step("run-1")  # dispense 失败
    assert not r["ok"] and r["run_status"] == "awaiting_recovery"
    st = get_state(eng, "run-1")
    # 物理: 吸头带液; committed: 事务未提交
    assert volume(st["pipette"]["tip"]["contents"]) == Fraction(50)
    assert volume(well(st, "src_plate", "A1")["physical"]) == Fraction(70)
    assert volume(well(st, "src_plate", "A1")["committed"]) == Fraction(120)
    assert volume(well(st, "dst_plate", "A1")["committed"]) == Fraction(0)

    rec = eng.recover("run-1")
    assert rec["ok"]
    branch = rec["new_run_id"]
    assert branch != "run-1"
    # 原失败运行保留, 未被覆盖
    old = eng.store.get_run("run-1")
    assert old["status"] == "superseded"
    old_st = state_from_json(old["state_json"])
    assert old_st["status"] == "awaiting_recovery"
    assert volume(old_st["pipette"]["tip"]["contents"]) == Fraction(50)
    # 分支: committed 回到检查点, 物理吸头余液保留
    bst = get_state(eng, branch)
    assert volume(bst["pipette"]["tip"]["contents"]) == Fraction(50)
    assert volume(well(bst, "src_plate", "A1")["physical"]) == Fraction(70)
    assert volume(well(bst, "src_plate", "A1")["committed"]) == Fraction(120)
    assert bst["step_index"] == 2  # 从失败的 dispense 继续

    r = eng.run_all(branch)
    assert r["stopped"] == "done"
    bst = get_state(eng, branch)
    assert volume(well(bst, "dst_plate", "A1")["physical"]) == Fraction(50)
    # op1 提交 70, op5 再转出 25 -> 45
    assert volume(well(bst, "src_plate", "A1")["committed"]) == Fraction(45)
    # 不重复吸液: op1 的 aspirate 事件全库只有 1 条
    asp = [e for e in eng.store.list_events()
           if e["type"] == "aspirate" and e["op_id"] == "op1"]
    assert len(asp) == 1
    # 总量守恒 (含原运行丢弃的视图不影响分支物理)
    assert total_physical(bst) == Fraction(270)


def test_recovery_replay_is_idempotent(eng):
    """同一恢复事件重放两次仍幂等: 同一分支, 事件数不变。"""
    eng.step("run-1")
    eng.step("run-1")
    eng.inject_fault("run-1", "lld_failure")
    eng.step("run-1")
    rec1 = eng.recover("run-1")
    assert rec1["ok"]
    n_events = len(eng.store.list_events())
    n_runs = len(eng.store.list_runs())
    rec2 = eng.recover("run-1")
    assert rec2["ok"] and rec2.get("idempotent")
    assert rec2["new_run_id"] == rec1["new_run_id"]
    assert len(eng.store.list_events()) == n_events
    assert len(eng.store.list_runs()) == n_runs
    # 第三次也一样
    rec3 = eng.recover("run-1")
    assert rec3["new_run_id"] == rec1["new_run_id"]


def test_step_and_inject_replay_idempotent(eng):
    """步骤事件与故障注入事件按确定性 event_id 幂等。"""
    r1 = eng.step("run-1")
    ev_id = r1["event"]["event_id"]
    # 直接重放同一事件: 存储层 INSERT OR IGNORE
    again = eng.store.insert_event(r1["event"])
    assert again is False
    assert eng.store.get_event(ev_id)["seq"] == r1["event"]["seq"]
    # 同一位置重复注入同种故障 -> 同一 event_id, 幂等
    f1 = eng.inject_fault("run-1", "tip_not_ready")
    f2 = eng.inject_fault("run-1", "tip_not_ready")
    assert f2.get("idempotent")
    assert f1["event"]["event_id"] == f2["event"]["event_id"]
    assert len([e for e in eng.store.list_events()
                if e["type"] == "inject_fault"]) == 1


def test_tip_not_ready_recovery(eng):
    """吸头未就绪进入待恢复, 恢复后脚本可完成。"""
    eng.inject_fault("run-1", "tip_not_ready")
    r = eng.step("run-1")  # pickup 失败
    assert r["run_status"] == "awaiting_recovery"
    st = get_state(eng, "run-1")
    assert st["pipette"]["tip"] is None
    rec = eng.recover("run-1")
    branch = rec["new_run_id"]
    r = eng.run_all(branch)
    assert r["stopped"] == "done"


def test_motion_conflict_recovery(tmp_path):
    eng = Engine(str(tmp_path / "m.db"))
    eng.reset()
    # 执行到 op4 (move) 之前: op1..op3 共 4+4+6=14 步
    for _ in range(14):
        eng.step("run-1")
    eng.inject_fault("run-1", "motion_conflict")
    r = eng.step("run-1")  # move 失败
    assert r["run_status"] == "awaiting_recovery"
    st = get_state(eng, "run-1")
    assert st["labware"]["src_plate"]["slot"] == 2  # 未移动
    rec = eng.recover("run-1")
    r = eng.run_all(rec["new_run_id"])
    assert r["stopped"] == "done"
    st = get_state(eng, rec["new_run_id"])
    assert st["labware"]["src_plate"]["slot"] == 5
    eng.store.close()


def test_export_import_roundtrip(eng, tmp_path):
    """导出 -> 清空 -> 重新导入, 状态哈希一致可复核。"""
    eng.step("run-1")
    eng.step("run-1")
    eng.inject_fault("run-1", "lld_failure")
    eng.step("run-1")
    rec = eng.recover("run-1")
    eng.run_all(rec["new_run_id"])
    dump = eng.export_dump()
    hashes_before = {r["run_id"]: r["state_json"]
                     for r in eng.store.list_runs()
                     for r in [eng.store.get_run(r["run_id"])]}
    # 清空数据库后重新导入
    fresh = Engine(str(tmp_path / "fresh.db"))
    fresh.store.reset()
    fresh.import_dump(dump)
    for run_id, state_json in hashes_before.items():
        assert fresh.store.get_run(run_id)["state_json"] == state_json
    assert len(fresh.store.list_events()) == len(dump["events"])
    assert len(fresh.store.list_checkpoints()) == len(dump["checkpoints"])
    fresh.store.close()


def test_api(tmp_path, monkeypatch):
    """API 冒烟: 页面标题、单步、注入、恢复、导出。"""
    monkeypatch.setenv("TESTIMONY_DB", str(tmp_path / "api.db"))
    for mod in list(sys.modules):
        if mod == "app":
            del sys.modules[mod]
    import app as app_mod
    importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)

    r = client.get("/")
    assert r.status_code == 200 and "移液执行证言台" in r.text

    s = client.get("/api/state").json()
    assert s["run"]["run_id"] == "run-1"
    assert s["run"]["sim_time"] == 0

    assert client.post("/api/step", json={"run_id": "run-1"}).json()["ok"]
    assert client.post("/api/step", json={"run_id": "run-1"}).json()["ok"]
    client.post("/api/inject", json={"run_id": "run-1", "kind": "lld_failure"})
    r = client.post("/api/step", json={"run_id": "run-1"}).json()
    assert r["run_status"] == "awaiting_recovery"
    r = client.post("/api/recover", json={"run_id": "run-1"}).json()
    assert r["ok"]
    branch = r["new_run_id"]
    r = client.post("/api/run_all", json={"run_id": branch}).json()
    assert r["stopped"] == "done"
    dump = client.get("/api/export").json()
    assert dump["format"] == "testimony-dump-v1"
    r = client.post("/api/import", json=dump).json()
    assert r["ok"]
    s = client.get("/api/state", params={"run_id": branch}).json()
    assert s["run"]["status"] == "done"
