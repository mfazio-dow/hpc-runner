"""Background job invocations (async run_jobs + cancel)."""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import structlog

from harness import (
    InvocationControl,
    run_jobs,
    store_run,
)
from harness.config import Job, Resource, Solver, System

from .slurm_tools import query_slurm_job_state

logger = structlog.get_logger()

REGISTRY: dict[str, "InvocationRecord"] = {}
LOCK = threading.Lock()

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=32, thread_name_prefix="inv-worker"
            )
        return _executor


def shutdown_executor() -> None:
    global _executor
    with _executor_lock:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=True, cancel_futures=False)


def _local_snapshot(ctl: InvocationControl) -> dict[str, Any] | None:
    proc = ctl.current_proc
    if proc is None:
        return None
    alive = proc.poll() is None
    return {"pid": proc.pid, "alive": alive}


def _execution_backend(ctl: InvocationControl) -> str:
    if ctl.slurm_job_ids:
        return "slurm"
    return "local"


@dataclass
class InvocationRecord:
    id: str
    status: str
    batch_name: str = ""
    solver_name: str = ""
    job_names: list[str] = field(default_factory=list)
    # Sorted unique job.system values (for UI filtering while queued/running).
    system_names: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] | None = None
    error: str | None = None
    control: InvocationControl = field(default_factory=InvocationControl)


def _scancel_allowed() -> bool:
    if os.environ.get("HARNESS_ALLOW_SCANCEL") == "1":
        return True
    if os.environ.get("RUN_SLURM_E2E") == "1":
        return True
    cont = (
        os.environ.get("DOCKER_SLURM_SUBMIT_CONTAINER", "").strip()
        or os.environ.get("DOCKER_SLURM_CONTAINER", "").strip()
    )
    return bool(cont)


