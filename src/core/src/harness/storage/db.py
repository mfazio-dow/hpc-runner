# storage/db.py - SQLite storage for runs and metrics
from __future__ import annotations

import json
import queue
import sqlite3
import threading
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from ..runner import RunResult

logger = structlog.get_logger()

# Queue item types for DBWriter
_SingleItem = tuple[str, tuple, Future]
_CompoundItem = tuple[list[tuple[str, tuple]], Future]
_QueueItem = _SingleItem | _CompoundItem | None


# ---------------------------------------------------------------------------
# Read-only connection helper
# ---------------------------------------------------------------------------


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# DBWriter — single dedicated writer thread backed by a queue
# ---------------------------------------------------------------------------


class DBWriter:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._queue: queue.Queue[_QueueItem] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="db-writer")
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=30)
        self._started = False

    def enqueue(self, sql: str, params: tuple = ()) -> Future:
        fut: Future = Future()
        self._queue.put((sql, params, fut))
        return fut

    def enqueue_atomic(self, statements: list[tuple[str, tuple]]) -> Future:
        """Enqueue multiple statements as a single indivisible unit."""
        fut: Future = Future()
        self._queue.put((statements, fut))
        return fut

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            while True:
                batch, stop = self._drain_batch()
                self._execute_batch(conn, batch)
                if stop:
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _drain_batch(self) -> tuple[list[_SingleItem | _CompoundItem], bool]:
        item = self._queue.get()
        if item is None:
            return [], True
        batch: list[_SingleItem | _CompoundItem] = [item]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                return batch, True
            batch.append(nxt)
        return batch, False

    def _execute_batch(
        self, conn: sqlite3.Connection, batch: list[_SingleItem | _CompoundItem]
    ) -> None:
        if not batch:
            return
        try:
            conn.execute("BEGIN")
            for entry in batch:
                if len(entry) == 3:
                    self._execute_single(conn, entry)  # type: ignore[arg-type]
                else:
                    self._execute_compound(conn, entry)  # type: ignore[arg-type]
            conn.commit()
        except Exception as exc:
            logger.error("db_writer.batch_error", error=str(exc))
            try:
                conn.rollback()
            except Exception:
                pass
            self._rollback_futures(batch, exc)

    def _execute_single(self, conn: sqlite3.Connection, entry: _SingleItem) -> None:
        sql, params, fut = entry
        try:
            cur = conn.execute(sql, params)
            fut.set_result((cur.lastrowid or 0, cur.rowcount))
        except Exception as exc:
            logger.error("db_writer.exec_error", sql=sql[:120], error=str(exc))
            fut.set_exception(exc)

    def _execute_compound(self, conn: sqlite3.Connection, entry: _CompoundItem) -> None:
        statements, fut = entry
        if not statements:
            fut.set_result((0, 0))
            return
        try:
            cur = None
            for sql, params in statements:
                cur = conn.execute(sql, params)
            fut.set_result((cur.lastrowid or 0, cur.rowcount))  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("db_writer.exec_error", sql=sql[:120], error=str(exc))  # type: ignore[possibly-undefined]
            fut.set_exception(exc)

    def _rollback_futures(
        self, batch: list[_SingleItem | _CompoundItem], exc: Exception
    ) -> None:
        for entry in batch:
            if len(entry) == 3:
                _, _, fut = entry  # type: ignore[misc]
            else:
                _, fut = entry  # type: ignore[misc]
            if not fut.done():
                fut.set_exception(exc)


_writer: DBWriter | None = None
_writer_lock = threading.Lock()


def start_writer(db_path: str | Path) -> None:
    global _writer
    with _writer_lock:
        if _writer is not None:
            if Path(_writer._db_path) != Path(db_path):
                raise RuntimeError(
                    f"DBWriter already active for {_writer._db_path!r}; "
                    f"cannot start for {db_path!r}"
                )
            return
        _writer = DBWriter(db_path)
        _writer.start()


def stop_writer() -> None:
    global _writer
    with _writer_lock:
        w = _writer
        _writer = None
    if w is not None:
        w.stop()


@contextmanager
def db_writer_session(db_path: str | Path) -> Generator[None, None, None]:
    start_writer(db_path)
    try:
        yield
    finally:
        stop_writer()


def _require_writer() -> DBWriter:
    w = _writer
    if w is None:
        raise RuntimeError("DBWriter not started — call start_writer() first")
    return w


def _get_writer_db_path() -> str:
    """Return the db path the active writer is connected to."""
    return _require_writer()._db_path


def _enqueue_write(sql: str, params: tuple = ()) -> Future:
    return _require_writer().enqueue(sql, params)


