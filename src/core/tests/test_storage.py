# tests/test_storage.py - SQLite storage

import json

import pytest
from harness import RunResult
from harness.storage import (
    init_db,
    start_writer,
    stop_writer,
    db_writer_session,
    store_run,
    get_runs,
    get_run_by_id,
    delete_runs,
    get_solver_run_summaries,
    get_all_metrics_series,
    get_metrics_history,
    get_baseline_run,
    set_baseline_run,
    get_job_batch_uuids,
    get_baseline_comparison,
)


@pytest.fixture(autouse=True)
def _db_writer(tmp_path):
    """Start the DB writer thread before each test, stop it after."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    with db_writer_session(db_path):
        yield db_path


def _make_result(
    job_name="t1",
    solver_name="s1",
    system_name="dev",
    metrics=None,
    processor="x86_64",
    validation_errors=None,
    baseline=False,
    passed=True,
    returncode=0,
    timestamp=None,
    job_batch_uuid="",
):
    return RunResult(
        job_name=job_name,
        solver_name=solver_name,
        system_name=system_name,
        returncode=returncode,
        stdout="out",
        stderr="err",
        runtime_seconds=1.5,
        timestamp=timestamp if timestamp is not None else "2026-02-14T12:00:00+00:00",
        validation_errors=validation_errors if validation_errors is not None else [],
        passed=passed,
        metrics=metrics or {},
        processor=processor,
        baseline=baseline,
        job_batch_uuid=job_batch_uuid,
    )


def test_write_functions_do_not_require_db_path():
    """Write functions route through the DBWriter singleton and need no db_path argument."""
    result = _make_result(job_name="no-path-test", solver_name="s1")
    row_id = store_run(result)
    assert row_id > 0
    deleted = delete_runs([row_id])
    assert deleted == 1


def test_init_db_and_store_run(tmp_path):
    """Initialize DB and store a run."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    result = _make_result(metrics={"mlups": 1.5e6})
    row_id = store_run(result)
    assert row_id > 0


def test_get_runs(tmp_path):
    """Retrieve runs with optional solver filter."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(job_name="t1", solver_name="solver-a"))
    store_run(_make_result(job_name="t2", solver_name="solver-a"))
    store_run(_make_result(job_name="t3", solver_name="solver-b"))

    runs = get_runs(db_path)
    assert len(runs) == 3

    runs_a = get_runs(db_path, solver="solver-a")
    assert len(runs_a) == 2
    assert all(r["solver_name"] == "solver-a" for r in runs_a)


def test_get_runs_filter_by_processor(tmp_path):
    """Retrieve runs filtered by processor."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(job_name="t1", solver_name="s1", processor="x86_64"))
    store_run(_make_result(job_name="t2", solver_name="s1", processor="x86_64"))
    store_run(_make_result(job_name="t3", solver_name="s1", processor="aarch64"))

    runs_x86 = get_runs(db_path, processor="x86_64")
    assert len(runs_x86) == 2
    assert all(r["processor"] == "x86_64" for r in runs_x86)

    runs_arm = get_runs(db_path, processor="aarch64")
    assert len(runs_arm) == 1
    assert runs_arm[0]["processor"] == "aarch64"


def test_get_runs_filter_by_system(tmp_path):
    """Retrieve runs filtered by system_name."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(job_name="t1", system_name="sys-a"))
    store_run(_make_result(job_name="t2", system_name="sys-a"))
    store_run(_make_result(job_name="t3", system_name="sys-b"))

    runs_a = get_runs(db_path, system="sys-a")
    assert len(runs_a) == 2
    assert all(r["system_name"] == "sys-a" for r in runs_a)

    runs_b = get_runs(db_path, system="sys-b")
    assert len(runs_b) == 1
    assert runs_b[0]["system_name"] == "sys-b"


def test_delete_runs(tmp_path):
    """delete_runs removes rows; deleting baseline is allowed."""
    db_path = tmp_path / "test.db"
    id1 = store_run(_make_result(job_name="a", solver_name="s1", baseline=True))
    id2 = store_run(_make_result(job_name="b", solver_name="s1"))
    assert delete_runs([id1]) == 1
    assert get_run_by_id(db_path, id1) is None
    assert delete_runs([id2, 99999]) == 1
    assert get_runs(db_path) == []


def test_get_solver_run_summaries(tmp_path):
    """Per-solver aggregates from runs."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(job_name="j1", solver_name="s1", passed=True))
    store_run(_make_result(job_name="j2", solver_name="s1", passed=False, returncode=1))
    summ = get_solver_run_summaries(db_path)
    assert len(summ) == 1
    assert summ[0]["solver_name"] == "s1"
    assert summ[0]["total_runs"] == 2
    assert summ[0]["pass_count"] == 1


