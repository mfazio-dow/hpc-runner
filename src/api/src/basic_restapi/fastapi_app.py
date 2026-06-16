"""FastAPI REST API for HPC Regression Platform."""

import json
import os
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from harness import (
    ConfigError,
    load_all,
    build_jobs_from_solver_specs,
    run_jobs,
    init_db,
    store_run,
    get_runs,
    get_run_by_id,
    delete_runs,
    get_solver_run_summaries,
    get_metrics_history,
    get_all_metrics_series,
    get_baseline_run,
    set_baseline_run,
    get_baseline_comparison,
    get_config_dir,
    get_db_path,
    get_job_batch_uuids,
    list_matrix_presets,
    get_matrix_preset,
    upsert_matrix_preset,
    delete_matrix_preset,
)

from . import invocations
from .slurm_tools import query_slurm_job_state

app = FastAPI(title="HPC Regression API", version="0.1.0")
logger = structlog.get_logger()

# Allow the Streamlit UI (different port) to poll invocation JSON from the browser (live log iframe).
_cors_origins = [
    o.strip()
    for o in os.environ.get(
        "HPC_CORS_ORIGINS",
        "http://localhost:8501,http://127.0.0.1:8501,http://localhost:8502,http://127.0.0.1:8502",
    ).split(",")
    if o.strip()
]
# Iframe live-log fetch() from Streamlit on another host (e.g. LAN IP) needs a matching origin.
_cors_origin_regex = os.environ.get(
    "HPC_CORS_ORIGIN_REGEX",
    r"^http://[\w\.\-]+:(8501|8502)$",
).strip() or None
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_DIR = get_config_dir()
DB_PATH = get_db_path()


def _normalize_run_row(r: dict) -> None:
    """Decode JSON-ish columns for API responses (mutates dict in place)."""
    if r.get("metrics_json"):
        try:
            r["metrics"] = json.loads(r["metrics_json"])
        except Exception:
            r["metrics"] = {}
    else:
        r["metrics"] = {}
    if r.get("validation_errors") is not None:
        try:
            r["validation_errors"] = json.loads(r["validation_errors"])
        except Exception:
            r["validation_errors"] = []
    else:
        r["validation_errors"] = []
    r["passed"] = bool(r.get("passed"))
    r["is_baseline"] = bool(r.get("is_baseline", False))
    sj = r.get("scheduler_job_ids")
    if isinstance(sj, str):
        try:
            r["scheduler_job_ids"] = json.loads(sj or "[]")
        except Exception:
            r["scheduler_job_ids"] = []
    elif sj is None:
        r["scheduler_job_ids"] = []
    r["scheduler_backend"] = r.get("scheduler_backend") or ""
    r["submit_container"] = r.get("submit_container") or ""


def _load_definitions():
    return load_all(CONFIG_DIR, None)


@app.exception_handler(ConfigError)
def config_error_handler(request, exc: ConfigError):
    return JSONResponse(
        status_code=500,
        content={"error": "Config load failed", "detail": str(exc)},
    )


class SolverSpecBody(BaseModel):
    name: str
    system: str | None = None


class RunSolversRequest(BaseModel):
    """Body for POST /api/run_solvers.

    `session_label` and `batch_name` both set the optional session string stored on runs.
    If both are non-empty, `session_label` wins. Clients may send either field; `batch_name`
    remains for backward compatibility.
    """

    solvers: list[SolverSpecBody]
    batch_name: str = ""
    session_label: str = ""
    background: bool = False


def _effective_session_label(body: RunSolversRequest) -> str:
    """Resolve label for run_jobs / invocations: prefer session_label when both are set."""
    sl = (body.session_label or "").strip()
    bn = (body.batch_name or "").strip()
    if sl and bn:
        return sl
    return sl or bn


class DeleteRunsRequest(BaseModel):
    ids: list[int]


class MatrixPresetCell(BaseModel):
    name: str
    system: str


class MatrixPresetPut(BaseModel):
    """Body for PUT /api/matrix_presets/{label}."""

    cells: list[MatrixPresetCell]


@app.get("/")
def root():
    """Redirect to interactive API docs."""
    return RedirectResponse(url="/docs", status_code=302)


@app.get("/api/health")
@app.get("/health")
def api_health():
    """Health check for Docker/orchestration. Returns 200 when API is ready."""
    return {"status": "ok"}


@app.get("/api/solvers")
def api_solvers():
    """List configured solvers."""
    _, _, solvers = _load_definitions()
    return [
        {
            "name": s.name,
            "version": s.version,
            "allowed_systems": s.allowed_systems,
            "default_system": s.default_system,
            "has_parser": s.parser_config is not None,
        }
        for s in solvers.values()
    ]

@app.get("/api/systems")
def api_systems():
    """List configured systems."""
    _, systems, _ = _load_definitions()
    return [
        {
            "name": s.name,
            "resources": s.resources,
            "env": s.env,
            "constraints": s.constraints,
            "extra": s.extra,
        }
        for s in systems.values()
    ]