# ---------------------------------------------------------------------------
# Multi-statement write helper (for operations needing >1 SQL in one tx)
# ---------------------------------------------------------------------------


def _enqueue_multi_write(statements: list[tuple[str, tuple]]) -> Future:
    """Submit multiple SQL statements as a single atomic unit in one transaction."""
    return _require_writer().enqueue_atomic(statements)


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------


def init_db(path: str | Path) -> None:
    """Create tables if they don't exist. Enable WAL mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                solver_name TEXT NOT NULL,
                system_name TEXT NOT NULL,
                returncode INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                runtime_seconds REAL NOT NULL,
                timestamp TEXT NOT NULL,
                stdout TEXT,
                stderr TEXT,
                metrics_json TEXT,
                processor TEXT,
                validation_errors TEXT,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                job_batch_uuid TEXT NOT NULL,
                job_batch_date TEXT,
                job_batch_name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_solver ON runs(solver_name);
            CREATE INDEX IF NOT EXISTS idx_job_batch_uuid_solver ON runs(job_batch_uuid);
            CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);
            CREATE TABLE IF NOT EXISTS run_matrix_presets (
                label TEXT PRIMARY KEY,
                cells_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """
        )
        # Migration: add columns if missing (existing DBs)
        cur = conn.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cur.fetchall()]
        if "processor" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN processor TEXT")
        if "validation_errors" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN validation_errors TEXT")
        if "is_baseline" not in columns:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0"
            )
        if "job_batch_uuid" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN job_batch_uuid TEXT NOT NULL")
        if "job_batch_date" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN job_batch_date TEXT")
        if "job_batch_name" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN job_batch_name TEXT")
        if "scheduler_backend" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN scheduler_backend TEXT")
        if "scheduler_job_ids" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN scheduler_job_ids TEXT")
        if "submit_container" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN submit_container TEXT")
        conn.commit()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")


# ---------------------------------------------------------------------------
# Write functions — routed through the writer queue
# ---------------------------------------------------------------------------


