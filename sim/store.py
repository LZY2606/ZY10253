"""SQLite 持久层: 运行、事件、检查点三张表。

- events.event_id 全局唯一, INSERT OR IGNORE 保证重放幂等。
- 每个事件记录前后状态摘要 (哈希 + 关键快照)。
- 导出/导入为整张表的字典列表, 清空库后可原样复核。
"""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    checkpoint_id TEXT,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    sim_time INTEGER NOT NULL,
    op_id TEXT,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    pre_json TEXT NOT NULL,
    post_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    op_id TEXT,
    op_index INTEGER NOT NULL,
    sim_time INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def reset(self):
        with self._lock:
            self._conn.executescript(
                "DROP TABLE IF EXISTS runs;"
                "DROP TABLE IF EXISTS events;"
                "DROP TABLE IF EXISTS checkpoints;" + SCHEMA
            )
            self._conn.commit()

    # ---- runs ----
    def insert_run(self, run_id, parent_run_id, checkpoint_id, status, state_json):
        with self._lock:
            seq = self._next_created_seq()
            self._conn.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, parent_run_id, checkpoint_id, status, state_json, seq),
            )
            self._conn.commit()

    def update_run(self, run_id, status, state_json):
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, state_json=? WHERE run_id=?",
                (status, state_json, run_id),
            )
            self._conn.commit()

    def get_run(self, run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_runs(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, parent_run_id, checkpoint_id, status, created_seq"
                " FROM runs ORDER BY created_seq"
            ).fetchall()
            return [dict(r) for r in rows]

    def _next_created_seq(self):
        row = self._conn.execute(
            "SELECT COALESCE(MAX(created_seq),0)+1 AS s FROM runs"
        ).fetchone()
        return row["s"]

    # ---- events ----
    def next_event_seq(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events"
            ).fetchone()
            return row["s"]

    def insert_event(self, evt) -> bool:
        """幂等写入: event_id 已存在时返回 False, 不产生副作用。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    evt["event_id"], evt["run_id"], evt["seq"], evt["sim_time"],
                    evt.get("op_id"), evt["type"], evt["status"],
                    json.dumps(evt.get("detail", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(evt.get("pre", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(evt.get("post", {}), ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_event(self, event_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            return self._event_row(row) if row else None

    def list_events(self, run_id=None):
        with self._lock:
            if run_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY seq"
                ).fetchall()
            return [self._event_row(r) for r in rows]

    @staticmethod
    def _event_row(row):
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        d["pre"] = json.loads(d.pop("pre_json"))
        d["post"] = json.loads(d.pop("post_json"))
        return d

    # ---- checkpoints ----
    def insert_checkpoint(self, checkpoint_id, run_id, op_id, op_index, sim_time, state_json):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO checkpoints VALUES (?,?,?,?,?,?)",
                (checkpoint_id, run_id, op_id, op_index, sim_time, state_json),
            )
            self._conn.commit()

    def latest_checkpoint(self, run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=?"
                " ORDER BY sim_time DESC, op_index DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_checkpoints(self, run_id=None):
        with self._lock:
            if run_id:
                rows = self._conn.execute(
                    "SELECT checkpoint_id, run_id, op_id, op_index, sim_time"
                    " FROM checkpoints WHERE run_id=? ORDER BY op_index", (run_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT checkpoint_id, run_id, op_id, op_index, sim_time"
                    " FROM checkpoints ORDER BY run_id, op_index"
                ).fetchall()
            return [dict(r) for r in rows]

    # ---- export / import ----
    def export_dump(self):
        with self._lock:
            runs = [dict(r) for r in self._conn.execute(
                "SELECT * FROM runs ORDER BY created_seq").fetchall()]
            events = [self._event_row(r) for r in self._conn.execute(
                "SELECT * FROM events ORDER BY seq").fetchall()]
            ckpts = [dict(r) for r in self._conn.execute(
                "SELECT * FROM checkpoints ORDER BY run_id, op_index").fetchall()]
        return {"format": "testimony-dump-v1", "runs": runs,
                "events": events, "checkpoints": ckpts}

    def import_dump(self, dump):
        """清空后原样导入, 供复核。"""
        if dump.get("format") != "testimony-dump-v1":
            raise ValueError("无法识别的导出格式")
        with self._lock:
            self._conn.executescript(
                "DELETE FROM events; DELETE FROM checkpoints; DELETE FROM runs;"
            )
            for r in dump["runs"]:
                self._conn.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                    (r["run_id"], r["parent_run_id"], r["checkpoint_id"],
                     r["status"], r["state_json"], r["created_seq"]),
                )
            for e in dump["events"]:
                self._conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (e["event_id"], e["run_id"], e["seq"], e["sim_time"],
                     e.get("op_id"), e["type"], e["status"],
                     json.dumps(e["detail"], ensure_ascii=False, sort_keys=True),
                     json.dumps(e["pre"], ensure_ascii=False, sort_keys=True),
                     json.dumps(e["post"], ensure_ascii=False, sort_keys=True)),
                )
            for c in dump["checkpoints"]:
                self._conn.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
                    (c["checkpoint_id"], c["run_id"], c["op_id"],
                     c["op_index"], c["sim_time"], c["state_json"]),
                )
            self._conn.commit()

    def has_runs(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
            return row["n"] > 0