@app.post("/api/run_solvers")
def api_run_solvers(body: RunSolversRequest | None = None):
    """Run one or more solvers (solver-first). Each background solver gets its own invocation (cancel per solver)."""
    resources, systems, solvers = _load_definitions()
    if not body or not body.solvers:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No solvers in request",
                "available_solvers": sorted(solvers.keys()),
                "results": [],
            },
        )
    try:
        specs = [{"name": s.name, "system": s.system} for s in body.solvers]
        job_list = build_jobs_from_solver_specs(solvers, systems, specs)
    except ConfigError as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e), "results": []},
        )

    effective_label = _effective_session_label(body)
    if body.background:
        inv_rows: list[dict] = []
        for j in job_list:
            sub_batch = f"{effective_label}:{j.solver}" if effective_label.strip() else j.solver
            inv_id = invocations.start_background_run(
                [j],
                solvers,
                systems,
                resources,
                sub_batch,
                str(DB_PATH),
                solver_name=j.solver,
                job_names=[j.name],
            )
            inv_rows.append(
                {
                    "solver_name": j.solver,
                    "invocation_id": inv_id,
                    "run_labels": [j.name],
                }
            )
        content: dict = {
            "status": "queued",
            "invocations": inv_rows,
        }
        if len(inv_rows) == 1:
            content["invocation_id"] = inv_rows[0]["invocation_id"]
        return JSONResponse(status_code=202, content=content)

    results = run_jobs(job_list, solvers, systems, resources=resources, batch_name=effective_label)
    init_db(DB_PATH)
    for r in results:
        store_run(DB_PATH, r)

    response: list[dict] = []
    for r in results:
        item = {
            "job_name": r.job_name,
            "solver_name": r.solver_name,
            "system_name": r.system_name,
            "returncode": r.returncode,
            "passed": r.passed,
            "runtime_seconds": r.runtime_seconds,
            "timestamp": r.timestamp,
            "metrics": r.metrics,
            "processor": r.processor,
            "scheduler_backend": getattr(r, "scheduler_backend", "") or "",
            "scheduler_job_ids": getattr(r, "scheduler_job_ids", None) or [],
            "submit_container": getattr(r, "submit_container", "") or "",
        }
        if hasattr(r, "validation_errors"):
            item["validation_errors"] = r.validation_errors
        item["stdout"] = r.stdout or ""
        item["stderr"] = r.stderr or ""
        response.append(item)
    return response


