"""SQLite 持久层：运行 / 事件 / 检查点 / 故障计划 / 恢复分支。

关键约束：事件与检查点在同一事务内提交。检查点携带的是
*物理现实状态*，吸液后即使复合转移尚未提交，吸头持液也已落盘，
因此恢复时不会重复吸液、也不会丢掉现实中的液体。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import fixtures
from .engine import EngineError, Fault, OpResult, composition_matrix, exact_total, perform_op
from .state import LabState, ZERO, exact_text, frac

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL,
    parent_id INTEGER,
    branch_no INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    status TEXT NOT NULL,
    step_index INTEGER NOT NULL DEFAULT 0,
    op_index INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    faults_json TEXT NOT NULL DEFAULT '[]',
    initial_total TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    replay_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    step_id TEXT,
    step_index INTEGER,
    op_index INTEGER,
    attempt INTEGER NOT NULL,
    op_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ok INTEGER NOT NULL,
    fault TEXT,
    error TEXT,
    delta_clock TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    lineage_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, replay_key),
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    event_id INTEGER,
    step_index INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    commit_point INTEGER NOT NULL,
    clock TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NotFound(Exception):
    pass


class IllegalTransition(Exception):
    pass


class Store:
    def __init__(self, path: str | Path = "data/testimony.db") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 运行管理
    # ------------------------------------------------------------------
    def create_run(
        self,
        protocol: Optional[List[Dict[str, Any]]] = None,
        faults: Optional[List[Dict[str, Any]]] = None,
        label: str = "primary",
    ) -> Dict[str, Any]:
        protocol_data = json.loads(json.dumps(protocol or fixtures.FIXTURE_PROTOCOL))
        state = fixtures.initial_state()
        fixtures.validate_protocol(protocol_data, state)
        initial_total = exact_text(exact_total(state))
        created = now_iso()
        with self._lock, self.conn:
            cursor = self.conn.execute(
                """INSERT INTO runs(root_id, parent_id, branch_no, label, fixture_id,
                   protocol_json, status, step_index, op_index, attempt, faults_json,
                   initial_total, created_at)
                   VALUES (0, NULL, 0, ?, ?, ?, 'ready', 0, 0, 0, ?, ?, ?)""",
                (
                    label,
                    fixtures.FIXTURE_ID,
                    json.dumps(protocol_data, ensure_ascii=False),
                    json.dumps(faults or [], ensure_ascii=False),
                    initial_total,
                    created,
                ),
            )
            run_id = cursor.lastrowid
            self.conn.execute("UPDATE runs SET root_id=? WHERE id=?", (run_id, run_id))
            self._write_checkpoint(run_id, None, 0, 0, False, state)
        return self.get_run(run_id)

    def _row_to_run(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "root_id": row["root_id"],
            "parent_id": row["parent_id"],
            "branch_no": row["branch_no"],
            "label": row["label"],
            "fixture_id": row["fixture_id"],
            "protocol": json.loads(row["protocol_json"]),
            "status": row["status"],
            "cursor": {"step_index": row["step_index"], "op_index": row["op_index"], "attempt": row["attempt"]},
            "faults": json.loads(row["faults_json"]),
            "initial_total": row["initial_total"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }

    def get_run(self, run_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"运行不存在: {run_id}")
        return self._row_to_run(row)

    def list_runs(self, root_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if root_id is None:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY root_id, id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE root_id=? ORDER BY id", (root_id,)
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def latest_run(self) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchall()
        return self._row_to_run(rows[0]) if rows else None

    def _state_at(self, run_id: int) -> LabState:
        row = self.conn.execute(
            """SELECT state_json FROM checkpoints WHERE run_id=?
               ORDER BY id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"运行 {run_id} 缺少检查点")
        return LabState.from_json(json.loads(row["state_json"]))

    def _write_checkpoint(
        self,
        run_id: int,
        event_id: Optional[int],
        step_index: int,
        op_index: int,
        commit_point: bool,
        state: LabState,
    ) -> int:
        cursor = self.conn.execute(
            """INSERT INTO checkpoints(run_id, event_id, step_index, op_index,
               commit_point, clock, state_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                event_id,
                step_index,
                op_index,
                1 if commit_point else 0,
                exact_text(state.clock),
                json.dumps(state.to_json(), ensure_ascii=False),
                now_iso(),
            ),
        )
        return cursor.lastrowid

    def list_checkpoints(self, run_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, event_id, step_index, op_index, attempt, commit_point, clock
               FROM checkpoints WHERE run_id=? ORDER BY id""",
            (run_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "event_id": row["event_id"],
                "step_index": row["step_index"],
                "op_index": row["op_index"],
                "attempt": row["attempt"],
                "commit_point": bool(row["commit_point"]),
                "clock": row["clock"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 单步执行（微操作），重放幂等
    # ------------------------------------------------------------------
    @staticmethod
    def replay_key(step_index: int, op_index: int, attempt: int) -> str:
        return f"s{step_index}o{op_index}a{attempt}"

    def _find_fault(
        self, run: Dict[str, Any], step_id: str, op_name: str, op_index: int
    ) -> Optional[Fault]:
        for item in run["faults"]:
            if item.get("step_id") not in (None, step_id):
                continue
            if item.get("op_name") != op_name:
                continue
            if "op_index" in item and item["op_index"] != op_index:
                continue
            if op_name in fixtures.FAULT_OPS.get(item["fault"], set()):
                return Fault.from_json(item)
        return None

    def step(self, run_id: int, replay_key: Optional[str] = None) -> Dict[str, Any]:
        """执行下一个微操作。

        replay_key 显式给出时必须与游标期望一致（重放旧事件幂等）；
        不传则按游标推进，重复调用不会重复施加物理操作。
        """
        with self._lock:
            # 重放已存在的事件（含恢复事件）必须幂等，先查后判定游标。
            lookup_key = replay_key
            run = self.get_run(run_id)
            if lookup_key is None and run["status"] == "ready":
                lookup_key = self.replay_key(
                    run["cursor"]["step_index"],
                    run["cursor"]["op_index"],
                    run["cursor"]["attempt"],
                )
            if lookup_key is not None:
                existing = self.conn.execute(
                    "SELECT id FROM events WHERE run_id=? AND replay_key=?",
                    (run_id, lookup_key),
                ).fetchone()
                if existing:
                    event_data = self.get_event(existing["id"])
                    event_data["run"] = self.get_run(run_id)
                    return event_data
            if run["status"] in {"failed", "recovered", "done"}:
                raise IllegalTransition(f"运行 {run_id} 状态为 {run['status']}，不能继续")
            protocol = run["protocol"]
            step_index = run["cursor"]["step_index"]
            op_index = run["cursor"]["op_index"]
            attempt = run["cursor"]["attempt"]
            expected = self.replay_key(step_index, op_index, attempt)
            if replay_key is not None and replay_key != expected:
                raise IllegalTransition(
                    f"重放键 {replay_key} 与游标期望 {expected} 不一致"
                )

            if step_index >= len(protocol):
                raise IllegalTransition("协议已执行完")
            step = protocol[step_index]
            ops = fixtures.step_ops(dict(step))
            op_name, payload = ops[op_index]
            fault = self._find_fault(run, step["id"], op_name, op_index)
            state = self._state_at(run_id)
            result = perform_op(state, op_name, payload, fault)
            return self._persist_event(run, result, expected)

    def _persist_event(self, run: Dict[str, Any], result: OpResult, key: str) -> Dict[str, Any]:
        run_id = run["id"]
        protocol = run["protocol"]
        step_index = result.payload["step_index"]
        op_index = result.payload["op_index"]
        attempt = run["cursor"]["attempt"]
        new_state = result.after_state
        step_id = result.payload.get("step_id")
        with self.conn:
            event_cursor = self.conn.execute(
                """INSERT INTO events(run_id, replay_key, seq, step_id, step_index,
                   op_index, attempt, op_name, payload_json, ok, fault, error,
                   delta_clock, before_json, after_json, lineage_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    key,
                    self._next_seq(run_id),
                    step_id,
                    step_index,
                    op_index,
                    attempt,
                    result.op_name,
                    json.dumps(result.payload, ensure_ascii=False),
                    1 if result.ok else 0,
                    result.fault,
                    result.error,
                    result.delta_clock,
                    json.dumps(result.before, ensure_ascii=False),
                    json.dumps(result.after, ensure_ascii=False),
                    json.dumps(result.lineage, ensure_ascii=False)
                    if result.lineage
                    else None,
                    now_iso(),
                ),
            )
            event_id = event_cursor.lastrowid
            commit_point = bool(result.payload.get("compound_commit") and result.ok)
            self._write_checkpoint(
                run_id, event_id, step_index, op_index, commit_point, new_state
            )

            if not result.ok:
                self.conn.execute(
                    "UPDATE runs SET status='failed', attempt=? WHERE id=?",
                    (attempt + 1, run_id),
                )
                event_data = self.get_event(event_id)
                event_data["run"] = self.get_run(run_id)
                return event_data

            ops = fixtures.step_ops(dict(protocol[step_index]))
            if op_index + 1 < len(ops):
                next_op, next_attempt = op_index + 1, attempt
            else:
                next_op, next_attempt = 0, 0
                step_index += 1
            finished = step_index >= len(protocol)
            self.conn.execute(
                "UPDATE runs SET step_index=?, op_index=?, attempt=?, status=?, finished_at=? WHERE id=?",
                (
                    step_index,
                    next_op,
                    next_attempt,
                    "done" if finished else "ready",
                    now_iso() if finished else None,
                    run_id,
                ),
            )
        event_data = self.get_event(event_id)
        event_data["run"] = self.get_run(run_id)
        return event_data

    def _next_seq(self, run_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["seq"])

    # ------------------------------------------------------------------
    # 事件 / 谱系
    # ------------------------------------------------------------------
    def _row_to_event(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "replay_key": row["replay_key"],
            "seq": row["seq"],
            "step_id": row["step_id"],
            "step_index": row["step_index"],
            "op_index": row["op_index"],
            "attempt": row["attempt"],
            "op_name": row["op_name"],
            "op_label": {"move_head": "模块移动", "pick_tip": "拾取吸头",
                         "aspirate": "吸液", "dispense": "排液",
                         "eject_tip": "退出吸头"}.get(row["op_name"], row["op_name"]),
            "payload": json.loads(row["payload_json"]),
            "ok": bool(row["ok"]),
            "fault": row["fault"],
            "error": row["error"],
            "delta_clock": row["delta_clock"],
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
            "lineage": json.loads(row["lineage_json"]) if row["lineage_json"] else None,
            "created_at": row["created_at"],
        }

    def get_event(self, event_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound(f"事件不存在: {event_id}")
        return self._row_to_event(row)

    def list_events(self, run_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def _lineage_events(self, run_id: int) -> List[Dict[str, Any]]:
        """沿恢复父链收集谱系事件（父链成功事件 + 本分支事件）。"""
        chain: List[int] = []
        current = self.get_run(run_id)
        while current is not None:
            chain.append(current["id"])
            current = self.get_run(current["parent_id"]) if current["parent_id"] else None
        events: List[Dict[str, Any]] = []
        for ancestor in reversed(chain):
            for event in self.list_events(ancestor):
                if event["op_name"] == "resume_branch":
                    continue
                events.append(event)
        return events

    def well_lineage(self, run_id: int) -> Dict[str, Any]:
        """聚合每孔成分谱系：以初始装载为基线，叠加成功的吸/排事件。

        恢复分支的谱系沿父链拼接，失败父运行里“已吸未排”的现实持液
        不会被分支重复计算，也不会丢失。
        """
        initial = fixtures.initial_state()
        wells: Dict[str, Dict[str, Fraction]] = {
            ref: dict(well.contents) for ref, well in initial.wells()
        }
        origins: Dict[str, Dict[str, Any]] = {}
        trace: List[Dict[str, Any]] = []
        for event in self._lineage_events(run_id):
            lineage = event["lineage"]
            if not lineage or not event["ok"]:
                continue
            ref = lineage["well"]
            bucket = wells.setdefault(ref, {})
            if lineage["kind"] == "aspirate":
                for component, amount_text in lineage["components"].items():
                    bucket[component] = bucket.get(component, ZERO) - frac(amount_text)
            else:
                for component, amount_text in lineage["components"].items():
                    bucket[component] = bucket.get(component, ZERO) + frac(amount_text)
                origins.setdefault(ref, []).append(
                    {
                        "step_id": event["step_id"],
                        "seq": event["seq"],
                        "clock": event["after"]["clock"],
                        "components": lineage["components"],
                    }
                )
            trace.append(
                {
                    "seq": event["seq"],
                    "step_id": event["step_id"],
                    "kind": lineage["kind"],
                    "well": ref,
                    "amount": lineage["amount"],
                    "components": lineage["components"],
                    "op_name": event["op_name"],
                    "clock": event["after"]["clock"],
                }
            )
        physical = self._state_at(run_id)
        physical_balances = {ref: dict(well.contents) for ref, well in physical.wells()}
        reconciles = True
        for ref in set(wells) | set(physical_balances):
            expected = wells.get(ref, {})
            actual = physical_balances.get(ref, {})
            for component in set(expected) | set(actual):
                if expected.get(component, ZERO) != actual.get(component, ZERO):
                    reconciles = False
        return {
            "run_id": run_id,
            "trace": trace,
            "origins": {ref: entries for ref, entries in sorted(origins.items())},
            "balances": {
                ref: {
                    component: exact_text(amount)
                    for component, amount in sorted(components.items())
                    if amount != ZERO
                }
                for ref, components in sorted(wells.items())
            },
            "reconciles_with_physical": reconciles,
        }

    # ------------------------------------------------------------------
    # 恢复：从最后一个可提交检查点分叉，绝不覆盖失败运行
    # ------------------------------------------------------------------
    def resume(self, run_id: int, label: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id)
            if run["status"] not in {"failed", "recovered"}:
                raise IllegalTransition(f"运行 {run_id} 状态为 {run['status']}，无法恢复")
            existing = self.conn.execute(
                "SELECT * FROM runs WHERE parent_id=? ORDER BY id LIMIT 1", (run_id,)
            ).fetchone()
            if existing is not None:
                # 恢复本身幂等：同一失败运行重复恢复返回同一分支。
                child = self._row_to_run(existing)
                resume_event = self.conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND op_name='resume_branch' ORDER BY id LIMIT 1",
                    (child["id"],),
                ).fetchone()
                data = self.get_run(child["id"])
                data["resume_event"] = self._row_to_event(resume_event) if resume_event else None
                return data

            failed_event = self.conn.execute(
                """SELECT * FROM events WHERE run_id=? AND ok=0 ORDER BY id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            if failed_event is None:
                raise IllegalTransition("失败运行缺少失败事件")
            fail_step = int(failed_event["step_index"])
            fail_op = int(failed_event["op_index"])
            # 该失败步骤之前最近的复合提交点（上一个完整复合转移/混匀结束）。
            commit_row = self.conn.execute(
                """SELECT * FROM checkpoints WHERE run_id=? AND commit_point=1
                   AND step_index < ? ORDER BY id DESC LIMIT 1""",
                (run_id, fail_step),
            ).fetchone()
            aspirate_row = self.conn.execute(
                """SELECT * FROM events WHERE run_id=? AND ok=1 AND step_index=?
                   AND op_name='aspirate' ORDER BY id DESC LIMIT 1""",
                (run_id, fail_step),
            ).fetchone()
            if aspirate_row is not None and int(aspirate_row["op_index"]) < fail_op:
                # 吸液已成功、排液未完成：恢复必须保留吸头中的现实液体，
                # 从失败微操作本身继续（不重新吸液）。
                basis = self.conn.execute(
                    "SELECT * FROM checkpoints WHERE event_id=?",
                    (aspirate_row["id"],),
                ).fetchone()
                basis_kind = "physical_post_aspirate"
                step_index = fail_step
                op_index = fail_op
            elif commit_row is not None:
                basis = commit_row
                basis_kind = "last_compound_commit"
                step_index = fail_step
                op_index = 0
            else:
                # 第一个复合步骤在吸液前失败：回到运行起点检查点。
                basis = self.conn.execute(
                    "SELECT * FROM checkpoints WHERE run_id=? ORDER BY id ASC LIMIT 1", (run_id,)
                ).fetchone()
                basis_kind = "initial"
                step_index = fail_step
                op_index = 0
            branch_no = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(branch_no), 0) + 1 AS b FROM runs WHERE root_id=?",
                    (run["root_id"],),
                ).fetchone()["b"]
            )
            state = LabState.from_json(json.loads(basis["state_json"]))
            created = now_iso()
            branch_label = label or f"resume-of-run-{run_id}"
            with self.conn:
                cursor = self.conn.execute(
                    """INSERT INTO runs(root_id, parent_id, branch_no, label, fixture_id,
                       protocol_json, status, step_index, op_index, attempt, faults_json,
                       initial_total, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, 0, ?, ?, ?)""",
                    (
                        run["root_id"],
                        run_id,
                        branch_no,
                        branch_label,
                        run["fixture_id"],
                        json.dumps(run["protocol"], ensure_ascii=False),
                        step_index,
                        op_index,
                        "[]",
                        run["initial_total"],
                        created,
                    ),
                )
                child_id = cursor.lastrowid
                self._write_checkpoint(child_id, None, step_index, op_index, False, state)
                physical_tip = None
                if state.picked_tip:
                    tip = state.tip(state.picked_tip)
                    physical_tip = {
                        "tip": state.picked_tip,
                        "held_total": exact_text(tip.held_total),
                        "held": {k: exact_text(v) for k, v in sorted(tip.held.items())},
                    }
                resume_payload = {
                    "parent_run_id": run_id,
                    "basis_checkpoint_id": basis["id"],
                    "basis": basis_kind,
                    "resume_at": {"step_index": step_index, "op_index": op_index},
                    "physical_tip": physical_tip,
                    "physical_clock": exact_text(state.clock),
                }
                before_summary = {"clock": exact_text(state.clock), "head": self._head_json(state)}
                event_cursor = self.conn.execute(
                    """INSERT INTO events(run_id, replay_key, seq, step_id, step_index,
                       op_index, attempt, op_name, payload_json, ok, fault, error,
                       delta_clock, before_json, after_json, lineage_json, created_at)
                       VALUES (?, 'resume', 0, NULL, ?, ?, 0, 'resume_branch', ?, 1, NULL,
                       NULL, '0', ?, ?, NULL, ?)""",
                    (
                        child_id,
                        step_index,
                        op_index,
                        json.dumps(resume_payload, ensure_ascii=False),
                        json.dumps(before_summary, ensure_ascii=False),
                        json.dumps(before_summary, ensure_ascii=False),
                        created,
                    ),
                )
                self.conn.execute("UPDATE runs SET status='recovered' WHERE id=?", (run_id,))
                resume_row = self.conn.execute(
                    "SELECT * FROM events WHERE id=?", (event_cursor.lastrowid,)
                ).fetchone()
                data = self.get_run(child_id)
                data["resume_event"] = self._row_to_event(resume_row)
                return data

    @staticmethod
    def _head_json(state: LabState) -> Dict[str, Any]:
        picked = None
        if state.picked_tip:
            tip = state.tip(state.picked_tip)
            picked = {
                "tip": state.picked_tip,
                "state": tip.state,
                "held_total": exact_text(tip.held_total),
            }
        return {"head_slot": state.head_slot, "picked_tip": picked}

    # ------------------------------------------------------------------
    # 故障注入（实时单步）
    # ------------------------------------------------------------------
    def inject_fault(
        self, run_id: int, fault: str, step_id: Optional[str] = None, note: str = ""
    ) -> Dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id)
            if run["status"] not in {"ready"}:
                raise IllegalTransition(f"运行 {run_id} 状态为 {run['status']}，不能注入")
            if fault not in fixtures.FAULT_OPS:
                raise IllegalTransition(f"未知故障类型: {fault}")
            if step_id is None:
                cursor_step = run["protocol"][run["cursor"]["step_index"]]
                step_id = cursor_step["id"]
            step = next((item for item in run["protocol"] if item["id"] == step_id), None)
            if step is None:
                raise IllegalTransition(f"步骤不存在: {step_id}")
            # 根据当前游标微操作选择兼容的注入点；否则选择该故障的默认微操作。
            ops = fixtures.step_ops(dict(step))
            cursor_op = ops[run["cursor"]["op_index"]][0]
            allowed = fixtures.FAULT_OPS[fault]
            op_name = cursor_op if cursor_op in allowed else next(iter(allowed))
            faults = list(run["faults"])
            faults.append(
                {"fault": fault, "op_name": op_name, "step_id": step_id, "note": note}
            )
            self.conn.execute(
                "UPDATE runs SET faults_json=? WHERE id=?",
                (json.dumps(faults, ensure_ascii=False), run_id),
            )
            return self.get_run(run_id)

    # ------------------------------------------------------------------
    # 运行视图 / 守恒核对
    # ------------------------------------------------------------------
    def run_detail(self, run_id: int) -> Dict[str, Any]:
        run = self.get_run(run_id)
        state = self._state_at(run_id)
        initial_state = fixtures.initial_state()
        from .engine import conservation_report

        conservation = conservation_report(initial_state, state)
        held_tips = []
        for rack in state.tips.values():
            for tip in rack:
                if tip.state in {"picked", "empty"}:
                    held_tips.append(
                        {
                            "tip": f"{tip.rack_slot}:{tip.position}",
                            "state": tip.state,
                            "held_total": exact_text(tip.held_total),
                            "held": {k: exact_text(v) for k, v in sorted(tip.held.items())},
                        }
                    )
        pending = run["status"] == "failed" and any(item["held_total"] != "0" for item in held_tips)
        return {
            "run": run,
            "state": state.to_json(),
            "events": self.list_events(run_id),
            "checkpoints": self.list_checkpoints(run_id),
            "lineage": self.well_lineage(run_id),
            "conservation": conservation,
            "held_tips": held_tips,
            "recovery_pending": pending,
        }

    def deck_snapshot(self) -> Dict[str, Any]:
        latest = self.latest_run()
        if latest is None:
            state = fixtures.initial_state()
            return {"state": state.to_json(), "run_id": None}
        return {"state": self._state_at(latest["id"]).to_json(), "run_id": latest["id"]}

    # ------------------------------------------------------------------
    # 导出 / 导入（清空后可重放复核）
    # ------------------------------------------------------------------
    def export_data(self) -> Dict[str, Any]:
        runs = [
            dict(row)
            for row in self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        ]
        events = [
            dict(row)
            for row in self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        ]
        checkpoints = [
            dict(row)
            for row in self.conn.execute("SELECT * FROM checkpoints ORDER BY id").fetchall()
        ]
        return {
            "format": "pipette-testimony/1",
            "fixture_id": fixtures.FIXTURE_ID,
            "exported_at": now_iso(),
            "runs": runs,
            "events": events,
            "checkpoints": checkpoints,
        }

    def verify_export(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        """对导出包做独立重放：逐条事件推进，并与每个检查点核对。"""
        runs = {run["id"]: run for run in bundle.get("runs", [])}
        events_by_run: Dict[int, List[Dict[str, Any]]] = {}
        for event in bundle.get("events", []):
            events_by_run.setdefault(event["run_id"], []).append(event)
        checkpoints_by_run: Dict[int, List[Dict[str, Any]]] = {}
        for checkpoint in bundle.get("checkpoints", []):
            checkpoints_by_run.setdefault(checkpoint["run_id"], []).append(checkpoint)
        mismatch: List[Dict[str, Any]] = []

        def check_point(checkpoint: Dict[str, Any], state: LabState) -> None:
            if checkpoint["clock"] != exact_text(state.clock):
                mismatch.append(
                    {"run_id": checkpoint["run_id"], "checkpoint_id": checkpoint["id"],
                     "reason": "clock", "expected": exact_text(state.clock),
                     "actual": checkpoint["clock"]}
                )
                return
            stored = LabState.from_json(json.loads(checkpoint["state_json"]))
            if stored.to_json() != state.to_json():
                mismatch.append(
                    {"run_id": checkpoint["run_id"], "checkpoint_id": checkpoint["id"],
                     "reason": "state"}
                )

        for run_id in sorted(runs):
            run = runs[run_id]
            events = sorted(events_by_run.get(run_id, []), key=lambda item: item["seq"])
            checkpoints = sorted(checkpoints_by_run.get(run_id, []), key=lambda c: c["id"])
            if run["parent_id"] is not None:
                resume = next((e for e in events if e["op_name"] == "resume_branch"), None)
                if resume is None:
                    mismatch.append({"run_id": run_id, "reason": "missing_resume_event"})
                    continue
                basis_id = json.loads(resume["payload_json"])["basis_checkpoint_id"]
                basis = next(c for c in bundle["checkpoints"] if c["id"] == basis_id)
                state = LabState.from_json(json.loads(basis["state_json"]))
                micro_events = [e for e in events if e["op_name"] != "resume_branch"]
            else:
                state = fixtures.initial_state()
                micro_events = events
            event_by_checkpoint = {
                c["event_id"]: c for c in checkpoints if c["event_id"] is not None
            }
            for checkpoint in checkpoints:
                if checkpoint["event_id"] is None:
                    # 初始 / 恢复分叉基检查点。
                    if run["parent_id"] is None:
                        check_point(checkpoint, fixtures.initial_state())
                    else:
                        check_point(checkpoint, state)
            for event in micro_events:
                payload = json.loads(event["payload_json"])
                fault = (
                    Fault(fault=event["fault"], op_name=event["op_name"])
                    if event["fault"]
                    else None
                )
                state = perform_op(state, event["op_name"], payload, fault).after_state
                if event["id"] in event_by_checkpoint:
                    check_point(event_by_checkpoint[event["id"]], state)
        return {"verified": not mismatch, "mismatches": mismatch, "runs": len(runs)}

    def reset(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                "DELETE FROM checkpoints; DELETE FROM events; DELETE FROM runs;"
            )

    def import_data(self, bundle: Dict[str, Any], clear: bool = True) -> Dict[str, Any]:
        if bundle.get("format") != "pipette-testimony/1":
            raise IllegalTransition("导入格式不被支持")
        if bundle.get("fixture_id") != fixtures.FIXTURE_ID:
            raise IllegalTransition("fixture 不一致，拒绝导入")
        verification = self.verify_export(bundle)
        with self._lock, self.conn:
            if clear:
                self.conn.executescript(
                    "DELETE FROM checkpoints; DELETE FROM events; DELETE FROM runs;"
                )
            for run in bundle["runs"]:
                self.conn.execute(
                    """INSERT INTO runs(id, root_id, parent_id, branch_no, label, fixture_id,
                       protocol_json, status, step_index, op_index, attempt, faults_json,
                       initial_total, created_at, finished_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run["id"], run["root_id"], run["parent_id"], run["branch_no"],
                        run["label"], run["fixture_id"], run["protocol_json"], run["status"],
                        run["step_index"], run["op_index"], run["attempt"], run["faults_json"],
                        run["initial_total"], run["created_at"], run["finished_at"],
                    ),
                )
            for event in bundle["events"]:
                self.conn.execute(
                    """INSERT INTO events(id, run_id, replay_key, seq, step_id, step_index,
                       op_index, attempt, op_name, payload_json, ok, fault, error,
                       delta_clock, before_json, after_json, lineage_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event["id"], event["run_id"], event["replay_key"], event["seq"],
                        event["step_id"], event["step_index"], event["op_index"],
                        event["attempt"], event["op_name"], event["payload_json"],
                        event["ok"], event["fault"], event["error"], event["delta_clock"],
                        event["before_json"], event["after_json"], event["lineage_json"],
                        event["created_at"],
                    ),
                )
            for checkpoint in bundle["checkpoints"]:
                self.conn.execute(
                    """INSERT INTO checkpoints(id, run_id, event_id, step_index, op_index,
                       attempt, commit_point, clock, state_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        checkpoint["id"], checkpoint["run_id"], checkpoint["event_id"],
                        checkpoint["step_index"], checkpoint["op_index"],
                        checkpoint["attempt"], checkpoint["commit_point"], checkpoint["clock"],
                        checkpoint["state_json"], checkpoint["created_at"],
                    ),
                )
        return {"imported_runs": len(bundle["runs"]), "verification": verification}
