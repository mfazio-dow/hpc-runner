# cli.py - CLI entrypoint for harness
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .add_solver import add_solver
from .config import (
    ConfigError,
    load_all,
    load_resources,
    load_systems,
    build_jobs_from_solver_specs,
)
from .runner import RunResult, run_jobs
from .storage import init_db, db_writer_session, store_run, get_runs


def _print_and_build_output(results: list[RunResult], verbose: bool) -> list[dict]:
    """Build JSON-serializable result rows; if verbose, print solver stdout/stderr to stderr."""
    output: list[dict] = []
    for r in results:
        if verbose:
            print(f"\n{'=' * 60}\nJob: {r.job_name}\n{'=' * 60}", file=sys.stderr)
            print("--- stdout ---", file=sys.stderr)
            print(r.stdout if r.stdout else "(empty)", file=sys.stderr)
            print("--- stderr ---", file=sys.stderr)
            print(r.stderr if r.stderr else "(empty)", file=sys.stderr)
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
            "validation_errors": r.validation_errors,
        }
        if verbose:
            item["stdout"] = r.stdout
            item["stderr"] = r.stderr
        output.append(item)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Harness - execution-agnostic job runner"
    )
    parser.add_argument(
        "config_dir",
        nargs="?",
        default="configs",
        help="Path to configuration directory (resources, systems, solvers)",
    )
    parser.add_argument(
        "--solvers-dir",
        default=None,
        help="Path to solvers directory (default: config_dir/solvers)",
    )
    parser.add_argument(
        "--solver",
        action="append",
        dest="solvers",
        help="Run only these solver names (can repeat)",
    )
    parser.add_argument(
        "--db",
        default="data/harness.db",
        help="SQLite database path for storing results",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Do not store results in database",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available jobs and exit",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="List recent runs from database and exit",
    )
    parser.add_argument(
        "--add",
        metavar="CMD",
        help="Create a solver from a shell command and run it",
    )
    parser.add_argument(
        "--system",
        metavar="NAME",
        help="With --add: required (target system for the new solver). Otherwise: run selected solvers on this system (overrides default_system).",
    )
    parser.add_argument(
        "--all-allowed-systems",
        action="store_true",
        help="Run each selected solver once per entry in its allowed_systems (cannot combine with --system)",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="Custom solver name (optional with --add)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print each job's stdout/stderr (solver output) on stderr; add stdout/stderr to JSON",
    )

    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir).resolve()

    # Resolve solvers directory (None = use config_dir/solvers)
    solvers_root = Path(args.solvers_dir).resolve() if args.solvers_dir else None
    if solvers_root is None:
        solvers_root = config_dir / "solvers"

    if args.add and args.all_allowed_systems:
        print("Cannot use --all-allowed-systems with --add", file=sys.stderr)
        return 1

    # --add: create solver and job, then run
    if args.add:
        if not args.system:
            print("--system is required when using --add", file=sys.stderr)
            return 1
        resources = load_resources(config_dir)
        systems = load_systems(config_dir)
        if args.system not in systems:
            print(
                f"System '{args.system}' not found. Available: {sorted(systems.keys())}",
                file=sys.stderr,
            )
            return 1
        try:
            solver_name = add_solver(
                config_dir, solvers_root, args.add, args.system, args.name
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        try:
            resources, systems, solvers = load_all(config_dir, solvers_root)
        except ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 1
        if solver_name not in solvers:
            print(f"Solver '{solver_name}' not found after add", file=sys.stderr)
            return 1
        try:
            job_list = build_jobs_from_solver_specs(
                solvers, systems, [{"name": solver_name, "system": None}]
            )
        except ConfigError as e:
            print(str(e), file=sys.stderr)
            return 1
        results = run_jobs(job_list, solvers, systems, resources=resources)
        if not args.no_store:
            init_db(args.db)
            with db_writer_session(args.db):
                for r in results:
                    store_run(r)
        output = _print_and_build_output(results, args.verbose)
        print(json.dumps(output, indent=2))
        return 0 if all(r.passed for r in results) else 1

    try:
        resources, systems, solvers = load_all(config_dir, solvers_root)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    if args.list:
        print("Available solvers:")
        for s in sorted(solvers.values(), key=lambda x: x.name):
            extra = f" default_system={s.default_system}" if s.default_system else ""
            print(f"  - {s.name}{extra} allowed_systems={s.allowed_systems}")
        return 0

    if args.list_runs:
        init_db(args.db)
        runs = get_runs(args.db, limit=20)
        for r in runs:
            if r.get("passed"):
                status = "PASS"
            elif r.get("validation_errors"):
                status = "VALIDATION_ERROR"
            else:
                status = "FAIL"
            proc = r.get("processor") or "-"
            err_info = ""
            # if validation errors are present, show them
            try:
                validation_errors = json.loads(r.get("validation_errors") or "[]")
                if validation_errors:
                    n = len(validation_errors)
                    err_info = f" | {n} validation error{'s' if n != 1 else ''}"
            except (json.JSONDecodeError, TypeError):
                pass
            print(
                f"  {r['id']}: {r['job_name']} | {status} | {proc} | {r['timestamp']}{err_info}"
            )
        return 0

    solver_names = sorted(solvers.keys())
    if args.solvers:
        unknown = [n for n in args.solvers if n not in solvers]
        if unknown:
            print(
                f"Unknown solver(s): {unknown}. Available: {solver_names}",
                file=sys.stderr,
            )
            return 1
        selected = args.solvers
    else:
        selected = solver_names

    if not selected:
        print(
            f"No solvers in {config_dir}/solvers. Add solver folders with solver.yaml.",
            file=sys.stderr,
        )
        return 1

    if args.system and args.all_allowed_systems:
        print("Cannot use both --system and --all-allowed-systems", file=sys.stderr)
        return 1

    try:
        specs: list[dict] = []
        if args.all_allowed_systems:
            for name in selected:
                solver = solvers[name]
                if solver.allowed_systems:
                    for sys_name in solver.allowed_systems:
                        specs.append({"name": name, "system": sys_name})
                else:
                    specs.append({"name": name, "system": None})
        elif args.system:
            specs = [{"name": n, "system": args.system} for n in selected]
        else:
            specs = [{"name": n, "system": None} for n in selected]
        job_list = build_jobs_from_solver_specs(solvers, systems, specs)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1

    results = run_jobs(job_list, solvers, systems, resources=resources)

    if not args.no_store:
        init_db(args.db)
        with db_writer_session(args.db):
            for r in results:
                store_run(r)

    output = _print_and_build_output(results, args.verbose)
    print(json.dumps(output, indent=2))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
