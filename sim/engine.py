"""离散事件模拟引擎。

关键语义:
- 容量全部使用有理微升 (Fraction), 不存在浮点累积。
- 复合转移 = pickup -> aspirate -> dispense -> drop, 全部成功才提交
  (committed 视图与 physical 视图对齐); 中途失败进入 awaiting_recovery,
  committed 视图回滚到最近检查点, 但 physical 视图保留现实
  (例如吸头里已吸入的液体)。
- 恢复从最后一个可提交检查点派生新分支 run, 不覆盖原失败运行;
  分支继承失败时刻的 physical 状态 (吸头余液不丢失), 并从失败步骤
  继续 (不重复吸液)。
- 所有事件以确定性 event_id 写入, 重复应用幂等。
"""
from __future__ import annotations

from fractions import Fraction

from . import fixtures
from .state import (deep_copy, fmt_frac, frac, state_from_json, state_hash,
                    state_to_json, volume, draw)
from .store import Store

FAULT_CATEGORY = {
    "tip_not_ready": "tip",
    "lld_failure": "liquid",
    "motion_conflict": "motion",
}
FAULT_LABELS = {
    "tip_not_ready": "吸头未就绪",
    "lld_failure": "液面检测失败",
    "motion_conflict": "运动冲突",
}
STEP_DURATION = {
    "pickup_tip": 2, "aspirate": 4, "dispense": 4, "drop_tip": 1, "move": 6,
}
TIP_MAX_VOLUME = "300"


def expand_op(op: dict) -> list:
    """把脚本操作展开为原子步骤序列。"""
    p = op["params"]
    if op["type"] == "transfer":
        return [
            {"name": "pickup_tip", "cat": "tip",
             "rack": p["rack"], "pos": p["tip_pos"]},
            {"name": "aspirate", "cat": "liquid", "labware": p["src"],
             "well": p["src_well"], "volume": p["volume"]},
            {"name": "dispense", "cat": "liquid", "labware": p["dst"],
             "well": p["dst_well"], "volume": p["volume"]},
            {"name": "drop_tip", "cat": "tip"},
        ]
    if op["type"] == "mix":
        steps = [{"name": "pickup_tip", "cat": "tip",
                  "rack": p["rack"], "pos": p["tip_pos"]}]
        for _ in range(int(p["reps"])):
            steps.append({"name": "aspirate", "cat": "liquid",
                          "labware": p["labware"], "well": p["well"],
                          "volume": p["volume"]})
            steps.append({"name": "dispense", "cat": "liquid",
                          "labware": p["labware"], "well": p["well"],
                          "volume": p["volume"]})
        steps.append({"name": "drop_tip", "cat": "tip"})
        return steps
    if op["type"] == "move":
        return [{"name": "move", "cat": "motion",
                 "labware": p["labware"], "to_slot": p["to_slot"]}]
    raise ValueError(f"未知操作类型: {op['type']}")


def summarize(st: dict) -> dict:
    """状态摘要: 哈希 + 非空孔体积 + 吸头 + 提交偏差。"""
    wells = {}
    divergent = False
    for lw, plate in st["wells"].items():
        for wkey, w in plate.items():
            vol = volume(w["physical"])
            if vol:
                wells[f"{lw}/{wkey}"] = fmt_frac(vol)
            if {k: v for k, v in w["committed"].items() if v} != \
               {k: v for k, v in w["physical"].items() if v}:
                divergent = True
    tip = st["pipette"]["tip"]
    return {
        "hash": state_hash(st),
        "wells": wells,
        "tip": None if not tip else {
            "tip_id": tip["tip_id"], "volume": fmt_frac(volume(tip["contents"])),
        },
        "divergent": divergent,
    }