def try_scancel(job_ids: list[str], submit_container: str | None) -> list[str]:
    """Best-effort scancel via docker exec (or host if submit_container is host)."""
    notes: list[str] = []
    if not _scancel_allowed():
        return [
            "scancel skipped: set HARNESS_ALLOW_SCANCEL=1, RUN_SLURM_E2E=1, "
            "or DOCKER_SLURM_CONTAINER / DOCKER_SLURM_SUBMIT_CONTAINER"
        ]
    if not job_ids:
        return []
    cont = (
        (submit_container or "").strip()
        or os.environ.get("DOCKER_SLURM_SUBMIT_CONTAINER")
        or os.environ.get("DOCKER_SLURM_CONTAINER", "")
    )
    for jid in job_ids:
        if cont == "host":
            r = subprocess.run(
                ["bash", "-lc", f"scancel {jid}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        elif cont:
            r = subprocess.run(
                ["docker", "exec", cont, "bash", "-lc", f"scancel {jid}"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            notes.append("No container / host hint for scancel; subprocess cancel only")
            continue
        if r.returncode != 0 and (r.stderr or r.stdout):
            notes.append(f"scancel {jid}: {(r.stderr or r.stdout).strip()}")
    return notes


def _results_to_json(results: list[Any]) -> list[dict[str, Any]]:
    """Mirror api_run_jobs response shape."""
    out: list[dict[str, Any]] = []
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
            "validation_errors": getattr(r, "validation_errors", []),
            "stdout": r.stdout or "",
            "stderr": r.stderr or "",
            "scheduler_backend": getattr(r, "scheduler_backend", "") or "",
            "scheduler_job_ids": getattr(r, "scheduler_job_ids", None) or [],
            "submit_container": getattr(r, "submit_container", "") or "",
        }
        out.append(item)
    return out


def invocation_to_dict(rec: InvocationRecord) -> dict[str, Any]:
    """JSON-serializable view (includes live control snapshot while running)."""
    ctl = rec.control
    labels = list(rec.job_names)
    bn = rec.batch_name or ""
    return {
        "invocation_id": rec.id,
        "status": rec.status,
        "batch_name": bn,
        "session_label": bn,
        "solver_name": rec.solver_name or "",
        "system_names": list(rec.system_names),
        "job_names": labels,
        "run_labels": labels,
        "results": rec.results,
        "error": rec.error,
        "scheduler_job_ids": list(ctl.slurm_job_ids),
        "submit_container": (ctl.submit_container or "") or "",
        "jobs_total": ctl.jobs_total,
        "jobs_completed": ctl.jobs_completed,
        "execution": {
            "backend": _execution_backend(ctl),
            "local": _local_snapshot(ctl),
            "scheduler_job_ids": list(ctl.slurm_job_ids),
        },
        "live_stdout": ctl.snapshot_live_stdout(),
    }


def list_invocations(*, active_only: bool = False) -> list[dict[str, Any]]:
    with LOCK:
        rows = [invocation_to_dict(r) for r in REGISTRY.values()]
    if active_only:
        rows = [r for r in rows if r.get("status") in ("queued", "running")]
    rows.sort(key=lambda r: (r.get("solver_name") or "", r["invocation_id"]))
    return rows


def start_background_run(
    job_list: list[Job],
    solvers: dict[str, Solver],
    systems: dict[str, System],
    resources: dict[str, Resource],
    batch_name: str,
    *,
    solver_name: str = "",
    job_names: list[str] | None = None,
) -> str:
    inv_id = uuid.uuid4().hex
    jnames = list(job_names) if job_names is not None else [j.name for j in job_list]
    sys_names = sorted({j.system for j in job_list})
    rec = InvocationRecord(
        id=inv_id,
        status="queued",
        batch_name=batch_name or "",
        solver_name=solver_name or "",
        job_names=jnames,
        system_names=sys_names,
    )
    with LOCK:
        REGISTRY[inv_id] = rec

    def worker() -> None:
        with LOCK:
            r0 = REGISTRY.get(inv_id)
        if not r0:
            return
        r0.status = "running"
        ctl = r0.control
        try:
            results = run_jobs(
                job_list,
                solvers,
                systems,
                resources=resources,
                batch_name=batch_name,
                invoke_ctl=ctl,
            )
            for res in results:
                store_run(res)
            r0.results = _results_to_json(results)
            # Only mark cancelled if a job was stopped mid-run (runner sets this on RunResult).
            cancelled = any(
                "Cancelled by user" in (getattr(res, "validation_errors", None) or [])
                for res in results
            )
            r0.status = "cancelled" if cancelled else "completed"
        except Exception as e:
            logger.exception("invocation.failed", invocation_id=inv_id, error=str(e))
            r0.status = "failed"
            r0.error = str(e)
        finally:
            r0.control.clear_live_stdout()

    _get_executor().submit(worker)
    return inv_id


def get_invocation(invocation_id: str) -> InvocationRecord | None:
    with LOCK:
        return REGISTRY.get(invocation_id)


def get_invocation_slurm_status(invocation_id: str) -> dict[str, Any] | None:
    rec = get_invocation(invocation_id)
    if not rec:
        return None
    ctl = rec.control
    return query_slurm_job_state(list(ctl.slurm_job_ids), ctl.submit_container)


def get_invocation_execution_status(invocation_id: str) -> dict[str, Any] | None:
    """Unified monitor payload: invocation snapshot + live scheduler query when SLURM ids exist."""
    rec = get_invocation(invocation_id)
    if not rec:
        return None
    ctl = rec.control
    out: dict[str, Any] = dict(invocation_to_dict(rec))
    out["scheduler_detail"] = {}
    if ctl.slurm_job_ids:
        out["scheduler_detail"] = (
            query_slurm_job_state(list(ctl.slurm_job_ids), ctl.submit_container) or {}
        )
    return out


def cancel_invocation(invocation_id: str) -> tuple[bool, str, list[str]]:
    with LOCK:
        rec = REGISTRY.get(invocation_id)
    if not rec:
        return False, "not_found", []
    if rec.status not in ("queued", "running"):
        return False, f"not_active:{rec.status}", []
    ctl = rec.control
    ctl.cancel_event.set()
    notes = try_scancel(ctl.slurm_job_ids, ctl.submit_container)
    if ctl.current_proc is not None:
        try:
            ctl.current_proc.terminate()
        except Exception as e:
            logger.warning("terminate_failed", err=str(e))
    return True, "ok", notes