def test_get_job_batch_uuids_orders_by_max_timestamp(tmp_path):
    """Batch UUID list is ordered by most recent run in each batch."""
    db_path = tmp_path / "test.db"
    store_run(
        _make_result(
            job_name="a1",
            job_batch_uuid="batch-a",
            timestamp="2026-01-01T00:00:00+00:00",
        )
    )
    store_run(
        _make_result(
            job_name="b1",
            job_batch_uuid="batch-b",
            timestamp="2026-02-01T00:00:00+00:00",
        )
    )
    store_run(
        _make_result(
            job_name="a2",
            job_batch_uuid="batch-a",
            timestamp="2026-03-01T00:00:00+00:00",
        )
    )
    uuids = get_job_batch_uuids(db_path)
    assert uuids == ["batch-a", "batch-b"]


def test_get_job_batch_uuids_returns_empty_list_when_no_runs(tmp_path):
    """get_job_batch_uuids returns [] not None when there are no runs."""
    db_path = tmp_path / "empty.db"
    init_db(db_path)
    result = get_job_batch_uuids(db_path)
    assert result == []
    assert result is not None


def test_get_run_by_id(tmp_path):
    """Retrieve single run by id."""
    db_path = tmp_path / "test.db"
    row_id = store_run(_make_result(job_name="my-test", processor="x86_64"))
    run = get_run_by_id(db_path, row_id)
    assert run is not None
    assert run["job_name"] == "my-test"
    assert run["passed"] == 1
    assert run["processor"] == "x86_64"

    assert get_run_by_id(db_path, 99999) is None


def test_validation_errors_stored_and_retrieved(tmp_path):
    """Store a run with validation_errors and assert they round-trip correctly."""
    db_path = tmp_path / "test.db"
    errors = ["output mismatch on line 42", "max residual exceeded"]
    result = _make_result(
        job_name="val-test",
        validation_errors=errors,
    )
    row_id = store_run(result)
    run = get_run_by_id(db_path, row_id)
    assert run is not None
    assert run.get("validation_errors") is not None
    stored_errors = json.loads(run["validation_errors"])
    assert stored_errors == errors


def test_get_all_metrics_series(tmp_path):
    """Discover all (solver, metric) pairs with data."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(solver_name="s1", metrics={"mlups": 1.0, "runtime": 0.5}))
    store_run(_make_result(solver_name="s1", metrics={"mlups": 2.0}))
    store_run(_make_result(solver_name="s2", metrics={"throughput": 100}))

    series = get_all_metrics_series(db_path)
    assert ("s1", "mlups") in series
    assert ("s1", "runtime") in series
    assert ("s2", "throughput") in series
    assert len(series) == 3


def test_get_metrics_history(tmp_path):
    """Retrieve metric history for trend visualization."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(solver_name="s1", metrics={"mlups": 1.0}))
    store_run(_make_result(solver_name="s1", metrics={"mlups": 2.0}))
    store_run(_make_result(solver_name="s1", metrics={}))

    history = get_metrics_history(db_path, "s1", "mlups")
    assert len(history) == 2
    values = [v for _, v in history]
    assert 1.0 in values
    assert 2.0 in values


def test_store_run_with_baseline_sets_is_baseline(tmp_path):
    """Storing a run with baseline=True persists is_baseline=1."""
    db_path = tmp_path / "test.db"
    result = _make_result(
        job_name="base", solver_name="s1", metrics={"m": 10.0}, baseline=True
    )
    row_id = store_run(result)
    assert row_id > 0
    run = get_run_by_id(db_path, row_id)
    assert run is not None
    assert run.get("is_baseline") == 1


