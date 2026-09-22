"""移液执行证言台 — FastAPI 服务入口。

运行: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5593
"""
from __future__ import annotations

import os
from fractions import Fraction

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sim import deck as deck_view
from sim.engine import Engine, FAULT_LABELS, expand_op
from sim.state import fmt_frac, state_from_json, volume

DB_PATH = os.environ.get("TESTIMONY_DB", "testimony.db")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

engine = Engine(DB_PATH)
engine.ensure_initialized()

app = FastAPI(title="移液执行证言台")


class StepRequest(BaseModel):
    run_id: str


class FaultRequest(BaseModel):
    run_id: str
    kind: str


class RecoverRequest(BaseModel):
    run_id: str


def build_view(run_id: str | None = None) -> dict:
    runs = engine.store.list_runs()
    if not runs:
        return {"runs": [], "run": None}
    if not run_id:
        run_id = runs[-1]["run_id"]
    row = engine.store.get_run(run_id)
    if not row:
        raise HTTPException(404, f"运行不存在: {run_id}")
    st = state_from_json(row["state_json"])
    wells = {}
    for lw, plate in st["wells"].items():
        wells[lw] = {}
        for wkey, w in plate.items():
            wells[lw][wkey] = {
                "physical": {c: fmt_frac(a) for c, a in w["physical"].items() if a},
                "committed": {c: fmt_frac(a) for c, a in w["committed"].items() if a},
                "volume": fmt_frac(volume(w["physical"])),
                "capacity": fmt_frac(w["capacity"]),
                "lineage": w["lineage"],
            }
    tip = st["pipette"]["tip"]
    events = engine.store.list_events(run_id)
    summary = None
    from sim.engine import summarize
    summary = summarize(st)
    return {
        "runs": runs,
        "run": {
            "run_id": run_id,
            "status": row["status"],
            "parent_run_id": row["parent_run_id"],
            "checkpoint_id": row["checkpoint_id"],
            "sim_time": st["sim_time"],
            "op_index": st["op_index"],
            "step_index": st["step_index"],
            "script_len": len(st["script"]),
            "active": st["active"],
            "pending_faults": st["pending_faults"],
            "summary": summary,
        },
        "deck": deck_view.deck_occupancy(st),
        "labware": st["labware"],
        "wells": wells,
        "plates": {lw: deck_view.plate_volume_matrix(st, lw)
                   for lw in st["wells"]},
        "tips": st["tips"],
        "pipette": None if not tip else {
            "tip_id": tip["tip_id"],
            "volume": fmt_frac(volume(tip["contents"])),
            "contents": {c: fmt_frac(a) for c, a in tip["contents"].items() if a},
        },
        "trash": [{"tip_id": t["tip_id"],
                   "contents": {c: fmt_frac(a) for c, a in t["contents"].items()}}
                  for t in st["trash"]],
        "script": st["script"],
        "timeline": events,
        "checkpoints": engine.store.list_checkpoints(run_id),
        "fault_labels": FAULT_LABELS,
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/state")
def api_state(run_id: str | None = None):
    return build_view(run_id)


@app.post("/api/step")
def api_step(req: StepRequest):
    return engine.step(req.run_id)


@app.post("/api/run_all")
def api_run_all(req: StepRequest):
    return engine.run_all(req.run_id)


@app.post("/api/inject")
def api_inject(req: FaultRequest):
    try:
        return engine.inject_fault(req.run_id, req.kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/recover")
def api_recover(req: RecoverRequest):
    return engine.recover(req.run_id)


@app.post("/api/reset")
def api_reset():
    run_id = engine.reset()
    return {"ok": True, "run_id": run_id}


@app.get("/api/export")
def api_export():
    return engine.export_dump()


@app.post("/api/import")
def api_import(dump: dict):
    try:
        return engine.import_dump(dump)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