def apply_step(st: dict, sd: dict, ctx: dict):
    """对 physical 视图应用一个原子步骤。返回 None 或错误串。"""
    name = sd["name"]
    if name == "pickup_tip":
        if st["pipette"]["tip"] is not None:
            return "移液器上已有吸头"
        rack = st["tips"].get(sd["rack"], {})
        if rack.get(sd["pos"]) != "present":
            return f"吸头缺失: {sd['rack']}/{sd['pos']}"
        rack[sd["pos"]] = "empty"
        st["pipette"]["tip"] = {
            "tip_id": f"{sd['rack']}/{sd['pos']}",
            "contents": {},
            "max_volume": frac(TIP_MAX_VOLUME),
        }
        return None
    if name == "aspirate":
        tip = st["pipette"]["tip"]
        if tip is None:
            return "未拾取吸头"
        well = st["wells"][sd["labware"]][sd["well"]]
        vol = frac(sd["volume"])
        if volume(tip["contents"]) + vol > tip["max_volume"]:
            return "超出吸头最大容量"
        taken = draw(well["physical"], vol)
        if taken is None:
            return "孔内液量不足"
        for comp, amount in taken.items():
            well["physical"][comp] -= amount
            tip["contents"][comp] = tip["contents"].get(comp, Fraction(0)) + amount
        return None
    if name == "dispense":
        tip = st["pipette"]["tip"]
        if tip is None:
            return "未拾取吸头"
        well = st["wells"][sd["labware"]][sd["well"]]
        vol = frac(sd["volume"])
        if volume(well["physical"]) + vol > well["capacity"]:
            return "超出孔容量"
        out = draw(tip["contents"], vol)
        if out is None:
            return "吸头内液量不足"
        for comp, amount in out.items():
            tip["contents"][comp] -= amount
            well["physical"][comp] = well["physical"].get(comp, Fraction(0)) + amount
        well["lineage"].append({
            "event": ctx["event_id"], "op": ctx["op_id"],
            "volume": fmt_frac(vol),
            "components": {c: fmt_frac(a) for c, a in out.items() if a},
        })
        return None
    if name == "drop_tip":
        tip = st["pipette"]["tip"]
        if tip is None:
            return "未拾取吸头"
        st["trash"].append({
            "tip_id": tip["tip_id"],
            "contents": {c: a for c, a in tip["contents"].items() if a},
        })
        st["pipette"]["tip"] = None
        return None
    if name == "move":
        target = str(sd["to_slot"])
        if st["deck"]["slots"].get(target) is not None:
            return f"目标槽位被占用: {target}"
        lw = sd["labware"]
        old = str(st["labware"][lw]["slot"])
        st["deck"]["slots"][old] = None
        st["deck"]["slots"][target] = lw
        st["labware"][lw]["slot"] = sd["to_slot"]
        return None
    return f"未知步骤: {name}"


def commit(st: dict):
    """提交复合操作: committed 视图对齐 physical。"""
    for plate in st["wells"].values():
        for w in plate.values():
            w["committed"] = deep_copy(w["physical"])