def test_store_run_baseline_replaces_previous_baseline(tmp_path):
    """Storing a run with baseline=True clears is_baseline on other runs of same solver."""
    db_path = tmp_path / "test.db"
    store_run(
        _make_result(
            job_name="old-base", solver_name="s1", metrics={"m": 1.0}, baseline=True
        )
    )
    store_run(
        _make_result(
            job_name="other", solver_name="s1", metrics={"m": 2.0}, baseline=False
        )
    )
    # New baseline run for same solver
    store_run(
        _make_result(
            job_name="new-base", solver_name="s1", metrics={"m": 3.0}, baseline=True
        )
    )

    runs = get_runs(db_path, solver="s1")
    assert len(runs) == 3
    baseline_runs = [r for r in runs if r.get("is_baseline")]
    assert len(baseline_runs) == 1
    assert baseline_runs[0]["job_name"] == "new-base"


def test_get_baseline_run(tmp_path):
    """get_baseline_run returns the run with is_baseline=1 for that solver, or None."""
    db_path = tmp_path / "test.db"
    store_run(
        _make_result(
            job_name="base", solver_name="s1", metrics={"m": 10.0}, baseline=True
        )
    )
    store_run(
        _make_result(
            job_name="other", solver_name="s1", metrics={"m": 12.0}, baseline=False
        )
    )

    baseline = get_baseline_run(db_path, "s1")
    assert baseline is not None
    assert baseline["job_name"] == "base"
    assert baseline.get("metrics", {}).get("m") == 10.0
    assert baseline.get("is_baseline") is True

    assert get_baseline_run(db_path, "nonexistent") is None


def test_set_baseline_run(tmp_path):
    """set_baseline_run sets the given run as baseline and clears others for that solver."""
    db_path = tmp_path / "test.db"
    id1 = store_run(_make_result(job_name="first", solver_name="s1", baseline=True))
    id2 = store_run(_make_result(job_name="second", solver_name="s1", baseline=False))

    out = set_baseline_run(id2)
    assert out is not None
    assert out["id"] == id2
    assert out.get("is_baseline") is True

    assert get_baseline_run(db_path, "s1")["id"] == id2
    run1 = get_run_by_id(db_path, id1)
    assert run1.get("is_baseline") == 0

    assert set_baseline_run(99999) is None


def test_get_baseline_comparison(tmp_path):
    """get_baseline_comparison returns baseline run and comparisons with delta/delta_pct."""
    db_path = tmp_path / "test.db"
    store_run(
        _make_result(
            job_name="base",
            solver_name="s1",
            metrics={"runtime_seconds": 1.0, "mlups": 100.0},
            baseline=True,
        )
    )
    store_run(
        _make_result(
            job_name="other",
            solver_name="s1",
            metrics={"runtime_seconds": 1.2, "mlups": 90.0},
            baseline=False,
        )
    )

    comparison = get_baseline_comparison(db_path, solver_name="s1")
    assert len(comparison) == 1
    entry = comparison[0]
    assert entry["solver_name"] == "s1"
    assert entry["baseline_run"] is not None
    assert entry["baseline_run"]["job_name"] == "base"
    assert len(entry["comparisons"]) == 1
    vs = entry["comparisons"][0]["vs_baseline"]
    assert vs["runtime_seconds"]["baseline"] == 1.0
    assert vs["runtime_seconds"]["value"] == 1.2
    assert vs["runtime_seconds"]["delta"] == pytest.approx(0.2)
    assert vs["runtime_seconds"]["delta_pct"] == pytest.approx(20.0)
    assert vs["mlups"]["delta"] == pytest.approx(-10.0)
    assert vs["mlups"]["delta_pct"] == pytest.approx(-10.0)


def test_get_baseline_comparison_zero_baseline_metric(tmp_path):
    """When baseline value is 0, delta_pct is None to avoid division by zero."""
    db_path = tmp_path / "test.db"
    store_run(
        _make_result(
            job_name="base",
            solver_name="s1",
            metrics={"count": 0.0},
            baseline=True,
        )
    )
    store_run(
        _make_result(
            job_name="other",
            solver_name="s1",
            metrics={"count": 5.0},
            baseline=False,
        )
    )
    comparison = get_baseline_comparison(db_path, solver_name="s1")
    assert len(comparison) == 1
    vs = comparison[0]["comparisons"][0]["vs_baseline"]
    assert vs["count"]["baseline"] == 0.0
    assert vs["count"]["value"] == 5.0
    assert vs["count"]["delta"] == 5.0
    assert vs["count"]["delta_pct"] is None