def store_run(result: RunResult) -> int:
    """Store a run result and return the inserted row id."""
    metrics_json = json.dumps(result.metrics) if result.metrics else None
    validation_errors_json = json.dumps(result.validation_errors or [])
    is_baseline = 1 if getattr(result, "baseline", False) else 0

    insert_sql = """
        INSERT INTO runs (
            job_name, solver_name, system_name, returncode, passed,
            runtime_seconds, timestamp, stdout, stderr, metrics_json,
            processor, validation_errors, is_baseline, job_batch_uuid,
            job_batch_date, job_batch_name, scheduler_backend,
            scheduler_job_ids, submit_container
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    insert_params = (
        result.job_name,
        result.solver_name,
        result.system_name,
        result.returncode,
        1 if result.passed else 0,
        result.runtime_seconds,
        result.timestamp,
        result.stdout,
        result.stderr,
        metrics_json,
        result.processor,
        validation_errors_json,
        is_baseline,
        result.job_batch_uuid,
        result.job_batch_date,
        result.job_batch_name,
        getattr(result, "scheduler_backend", None) or "",
        json.dumps(getattr(result, "scheduler_job_ids", None) or []),
        getattr(result, "submit_container", None) or "",
    )

    if is_baseline:
        stmts: list[tuple[str, tuple]] = [
            (
                "UPDATE runs SET is_baseline = 0 WHERE solver_name = ?",
                (result.solver_name,),
            ),
            (insert_sql, insert_params),
        ]
        fut = _enqueue_multi_write(stmts)
    else:
        fut = _enqueue_write(insert_sql, insert_params)

    lastrowid, _ = fut.result()
    return lastrowid


def delete_runs(run_ids: list[int]) -> int:
    """Delete runs by primary key. Returns the number of rows deleted."""
    if not run_ids:
        return 0
    unique_ids = list(dict.fromkeys(int(i) for i in run_ids))
    placeholders = ",".join("?" * len(unique_ids))
    fut = _enqueue_write(
        f"DELETE FROM runs WHERE id IN ({placeholders})",
        tuple(unique_ids),
    )
    _, rowcount = fut.result()
    return rowcount


def set_baseline_run(run_id: int) -> dict[str, Any] | None:
    """Set a specific run as the baseline for its solver."""
    db_path = _get_writer_db_path()
    with _connect_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT id, solver_name FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    solver_name = row[1]

    stmts: list[tuple[str, tuple]] = [
        ("UPDATE runs SET is_baseline = 0 WHERE solver_name = ?", (solver_name,)),
        ("UPDATE runs SET is_baseline = 1 WHERE id = ?", (run_id,)),
    ]
    _enqueue_multi_write(stmts).result()  # wait for completion

    with _connect_readonly(db_path) as conn:
        updated = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_to_response(dict(updated))


def upsert_matrix_preset(label: str, cells: list[dict[str, Any]]) -> None:
    """Insert or replace a Run Matrix preset."""
    key = _normalize_matrix_preset_label(label)
    if not key:
        raise ValueError("preset label must be non-empty")
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(cells, ensure_ascii=False)
    fut = _enqueue_write(
        """
        INSERT INTO run_matrix_presets (label, cells_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(label) DO UPDATE SET
            cells_json = excluded.cells_json,
            updated_at = excluded.updated_at
        """,
        (key, payload, now),
    )
    fut.result()  # wait for completion


def delete_matrix_preset(label: str) -> int:
    """Delete preset by normalized label. Returns rowcount (0 if missing)."""
    key = _normalize_matrix_preset_label(label)
    if not key:
        return 0
    fut = _enqueue_write(
        "DELETE FROM run_matrix_presets WHERE label = ?",
        (key,),
    )
    _, rowcount = fut.result()
    return rowcount


# ---------------------------------------------------------------------------
# Read functions — direct connections (WAL allows concurrent readers)
# ---------------------------------------------------------------------------


def get_runs(
    db_path: str | Path,
    solver: str | None = None,
    processor: str | None = None,
    system: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch runs with optional solver, processor, and system (system_name) filters."""
    conditions: list[str] = []
    params: list[Any] = []
    if solver:
        conditions.append("solver_name = ?")
        params.append(solver)
    if processor:
        conditions.append("processor = ?")
        params.append(processor)
    if system:
        conditions.append("system_name = ?")
        params.append(system)
    where = ("WHERE " + " AND ".join(conditions) + " ") if conditions else ""
    params.extend([limit, offset])
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            f"""SELECT * FROM runs {where}ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_run_by_id(db_path: str | Path, run_id: int) -> dict[str, Any] | None:
    """Fetch a single run by id."""
    with _connect_readonly(db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_solver_run_summaries(db_path: str | Path) -> list[dict[str, Any]]:
    """Per-solver aggregates for monitoring: run count, passes, last run time, last job name."""
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                solver_name,
                COUNT(*) AS total_runs,
                SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS pass_count,
                MAX(timestamp) AS last_timestamp
            FROM runs
            GROUP BY solver_name
            ORDER BY solver_name
            """
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            rdict = dict(row)
            sname = rdict["solver_name"]
            last = conn.execute(
                """SELECT job_name, passed FROM runs WHERE solver_name = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (sname,),
            ).fetchone()
            rdict["last_job_name"] = last[0] if last else None
            rdict["last_passed"] = bool(last[1]) if last else None
            summaries.append(rdict)
        return summaries


def get_all_metrics_series(
    db_path: str | Path, limit: int = 500
) -> list[tuple[str, str]]:
    """Discover all (solver_name, metric_name) pairs that have data."""
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """SELECT solver_name, metrics_json FROM runs
               WHERE metrics_json IS NOT NULL AND metrics_json != '{}'
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for solver_name, mj in rows:
        try:
            m = json.loads(mj or "{}")
            for k, v in m.items():
                if (
                    not isinstance(v, bool)
                    and isinstance(v, (int, float))
                    and (solver_name, k) not in seen
                ):
                    seen.add((solver_name, k))
                    result.append((solver_name, k))
        except json.JSONDecodeError:
            pass
    return sorted(result, key=lambda x: (x[0], x[1]))


def get_metrics_history(
    db_path: str | Path,
    solver_name: str,
    metric_name: str,
    limit: int = 100,
) -> list[tuple[str, float]]:
    """Get (timestamp, value) history for a metric."""
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """SELECT timestamp, metrics_json FROM runs
               WHERE solver_name = ? AND metrics_json IS NOT NULL
               ORDER BY timestamp DESC LIMIT ?""",
            (solver_name, limit),
        ).fetchall()
    result: list[tuple[str, float]] = []
    for ts, mj in rows:
        try:
            m = json.loads(mj or "{}")
            if (
                metric_name in m
                and not isinstance(m[metric_name], bool)
                and isinstance(m[metric_name], (int, float))
            ):
                result.append((ts, float(m[metric_name])))
        except json.JSONDecodeError:
            pass
    result.reverse()
    return result


def _run_to_response(r: dict[str, Any]) -> dict[str, Any]:
    """Decode metrics_json and validation_errors for a run row."""
    out = dict(r)
    if out.get("metrics_json"):
        try:
            out["metrics"] = json.loads(out["metrics_json"])
        except json.JSONDecodeError:
            out["metrics"] = {}
    else:
        out["metrics"] = {}
    if out.get("validation_errors") is not None:
        try:
            out["validation_errors"] = json.loads(out["validation_errors"])
        except json.JSONDecodeError:
            out["validation_errors"] = []
    else:
        out["validation_errors"] = []
    out["passed"] = bool(out.get("passed"))
    out["is_baseline"] = bool(out.get("is_baseline", False))
    return out


def get_baseline_run(db_path: str | Path, solver_name: str) -> dict[str, Any] | None:
    """Return the run marked as baseline for the given solver, or None."""
    with _connect_readonly(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM runs
               WHERE solver_name = ? AND is_baseline = 1
               ORDER BY timestamp DESC LIMIT 1""",
            (solver_name,),
        ).fetchone()
    if row is None:
        return None
    return _run_to_response(dict(row))


def get_baseline_comparison(
    db_path: str | Path,
    solver_name: str | None = None,
    limit_per_solver: int = 50,
) -> list[dict[str, Any]]:
    """Per-solver baseline comparison with per-metric deltas."""
    with _connect_readonly(db_path) as conn:
        if solver_name:
            solvers_rows = conn.execute(
                "SELECT DISTINCT solver_name FROM runs WHERE solver_name = ?",
                (solver_name,),
            ).fetchall()
        else:
            solvers_rows = conn.execute(
                "SELECT DISTINCT solver_name FROM runs"
            ).fetchall()
    solvers_list = [r[0] for r in solvers_rows]
    result: list[dict[str, Any]] = []
    for sname in solvers_list:
        baseline = get_baseline_run(db_path, sname)
        if not baseline:
            result.append(
                {
                    "solver_name": sname,
                    "baseline_run": None,
                    "other_runs": [],
                    "comparisons": [],
                }
            )
            continue
        baseline_metrics = baseline.get("metrics") or {}
        with _connect_readonly(db_path) as conn:
            others = conn.execute(
                """SELECT * FROM runs
                   WHERE solver_name = ? AND (is_baseline = 0 OR id != ?)
                   ORDER BY timestamp DESC LIMIT ?""",
                (sname, baseline["id"], limit_per_solver),
            ).fetchall()
        comparisons: list[dict[str, Any]] = []
        other_runs_decoded: list[dict[str, Any]] = []
        for row in others:
            r = _run_to_response(dict(row))
            other_runs_decoded.append(r)
            vs: dict[str, dict[str, Any]] = {}
            for k, base_val in baseline_metrics.items():
                if not isinstance(base_val, (int, float)):
                    continue
                val = r["metrics"].get(k)
                if val is None or not isinstance(val, (int, float)):
                    continue
                base_f = float(base_val)
                val_f = float(val)
                delta = val_f - base_f
                delta_pct = (100.0 * delta / base_f) if base_f != 0 else None
                vs[k] = {
                    "baseline": base_f,
                    "value": val_f,
                    "delta": delta,
                    "delta_pct": delta_pct,
                }
            comparisons.append(
                {
                    "run_id": r["id"],
                    "job_name": r["job_name"],
                    "timestamp": r["timestamp"],
                    "metrics": r["metrics"],
                    "vs_baseline": vs,
                }
            )
        result.append(
            {
                "solver_name": sname,
                "baseline_run": baseline,
                "other_runs": other_runs_decoded,
                "comparisons": comparisons,
            }
        )
    return result


def get_job_batch_uuids(db_path: str | Path, limit: int = 100) -> list[Any] | None:
    """Return job_batch_uuid values ordered by most recent run in each batch."""
    lim = int(limit)
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT job_batch_uuid
            FROM runs
            WHERE job_batch_uuid != ''
            GROUP BY job_batch_uuid
            ORDER BY MAX(timestamp) DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    if rows is None:
        return None
    return [row["job_batch_uuid"] for row in rows]


def _normalize_matrix_preset_label(label: str) -> str:
    return (label or "").strip().lower()


def list_matrix_presets(db_path: str | Path) -> list[dict[str, Any]]:
    """Return saved Run Matrix selections."""
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            "SELECT label, cells_json, updated_at FROM run_matrix_presets ORDER BY label ASC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        try:
            r["cells"] = json.loads(r.pop("cells_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            r["cells"] = []
        out.append(r)
    return out


def get_matrix_preset(db_path: str | Path, label: str) -> dict[str, Any] | None:
    """Fetch one preset by label (normalized)."""
    key = _normalize_matrix_preset_label(label)
    if not key:
        return None
    with _connect_readonly(db_path) as conn:
        row = conn.execute(
            "SELECT label, cells_json, updated_at FROM run_matrix_presets WHERE label = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    try:
        r["cells"] = json.loads(r.pop("cells_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        r["cells"] = []
    return r
