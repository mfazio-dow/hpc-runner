# tests/test_db_writer_atomicity.py - Concurrency and atomicity tests for DBWriter
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from harness import RunResult
from harness.storage import (
    delete_runs,
    get_run_by_id,
    get_runs,
    init_db,
    set_baseline_run,
    start_writer,
    stop_writer,
    store_run,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture(autouse=True)
def _writer(db_path):
    start_writer(db_path)
    yield
    stop_writer()


def _make_result(
    job_name: str = "j1",
    solver_name: str = "s1",
    baseline: bool = False,
) -> RunResult:
    return RunResult(
        job_name=job_name,
        solver_name=solver_name,
        system_name="dev",
        returncode=0,
        stdout="",
        stderr="",
        runtime_seconds=1.0,
        timestamp="2026-06-25T00:00:00+00:00",
        validation_errors=[],
        passed=True,
        metrics={},
        processor="x86_64",
        baseline=baseline,
        job_batch_uuid="batch-1",
    )


def test_concurrent_baseline_writes_single_winner(db_path):
    """Two threads storing baseline runs for the same solver must leave exactly one baseline.

    Run multiple rounds to increase the chance of triggering the interleaving.
    """
    num_workers = 4
    rounds = 20

    for round_num in range(rounds):
        barrier = threading.Barrier(num_workers, timeout=5)

        def _store_baseline(name: str) -> int:
            barrier.wait()
            return store_run(_make_result(job_name=name, baseline=True))

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = [
                pool.submit(_store_baseline, f"round{round_num}-worker{i}")
                for i in range(num_workers)
            ]
            for f in futures:
                f.result(timeout=10)

        runs = get_runs(db_path, solver="s1")
        baseline_runs = [r for r in runs if r.get("is_baseline")]
        assert len(baseline_runs) == 1, (
            f"Round {round_num}: Expected exactly 1 baseline row, got "
            f"{len(baseline_runs)}: {[r['job_name'] for r in baseline_runs]}"
        )


def test_enqueue_atomic_prevents_interleaving(db_path):
    """Verify that enqueue_atomic keeps multi-statement groups indivisible.

    Even when two atomic envelopes are submitted concurrently, each envelope's
    statements execute together — the second envelope's UPDATE clears the first
    envelope's INSERT, leaving exactly one baseline.
    """
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    solver = "s1"
    insert_sql = """
        INSERT INTO runs (
            job_name, solver_name, system_name, returncode, passed,
            runtime_seconds, timestamp, stdout, stderr, metrics_json,
            processor, validation_errors, is_baseline, job_batch_uuid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    base_params = (
        "dev",
        0,
        1,
        1.0,
        "2026-06-25T00:00:00+00:00",
        "",
        "",
        "{}",
        "x86_64",
        "[]",
    )

    # Two atomic envelopes — each is UPDATE + INSERT as a single queue item
    fut_a = w.enqueue_atomic(
        [
            ("UPDATE runs SET is_baseline = 0 WHERE solver_name = ?", (solver,)),
            (insert_sql, ("run-a", solver, *base_params, 1, "batch-1")),
        ]
    )
    fut_b = w.enqueue_atomic(
        [
            ("UPDATE runs SET is_baseline = 0 WHERE solver_name = ?", (solver,)),
            (insert_sql, ("run-b", solver, *base_params, 1, "batch-1")),
        ]
    )

    fut_a.result(timeout=5)
    fut_b.result(timeout=5)

    runs = get_runs(db_path, solver=solver)
    baseline_runs = [r for r in runs if r.get("is_baseline")]
    assert len(baseline_runs) == 1, (
        f"Expected exactly 1 baseline row, got {len(baseline_runs)}: "
        f"{[r['job_name'] for r in baseline_runs]}"
    )


def test_enqueue_atomic_error_sets_future_exception(db_path):
    """If any statement in a compound envelope fails, the future gets the exception."""
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    fut = w.enqueue_atomic(
        [
            (
                "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
                "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "ok-row",
                    "s1",
                    "dev",
                    0,
                    1,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    0,
                    "b1",
                ),
            ),
            ("INSERT INTO nonexistent_table VALUES (?)", ("boom",)),
        ]
    )

    with pytest.raises(Exception, match="nonexistent_table"):
        fut.result(timeout=5)


def test_enqueue_atomic_returns_last_statement_result(db_path):
    """The future resolves to (lastrowid, rowcount) of the last statement."""
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    fut = w.enqueue_atomic(
        [
            (
                "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
                "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("first", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
            ),
            (
                "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
                "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "second",
                    "s1",
                    "dev",
                    0,
                    1,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    0,
                    "b1",
                ),
            ),
        ]
    )

    lastrowid, rowcount = fut.result(timeout=5)
    assert lastrowid > 0
    assert rowcount == 1

    run = get_run_by_id(db_path, lastrowid)
    assert run is not None
    assert run["job_name"] == "second"

    all_runs = get_runs(db_path, solver="s1")
    job_names = {r["job_name"] for r in all_runs}
    assert "first" in job_names
    assert "second" in job_names


def test_single_enqueue_still_works(db_path):
    """The existing single-statement enqueue path remains functional."""
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    fut = w.enqueue(
        "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
        "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("single", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
    )

    lastrowid, rowcount = fut.result(timeout=5)
    assert lastrowid > 0
    assert rowcount == 1

    run = get_run_by_id(db_path, lastrowid)
    assert run is not None
    assert run["job_name"] == "single"


def test_compound_and_single_items_coexist_in_batch(db_path):
    """Single items and compound envelopes can coexist in the same batch."""
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    insert_sql = (
        "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
        "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    fut1 = w.enqueue(
        insert_sql,
        ("single-1", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
    )
    fut2 = w.enqueue_atomic(
        [
            (
                insert_sql,
                (
                    "atomic-1",
                    "s1",
                    "dev",
                    0,
                    1,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    0,
                    "b1",
                ),
            ),
            (
                insert_sql,
                (
                    "atomic-2",
                    "s1",
                    "dev",
                    0,
                    1,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    0,
                    "b1",
                ),
            ),
        ]
    )
    fut3 = w.enqueue(
        insert_sql,
        ("single-2", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
    )

    fut1.result(timeout=5)
    fut2.result(timeout=5)
    fut3.result(timeout=5)

    runs = get_runs(db_path, solver="s1")
    job_names = sorted(r["job_name"] for r in runs)
    assert job_names == ["atomic-1", "atomic-2", "single-1", "single-2"]


def test_concurrent_set_baseline_run_single_winner(db_path):
    """Two concurrent set_baseline_run calls for the same solver leave one baseline."""
    id1 = store_run(_make_result(job_name="run-1", baseline=False))
    id2 = store_run(_make_result(job_name="run-2", baseline=False))

    barrier = threading.Barrier(2, timeout=5)

    def _set_baseline(run_id: int) -> None:
        barrier.wait()
        set_baseline_run(run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_set_baseline, id1)
        f2 = pool.submit(_set_baseline, id2)
        f1.result(timeout=10)
        f2.result(timeout=10)

    runs = get_runs(db_path, solver="s1")
    baseline_runs = [r for r in runs if r.get("is_baseline")]
    assert len(baseline_runs) == 1


def test_batch_rollback_rejects_all_futures_including_earlier_items(db_path):
    """When a later item in a batch causes rollback, earlier items' futures must NOT resolve.

    Regression test: prior to the fix, _execute_single called fut.set_result() eagerly
    *inside* the open transaction. If a subsequent item in the same batch raised, the
    transaction was rolled back but the earlier future had already been resolved with a
    phantom lastrowid pointing to a row that no longer exists.
    """
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    insert_sql = (
        "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
        "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    # Block the writer thread so both items land in the queue before it drains
    blocker = w.enqueue(
        insert_sql,
        ("blocker", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
    )
    blocker.result(timeout=5)  # ensure the writer has processed the blocker batch

    # Now enqueue two items that will coalesce into the SAME batch:
    # item 1 is valid SQL, item 2 references a nonexistent table
    fut_good = w.enqueue(
        insert_sql,
        ("good-row", "s1", "dev", 0, 1, 1.0, "2026-01-01T00:00:00+00:00", 0, "b1"),
    )
    fut_bad = w.enqueue(
        "INSERT INTO nonexistent_table VALUES (?)",
        ("boom",),
    )

    # Both futures must raise — the entire batch was rolled back
    with pytest.raises(Exception):
        fut_good.result(timeout=5)

    with pytest.raises(Exception):
        fut_bad.result(timeout=5)

    # The "good-row" must NOT exist in the database — it was rolled back
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT id FROM runs WHERE job_name = ?", ("good-row",)
    ).fetchone()
    conn.close()
    assert row is None, "Row from rolled-back transaction must not exist in DB"


def test_set_baseline_run_after_delete_no_stale_read(db_path, monkeypatch):
    """set_baseline_run must not use a stale read when the run is deleted concurrently.

    Regression test for TOCTOU race: the old implementation read the solver_name
    outside the writer queue, then enqueued the write separately. If a delete
    landed between those two operations, the UPDATE silently matched zero rows
    and the old baseline was cleared even though no new baseline was set.

    This test forces the interleaving deterministically: it hooks _enqueue_multi_write
    to delete the target run AFTER the read but BEFORE the write executes.
    The correct behavior is: either set_baseline_run returns None (detecting the
    deletion) OR the existing baseline is preserved (never left with zero baselines
    when one existed before, unless the fix returns None to signal failure).
    """
    import harness.storage.db as db_mod

    id1 = store_run(_make_result(job_name="baseline-candidate", solver_name="s1"))
    store_run(
        _make_result(job_name="existing-baseline", solver_name="s1", baseline=True)
    )

    original_enqueue_multi = db_mod._enqueue_multi_write

    def _intercept_and_delete(statements):
        # Delete the target run AFTER set_baseline_run has read it but BEFORE
        # the write executes — this simulates the TOCTOU gap.
        delete_runs([id1])
        return original_enqueue_multi(statements)

    monkeypatch.setattr(db_mod, "_enqueue_multi_write", _intercept_and_delete)

    result = set_baseline_run(id1)

    # The function MUST detect that the row is gone and return None.
    # The old buggy code would clear the existing baseline (UPDATE SET 0)
    # and then fail to set a new one, leaving the solver with NO baseline
    # but returning None only because the post-write SELECT found nothing.
    assert result is None

    # Critical invariant: since the operation failed, the OLD baseline must
    # still be in place — we must not have cleared it.
    runs = get_runs(db_path, solver="s1")
    baseline_runs = [r for r in runs if r.get("is_baseline")]
    assert len(baseline_runs) == 1, (
        f"Old baseline must be preserved when set_baseline_run fails, "
        f"got {len(baseline_runs)} baselines"
    )


def test_set_baseline_run_nonexistent_returns_none(db_path):
    """set_baseline_run on a run_id that never existed returns None."""
    result = set_baseline_run(9999)
    assert result is None


def test_set_baseline_run_atomic_clears_old_baseline(db_path):
    """set_baseline_run atomically clears old baseline and sets the new one.

    This verifies the fix works correctly in the happy path: the subquery-based
    UPDATE correctly identifies the solver from run_id without a separate read.
    """
    store_run(_make_result(job_name="first", solver_name="solverA", baseline=True))
    id2 = store_run(_make_result(job_name="second", solver_name="solverA"))

    result = set_baseline_run(id2)

    assert result is not None
    assert result["id"] == id2
    assert result["is_baseline"] is True

    runs = get_runs(db_path, solver="solverA")
    baseline_runs = [r for r in runs if r.get("is_baseline")]
    assert len(baseline_runs) == 1
    assert baseline_runs[0]["id"] == id2


def test_drain_batch_mid_drain_sentinel_flushes_pending(db_path):
    """Items enqueued before the stop sentinel mid-drain are all committed.

    Regression test for the "drain-before-stop" semantic: when _drain_batch
    encounters the stop sentinel after already accumulating items, it must
    return those items (not discard them) so _run() flushes them before exit.
    """
    from harness.storage.db import DBWriter, _SingleWork

    # Use a fresh writer so we can pre-load the queue before starting.
    w = DBWriter(db_path)

    insert_sql = (
        "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
        "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    # Pre-load queue: 3 items + stop sentinel — all before start().
    # This guarantees all items and the sentinel are present for a single
    # _drain_batch call, exercising the mid-drain None path.
    futures = []
    for i in range(3):
        fut = Future()
        w._queue.put(
            _SingleWork(
                sql=insert_sql,
                params=(f"mid-drain-{i}", "s1", "dev", 0, 1, 1.0,
                        "2026-01-01T00:00:00+00:00", 0, "b1"),
                fut=fut,
            )
        )
        futures.append(fut)
    w._queue.put(None)  # stop sentinel after the 3 items

    # Now start the writer — it will drain all 3 items + sentinel in one call.
    w.start()
    w._thread.join(timeout=10)

    # All 3 futures must resolve successfully.
    for i, fut in enumerate(futures):
        lastrowid, rowcount = fut.result(timeout=5)
        assert lastrowid > 0, f"Item {i} was not committed"
        assert rowcount == 1

    # Verify all rows exist in the database.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT job_name FROM runs WHERE job_name LIKE 'mid-drain-%' ORDER BY job_name"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == [
        "mid-drain-0", "mid-drain-1", "mid-drain-2"
    ]


def test_stop_with_pending_compound_envelope(db_path):
    """Compound envelopes enqueued before stop() execute before shutdown."""
    from harness.storage.db import _writer

    assert _writer is not None
    w = _writer

    insert_sql = (
        "INSERT INTO runs (job_name, solver_name, system_name, returncode, passed, "
        "runtime_seconds, timestamp, is_baseline, job_batch_uuid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    fut = w.enqueue_atomic(
        [
            (
                insert_sql,
                (
                    "before-stop",
                    "s1",
                    "dev",
                    0,
                    1,
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                    0,
                    "b1",
                ),
            ),
        ]
    )

    stop_writer()

    lastrowid, _ = fut.result(timeout=5)
    assert lastrowid > 0

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT job_name FROM runs WHERE id = ?", (lastrowid,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "before-stop"