def test_get_baseline_comparison_solver_without_baseline(tmp_path):
    """Solver with no baseline run still appears with baseline_run=None and empty comparisons."""
    db_path = tmp_path / "test.db"
    store_run(_make_result(job_name="any", solver_name="s1", baseline=False))

    comparison = get_baseline_comparison(db_path, solver_name="s1")
    assert len(comparison) == 1
    assert comparison[0]["solver_name"] == "s1"
    assert comparison[0]["baseline_run"] is None
    assert comparison[0]["comparisons"] == []


def test_get_baseline_comparison_multi_solver(tmp_path):
    """All solvers returned with correct shape when no solver_name filter given."""
    db_path = tmp_path / "test.db"

    # Solver A: has baseline + 1 other run
    store_run(
        _make_result(
            job_name="a_base",
            solver_name="solverA",
            metrics={"mlups": 100.0},
            baseline=True,
        )
    )
    store_run(
        _make_result(
            job_name="a_other",
            solver_name="solverA",
            metrics={"mlups": 120.0},
            baseline=False,
        )
    )

    # Solver B: has baseline + 2 other runs
    store_run(
        _make_result(
            job_name="b_base",
            solver_name="solverB",
            metrics={"runtime_seconds": 5.0},
            baseline=True,
        )
    )
    store_run(
        _make_result(
            job_name="b_other1",
            solver_name="solverB",
            metrics={"runtime_seconds": 6.0},
            baseline=False,
        )
    )
    store_run(
        _make_result(
            job_name="b_other2",
            solver_name="solverB",
            metrics={"runtime_seconds": 4.5},
            baseline=False,
        )
    )

    # Solver C: no baseline
    store_run(
        _make_result(
            job_name="c_run",
            solver_name="solverC",
            metrics={"mlups": 50.0},
            baseline=False,
        )
    )

    result = get_baseline_comparison(db_path)
    assert len(result) == 3

    by_solver = {entry["solver_name"]: entry for entry in result}

    # Solver A
    a = by_solver["solverA"]
    assert a["baseline_run"] is not None
    assert a["baseline_run"]["job_name"] == "a_base"
    assert len(a["comparisons"]) == 1
    assert a["comparisons"][0]["vs_baseline"]["mlups"]["delta"] == pytest.approx(20.0)

    # Solver B
    b = by_solver["solverB"]
    assert b["baseline_run"] is not None
    assert b["baseline_run"]["job_name"] == "b_base"
    assert len(b["comparisons"]) == 2

    # Solver C — no baseline
    c = by_solver["solverC"]
    assert c["baseline_run"] is None
    assert c["comparisons"] == []
    assert c["other_runs"] == []


# --- db_writer_session context manager tests ---


def test_db_writer_session_starts_and_stops_writer(tmp_path):
    """db_writer_session starts the writer on entry and stops on exit."""
    stop_writer()  # clear autouse fixture's writer
    db_path = tmp_path / "cm.db"
    init_db(db_path)
    with db_writer_session(db_path):
        row_id = store_run(_make_result())
        assert row_id > 0
    from harness.storage.db import _writer

    assert _writer is None


def test_db_writer_session_stops_writer_on_exception(tmp_path):
    """db_writer_session stops the writer even when body raises."""
    stop_writer()  # clear autouse fixture's writer
    db_path = tmp_path / "cm_exc.db"
    init_db(db_path)
    with pytest.raises(ValueError, match="boom"):
        with db_writer_session(db_path):
            raise ValueError("boom")
    from harness.storage.db import _writer

    assert _writer is None


def test_start_writer_raises_on_different_path(tmp_path):
    """start_writer raises RuntimeError when called with a different path while active."""
    stop_writer()  # clear autouse fixture's writer
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    init_db(db_a)
    init_db(db_b)
    with db_writer_session(db_a):
        with pytest.raises(RuntimeError, match="already active"):
            start_writer(db_b)
