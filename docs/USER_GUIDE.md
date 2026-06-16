# User Guide

This guide is for **HPC administrators** and **DevOps engineers** who operate the **HPC Regression Platform**: validating environments after stack changes, comparing runs over time, and triaging failures. It is **not** a tutorial for authoring new solvers from scratch—use **[USER_SOLVER_TEMPLATE.md](USER_SOLVER_TEMPLATE.md)** for package layout and key fields, **[ARCHITECTURE.md](ARCHITECTURE.md)** for how the harness expands runs and how the API executes work, **[UI_DESIGN.md](UI_DESIGN.md)** for screen-by-screen UI behavior, and **[TESTING_SLURM.md](TESTING_SLURM.md)** for Docker SLURM and scheduler-backed smoke tests.

The harness treats each workload as **`{solver}@{system}`** (`job_name`): a **solver** package under `configs/solvers/` run on a named **system** from `configs/systems/`. **[Glossary](GLOSSARY.md)** defines terms used across docs.

**Prerequisites:** From the project root, `uv sync --all-extras --dev`. To use the browser UI or scripted API calls, start the API (`make api`, default http://localhost:8000) and optionally the Streamlit UI (`make ui`, default http://localhost:8501). The UI talks to the API **only over HTTP**—set **`HPC_API_URL`** if the API is not on localhost.

---

## What you need to know about `configs/`

Default on-disk layout (solver-first; **no** required `configs/jobs/` tree):

```
configs/
├── resources/   # Hardware capacity (cpus, memory, optional env)
├── systems/     # Named environments: resource bundles + env (e.g. local vs SLURM)
└── solvers/     # One folder per solver: solver.yaml, entrypoint script, optional parser
```

- **Systems** are *where* jobs run. Names match the API and UI; operators pick **solver × system** cells in the Run Matrix or pass `--system` / API `system`.
- **Solvers** are the packaged benchmarks or applications under test. Runs are expanded from each solver’s **`allowed_systems`** and **`default_system`** at load time—see **[ARCHITECTURE.md](ARCHITECTURE.md) §4** for how `build_jobs_from_solver_specs` forms the job list.

---

## Operational workflows

### Validate after a platform change

After OS updates, module trees, SLURM, drivers, or container images change, confirm that representative **solver × system** pairs still pass and that metrics look sane.

1. Start **API** (and **UI** if you prefer the Run Matrix).
2. Open **Run Matrix**, select the cells that cover your change (or use the CLI with `--solver` / `--system` / `--all-allowed-systems`).
3. Optionally set a **session label** (`session_label` on **`POST /api/run_solvers`**, or the Run Matrix field) to tie runs to a ticket or change ID.
4. Watch **Job Activity** for invocations (background runs), logs, pass/fail, and cancel/stop as needed.

### Investigate a regression

When performance or correctness drifts:

- Use **Individual Trends** or **Long-Term Trends** to see metric history and comparisons to **baselines**.
- Drill from trends into **Job Activity** where supported (**[UI_DESIGN.md](UI_DESIGN.md) §4.4**) to open stored runs, stdout/stderr, and parsed metrics.
- For SLURM-backed runs, use execution status and SLURM endpoints as described in **[ARCHITECTURE.md](ARCHITECTURE.md) §5.2** and **[TESTING_SLURM.md](TESTING_SLURM.md)** (`RUN_SLURM_E2E`, `DOCKER_SLURM_*`, live `squeue`/`sacct` when enabled).

### Batch runs and automation (CLI)

Prefer **narrow filters** so you do not accidentally run every solver in the tree (some workloads may be long or resource-heavy).

```bash
# List solvers (names, default_system, allowed_systems)
uv run hpc-runner configs --list

# Matrix smoke: one run per (solver, allowed system) for the listed solvers; no DB write
uv run hpc-runner configs --solver echo-solver --solver python-solver --all-allowed-systems --no-store

# Single solver on one system
uv run hpc-runner configs --solver echo-solver --system dev-system

# Recent stored runs
uv run hpc-runner configs --list-runs
```

**`--no-store`** is useful for dry validation without touching **`data/harness.db`**. Override the DB path with **`--db`** when isolating test data.

### Scheduler / SLURM path

For real clusters or the Docker SLURM stack used in this repo’s LAMMPS demos, follow **[TESTING_SLURM.md](TESTING_SLURM.md)** (`SLURM_COMPOSE_DIR`, `RUN_SLURM_E2E`, `DOCKER_SLURM_CONTAINER`, `make slurm-up`, etc.).

**Cancel behavior:** **`POST /api/invocations/<id>/cancel`** always tries to stop the local subprocess; best-effort **`scancel`** applies when allowed (**`HARNESS_ALLOW_SCANCEL`**, **`RUN_SLURM_E2E`**, or **`DOCKER_SLURM_*`**—see **[ARCHITECTURE.md](ARCHITECTURE.md) §5.2**).

### Local background-run smoke (optional)

The **`sleep-60-local`** solver is a long-ish local subprocess useful for exercising **Run Matrix**, **Job Activity**, and cancel APIs without SLURM. For a quicker CLI check, use **`echo-solver`**. Changing or adding solvers is covered in **[USER_SOLVER_TEMPLATE.md](USER_SOLVER_TEMPLATE.md)**.

---

## CLI quick reference

Entry point: **`uv run hpc-runner configs`** (optional first argument: config directory, default **`configs`**).

| Option | Description |
|--------|-------------|
| `config_dir` | Optional positional path to config root (default: `configs`) |
| `--solver NAME` | Run only these solvers (repeatable) |
| `--system NAME` | Target system for selected solvers |
| `--all-allowed-systems` | One run per allowed system per selected solver (not with `--system`) |
| `--list` | List solvers and exit |
| `--list-runs` | List last 20 runs from the DB and exit |
| `--no-store` | Do not persist results |
| `--db PATH` | SQLite DB (default: `data/harness.db`) |
| `--solvers-dir` | Override solvers directory |
| `-v` / `--verbose` | Print solver stdout/stderr to stderr; include in JSON |
| `--add CMD` | Create a minimal solver from a command and run (requires `--system`; author-oriented) |

Custom config tree:

```bash
uv run hpc-runner /path/to/configs --solvers-dir /path/to/configs/solvers
```

---

## API essentials (operators)

Use **`GET /docs`** on the API host for the full OpenAPI surface. Typical automation and triage:

| Endpoint | Role |
|----------|------|
| `GET /api/solvers`, `GET /api/systems`, `GET /api/resources` | Discover names for scripts and Run Matrix |
| `POST /api/run_solvers` | Body: `solvers: [{ "name", "system"? }, ...]`, optional `session_label` / `batch_name`, `background`. Sync: **200** + results. **`background: true`**: **202** + **invocation** ids—see **[ARCHITECTURE.md](ARCHITECTURE.md) §5.2** |
| `GET /api/invocations`, `GET /api/invocations/<id>`, `GET /api/invocations/<id>/execution_status` | Track and inspect background runs |
| `POST /api/invocations/<id>/cancel` | Cancel run (subprocess + optional `scancel`) |
| `GET /api/runs`, `GET /api/runs/<id>` | Stored history and detail |
| `GET /api/metrics/...`, `GET /api/solvers/<solver>/baseline`, `GET /api/baseline_comparison` | Metrics and baseline comparison for trends |

**Session label:** Optional tag on **`POST /api/run_solvers`**; **`session_label`** wins over legacy **`batch_name`** when both are set. Persisted with runs for grouping.

---

## UI map (investigation focus)

Aligned with **[UI_DESIGN.md](UI_DESIGN.md) §5**—one line each:

| Page | Use for |
|------|---------|
| **Home** | Orientation |
| **Run Matrix** | Choose **solver × system** cells; optional session label; start background runs |
| **Job Activity** | Invocations and stored runs; logs; cancel; baselines; SLURM refresh when applicable |
| **Individual Trends** | Per-solver metric charts |
| **Long-Term Trends** | Broader metric views; drill to Job Activity where supported |
| **Configs** | Read-only browse of YAML on disk |

Global **toast** notifications when background jobs finish are described in **UI_DESIGN** §4.3.

---

## Troubleshooting (operations)

| Issue | What to check |
|-------|----------------|
| UI cannot reach API | **`HPC_API_URL`**; API process listening; firewall |
| Empty or failing Run Matrix | **`GET /api/solvers`** / **`GET /api/systems`**; config errors in API logs |
| Run failed or validation errors | **Job Activity** or **`GET /api/runs/<id>`**; **`returncode`**, **`validation_errors`**, stdout/stderr |
| No SLURM metadata | **`RUN_SLURM_E2E`**, **`DOCKER_SLURM_*`** per **[TESTING_SLURM.md](TESTING_SLURM.md)** |
| Wrong or missing data in scripts | **`--db`** path; confirm you are querying the same DB the API uses |
| Cancel did not kill cluster job | **`HARNESS_ALLOW_SCANCEL`** / **`RUN_SLURM_E2E`** / **`DOCKER_SLURM_*`** for `scancel`; see **[ARCHITECTURE.md](ARCHITECTURE.md) §5.2** |

---

## Appendix: Legacy `configs/jobs/`

The config **loader** can still consume a **`configs/jobs/`** directory if present (older layouts). **This repository’s default workflow is solver-first:** maintain **`configs/resources/`**, **`configs/systems/`**, and **`configs/solvers/`** only.

---

## See also

[Glossary](GLOSSARY.md) · [Solver template](USER_SOLVER_TEMPLATE.md) · [Architecture](ARCHITECTURE.md) · [UI design](UI_DESIGN.md) · [SLURM testing](TESTING_SLURM.md)
