"""FastAPI 本地服务：移液执行证言台。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from lab import fixtures
from lab.store import IllegalTransition, NotFound, Store

DB_PATH = Path("data/testimony.db")

app = FastAPI(title="移液执行证言台", version="1.0.0")
app.mount("/static", StaticFiles(directory="web/static"), name="static")
store = Store(DB_PATH)


class FaultIn(BaseModel):
    fault: str = Field(pattern="^(tip_not_ready|level_fail|motion_conflict)$")
    step_id: Optional[str] = None
    note: str = ""


class CreateRunIn(BaseModel):
    label: str = "primary"
    faults: List[Dict[str, Any]] = Field(default_factory=list)


class ResumeIn(BaseModel):
    label: Optional[str] = None


class StepIn(BaseModel):
    replay_key: Optional[str] = None


class ImportIn(BaseModel):
    bundle: Dict[str, Any]
    clear: bool = True


@app.exception_handler(NotFound)
def not_found_handler(_request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(IllegalTransition)
def illegal_handler(_request, exc: IllegalTransition):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse("web/index.html")


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "title": "移液执行证言台"}


@app.get("/api/fixture")
def fixture() -> Dict[str, Any]:
    state = fixtures.initial_state()
    protocol = json.loads(json.dumps(fixtures.FIXTURE_PROTOCOL))
    fixtures.validate_protocol(protocol, state)
    expanded = []
    for step in protocol:
        expanded.append(
            {
                "id": step["id"],
                "kind": step["kind"],
                "tip": step["tip"],
                "target": step.get("dst") or step.get("well"),
                "src": step.get("src"),
                "volume": step["volume"],
                "ops": [name for name, _ in fixtures.step_ops(dict(step))],
            }
        )
    return {
        "fixture_id": fixtures.FIXTURE_ID,
        "deck_order": fixtures.DECK_SLOT_ORDER,
        "state": state.to_json(),
        "protocol": expanded,
    }


@app.get("/api/runs")
def runs() -> Dict[str, Any]:
    return {"runs": store.list_runs()}


@app.post("/api/runs")
def create_run(body: CreateRunIn) -> Dict[str, Any]:
    return store.create_run(faults=body.faults, label=body.label)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> Dict[str, Any]:
    return store.run_detail(run_id)


@app.post("/api/runs/{run_id}/step")
def step_run(run_id: int, body: StepIn) -> Dict[str, Any]:
    return store.step(run_id, body.replay_key)


@app.post("/api/runs/{run_id}/inject")
def inject(run_id: int, body: FaultIn) -> Dict[str, Any]:
    try:
        return store.inject_fault(run_id, body.fault, body.step_id, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/resume")
def resume(run_id: int, body: ResumeIn) -> Dict[str, Any]:
    return store.resume(run_id, body.label)


@app.get("/api/runs/{run_id}/lineage")
def lineage(run_id: int) -> Dict[str, Any]:
    return store.well_lineage(run_id)


@app.get("/api/export")
def export_data() -> Dict[str, Any]:
    return store.export_data()


@app.post("/api/import")
def import_data(body: ImportIn) -> Dict[str, Any]:
    try:
        return store.import_data(body.bundle, clear=body.clear)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/reset")
def reset() -> Dict[str, Any]:
    store.reset()
    return {"ok": True}