class Engine:
    def __init__(self, db_path: str):
        self.store = Store(db_path)

    # ---------- 初始化 ----------
    def ensure_initialized(self):
        if not self.store.has_runs():
            self.reset()

    def reset(self):
        self.store.reset()
        st = fixtures.fixture_state()
        self.store.insert_run("run-1", None, None, "running", state_to_json(st))
        return "run-1"

    # ---------- 内部工具 ----------
    def _load(self, run_id):
        row = self.store.get_run(run_id)
        if not row:
            raise KeyError(f"运行不存在: {run_id}")
        return row, state_from_json(row["state_json"])

    def _save(self, run_id, st):
        self.store.update_run(run_id, st["status"], state_to_json(st))

    def _record(self, run_id, st, event_id, op_id, etype, status, detail, pre):
        post = summarize(st)
        evt = {
            "event_id": event_id, "run_id": run_id,
            "seq": self.store.next_event_seq(), "sim_time": st["sim_time"],
            "op_id": op_id, "type": etype, "status": status,
            "detail": detail, "pre": pre, "post": post,
        }
        self.store.insert_event(evt)
        return evt

    def _make_checkpoint(self, run_id, st, op):
        ckpt_id = f"{run_id}:ckpt:{st['op_index']}:{op['op_id']}"
        self.store.insert_checkpoint(
            ckpt_id, run_id, op["op_id"], st["op_index"],
            st["sim_time"], state_to_json(st))
        return ckpt_id

    @staticmethod
    def _consume_fault(st, sd):
        for kind in list(st["pending_faults"]):
            if FAULT_CATEGORY[kind] == sd["cat"]:
                st["pending_faults"].remove(kind)
                return kind
        return None

    # ---------- 单步执行 ----------
    def step(self, run_id: str) -> dict:
        row, st = self._load(run_id)
        if st["status"] == "done":
            return {"ok": False, "terminal": True, "error": "脚本已完成"}
        if st["status"] != "running":
            return {"ok": False, "terminal": True,
                    "error": f"运行状态为 {st['status']}, 需要从检查点恢复"}
        op = st["script"][st["op_index"]]
        steps = expand_op(op)
        if st["step_index"] == 0:
            self._make_checkpoint(run_id, st, op)
        sd = steps[st["step_index"]]
        event_id = f"{run_id}:{op['op_id']}:s{st['step_index']}:{sd['name']}"
        existing = self.store.get_event(event_id)
        if existing:
            return {"ok": existing["status"] == "ok", "event": existing,
                    "idempotent": True}
        pre = summarize(st)
        fault = self._consume_fault(st, sd)
        error = None
        if fault:
            error = FAULT_LABELS[fault]
        else:
            error = apply_step(st, sd, {"event_id": event_id,
                                        "op_id": op["op_id"]})
        if error:
            st["sim_time"] += 1
            st["status"] = "awaiting_recovery"
            st["active"] = {
                "op_id": op["op_id"], "op_index": st["op_index"],
                "failed_step_index": st["step_index"],
                "failed_step": sd["name"], "fault": fault or "execution_error",
                "error": error,
            }
            evt = self._record(run_id, st, event_id, op["op_id"], sd["name"],
                               "failed", {"step": sd, "error": error,
                                          "fault": fault}, pre)
            self._save(run_id, st)
            return {"ok": False, "event": evt,
                    "run_status": "awaiting_recovery", "error": error}
        st["sim_time"] += STEP_DURATION[sd["name"]]
        st["step_index"] += 1
        committed = False
        if st["step_index"] >= len(steps):
            commit(st)
            st["op_index"] += 1
            st["step_index"] = 0
            committed = True
            if st["op_index"] >= len(st["script"]):
                st["status"] = "done"
        evt = self._record(run_id, st, event_id, op["op_id"], sd["name"],
                           "ok", {"step": sd}, pre)
        self._save(run_id, st)
        return {"ok": True, "event": evt, "committed": committed,
                "run_status": st["status"]}

    def run_all(self, run_id: str, limit: int = 500) -> dict:
        events = []
        for _ in range(limit):
            r = self.step(run_id)
            if r.get("event") and not r.get("idempotent"):
                events.append(r["event"]["event_id"])
            if r.get("terminal") or r.get("run_status") == "awaiting_recovery":
                return {"ok": r.get("ok", False), "stopped": r.get("error"),
                        "events": events}
            if r.get("run_status") == "done":
                return {"ok": True, "stopped": "done", "events": events}
        return {"ok": False, "stopped": "limit", "events": events}

    # ---------- 故障注入 ----------
    def inject_fault(self, run_id: str, kind: str) -> dict:
        if kind not in FAULT_CATEGORY:
            raise ValueError(f"未知故障类型: {kind}")
        row, st = self._load(run_id)
        if st["status"] != "running":
            return {"ok": False, "error": f"运行状态为 {st['status']}, 无法注入"}
        event_id = (f"{run_id}:fault@{st['op_index']}.{st['step_index']}:{kind}")
        existing = self.store.get_event(event_id)
        if existing:
            return {"ok": True, "event": existing, "idempotent": True}
        pre = summarize(st)
        st["pending_faults"].append(kind)
        evt = self._record(run_id, st, event_id, None, "inject_fault", "ok",
                           {"fault": kind, "label": FAULT_LABELS[kind]}, pre)
        self._save(run_id, st)
        return {"ok": True, "event": evt}

    # ---------- 检查点恢复 ----------
    def recover(self, run_id: str) -> dict:
        row, st = self._load(run_id)
        ck = self.store.latest_checkpoint(run_id)
        if not ck:
            return {"ok": False, "error": "没有可提交检查点"}
        rec_event_id = f"recover:{run_id}:{ck['checkpoint_id']}"
        existing = self.store.get_event(rec_event_id)
        if existing:
            return {"ok": True, "new_run_id": existing["detail"]["new_run_id"],
                    "event": existing, "idempotent": True}
        if st["status"] != "awaiting_recovery":
            return {"ok": False,
                    "error": f"运行状态为 {st['status']}, 无需恢复"}
        ckst = state_from_json(ck["state_json"])
        new = deep_copy(ckst)
        # committed 视图回到检查点; physical 现实(含吸头余液)整体继承。
        for lw, plate in new["wells"].items():
            for wkey, w in plate.items():
                src = st["wells"][lw][wkey]
                w["physical"] = deep_copy(src["physical"])
                w["lineage"] = deep_copy(src["lineage"])
        for key in ("tips", "pipette", "trash", "deck", "labware"):
            new[key] = deep_copy(st[key])
        new["sim_time"] = st["sim_time"]
        new["op_index"] = st["active"]["op_index"]
        new["step_index"] = st["active"]["failed_step_index"]
        new["status"] = "running"
        new["active"] = None
        new["pending_faults"] = list(st["pending_faults"])
        new_run_id = f"{run_id}~rec{st['active']['op_index']}"
        self.store.insert_run(new_run_id, run_id, ck["checkpoint_id"],
                              "running", state_to_json(new))
        pre = summarize(st)
        evt = {
            "event_id": rec_event_id, "run_id": new_run_id,
            "seq": self.store.next_event_seq(), "sim_time": new["sim_time"],
            "op_id": st["active"]["op_id"], "type": "recovery", "status": "ok",
            "detail": {"recovered_from": run_id,
                       "checkpoint_id": ck["checkpoint_id"],
                       "new_run_id": new_run_id,
                       "resume_step_index": st["active"]["failed_step_index"],
                       "carried_tip": summarize(st)["tip"]},
            "pre": pre, "post": summarize(new),
        }
        self.store.insert_event(evt)
        self.store.update_run(run_id, "superseded", state_to_json(st))
        return {"ok": True, "new_run_id": new_run_id, "event": evt}

    # ---------- 导出 / 导入 ----------
    def export_dump(self) -> dict:
        return self.store.export_dump()

    def import_dump(self, dump: dict) -> dict:
        self.store.import_dump(dump)
        return {"ok": True, "runs": len(dump["runs"]),
                "events": len(dump["events"]),
                "checkpoints": len(dump["checkpoints"])}