@app.get("/api/runs")
def api_runs(
    solver: str | None = None,
    processor: str | None = None,
    system: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """List recent runs, optionally filtered by solver, processor, or system (system_name)."""
    limit = min(limit, 500)
    init_db(DB_PATH)
    runs = get_runs(
        DB_PATH,
        solver=solver,
        processor=processor,
        system=system,
        limit=limit,
        offset=offset,
    )
    for r in runs:
        _normalize_run_row(r)
    return runs


@app.delete("/api/runs")
def api_delete_runs(body: DeleteRunsRequest):
    """Delete stored runs by id. May remove a baseline row (solver then has no baseline)."""
    if not body.ids:
        raise HTTPException(status_code=422, detail="ids must be non-empty")
    init_db(DB_PATH)
    n = delete_runs(DB_PATH, body.ids)
    if n == 0:
        raise HTTPException(status_code=404, detail="No matching run ids")
    return {"deleted": n}


@app.get("/api/matrix_presets")
def api_list_matrix_presets():
    """List saved Run Matrix selections (label, cells, updated_at)."""
    init_db(DB_PATH)
    return list_matrix_presets(DB_PATH)


@app.get("/api/matrix_presets/{label}")
def api_get_matrix_preset(label: str):
    """Get one Run Matrix preset by session label (normalized case-insensitively)."""
    init_db(DB_PATH)
    row = get_matrix_preset(DB_PATH, label)
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    return row


@app.put("/api/matrix_presets/{label}")
def api_put_matrix_preset(label: str, body: MatrixPresetPut):
    """Create or replace a Run Matrix preset."""
    init_db(DB_PATH)
    cells = [c.model_dump() for c in body.cells]
    try:
        upsert_matrix_preset(DB_PATH, label, cells)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    row = get_matrix_preset(DB_PATH, label)
    if not row:
        raise HTTPException(status_code=500, detail="Failed to read preset after save")
    return row


@app.delete("/api/matrix_presets/{label}")
def api_delete_matrix_preset(label: str):
    """Delete a saved Run Matrix preset."""
    init_db(DB_PATH)
    n = delete_matrix_preset(DB_PATH, label)
    if n == 0:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": n}


@app.get("/api/runs/{run_id}")
def api_run_detail(run_id: int):
    """Get a single run by id."""
    init_db(DB_PATH)
    run = get_run_by_id(DB_PATH, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run = dict(run)
    _normalize_run_row(run)
    return run


@app.get("/api/runs/{run_id}/slurm_status")
def api_run_slurm_status(run_id: int):
    """Live squeue/sacct for SLURM job ids on this run (requires RUN_SLURM_E2E + docker/host tools)."""
    init_db(DB_PATH)
    run = get_run_by_id(DB_PATH, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        jids = json.loads(run.get("scheduler_job_ids") or "[]")
    except json.JSONDecodeError:
        jids = []
    if not jids:
        return {"enabled": True, "job_ids": [], "output": "", "message": "No SLURM job ids recorded for this run"}
    cont = (run.get("submit_container") or "").strip() or None
    return query_slurm_job_state([str(j) for j in jids], cont)


@app.get("/api/invocations")
def api_list_invocations(active_only: bool = False):
    """List background solver invocations (optionally only queued/running)."""
    return invocations.list_invocations(active_only=active_only)


@app.get("/api/invocations/{invocation_id}/slurm_status")
def api_invocation_slurm_status(invocation_id: str):
    """Live squeue/sacct for SLURM job ids observed on this invocation (while running or after)."""
    body = invocations.get_invocation_slurm_status(invocation_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return body


@app.get("/api/invocations/{invocation_id}")
def api_get_invocation(invocation_id: str):
    """Status / results for a background solver invocation."""
    rec = invocations.get_invocation(invocation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return invocations.invocation_to_dict(rec)


@app.get("/api/invocations/{invocation_id}/execution_status")
def api_invocation_execution_status(invocation_id: str):
    """Unified monitoring: invocation + scheduler_detail (squeue/sacct) when SLURM ids exist."""
    body = invocations.get_invocation_execution_status(invocation_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return body


@app.post("/api/invocations/{invocation_id}/cancel")
def api_cancel_invocation(invocation_id: str):
    """Cancel background run: scancel (if known SLURM ids) + terminate local subprocess."""
    ok, code, notes = invocations.cancel_invocation(invocation_id)
    if not ok and code == "not_found":
        raise HTTPException(status_code=404, detail="Invocation not found")
    if not ok:
        return JSONResponse(status_code=400, content={"ok": False, "detail": code, "scancel_notes": notes})
    return {"ok": True, "scancel_notes": notes}


@app.get("/api/solver_summaries")
def api_solver_summaries():
    """Per-solver aggregate stats from stored runs (monitoring)."""
    init_db(DB_PATH)
    return get_solver_run_summaries(DB_PATH)


@app.get("/api/baseline_comparison")
def api_baseline_comparison(solver: str | None = None, limit: int = 50):
    """
    Compare all other runs to the baseline run per solver.
    Returns for each solver: baseline_run, other runs, and per-metric deltas (vs_baseline).
    """
    limit = min(limit, 200)
    init_db(DB_PATH)
    return get_baseline_comparison(DB_PATH, solver_name=solver, limit_per_solver=limit)


@app.get("/api/solvers/{solver_name}/baseline")
def api_solver_baseline(solver_name: str):
    """Return the current baseline run for the given solver, or 404."""
    init_db(DB_PATH)
    run = get_baseline_run(DB_PATH, solver_name)
    if not run:
        raise HTTPException(status_code=404, detail=f"No baseline run for solver '{solver_name}'")
    return run


@app.post("/api/runs/{run_id}/set_baseline")
def api_set_baseline(run_id: int):
    """
    Set a specific run as the baseline for its solver.
    Other runs of the same solver are no longer baseline. Returns the updated run.
    """
    init_db(DB_PATH)
    run = set_baseline_run(DB_PATH, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@app.get("/api/metrics/{solver_name}/{metric_name}")
def api_metrics_history(
    solver_name: str,
    metric_name: str,
    limit: int = 100,
):
    """Get metric history for trend visualization."""
    limit = min(limit, 500)
    init_db(DB_PATH)
    history: list[tuple[str, float]] = get_metrics_history(DB_PATH, solver_name, metric_name, limit=limit)
    return [{"timestamp": ts, "value": v} for ts, v in history]

@app.get("/api/available_metrics")
def api_available_metrics(
    limit: int = 100,
):
    """Get a list of all metrics/solver combos. by default limits the output to 100"""
    limit = min(limit, 500)
    init_db(DB_PATH)
    available_metrics: list[tuple[str, str]] = get_all_metrics_series(DB_PATH)
    return [{"solver": s, "metric": m} for s, m in available_metrics]

@app.get("/api/get_job_batch_uuids")
def api_job_batch_uuids(limit: int = 100):
    """
    Gets a list of
    """
    init_db(DB_PATH)
    return get_job_batch_uuids(DB_PATH, limit=limit)

def main():
    import uvicorn
    uvicorn.run(
        "basic_restapi.fastapi_app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
