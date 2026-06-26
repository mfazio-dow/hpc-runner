# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **`GET /api/resources`** — New endpoint listing configured resources (name, cpus, gpus, memory_gb, env). Same discovery pattern as `/api/solvers` and `/api/systems`.

### Fixed

- **Type annotation** on `GET /api/metrics/<solver>/<metric>` history — corrected return type from `list[tuple[str, str]]` to `list[tuple[str, float]]` to match what `get_metrics_history` actually returns.

## [Released]

### Documentation

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — Added **§5.2 Asynchronous invocation path** (`POST /api/run_solvers` with `background: true`, per-job **`start_background_run`**, in-memory **`REGISTRY`**, daemon threads, **`invoke_ctl`** / Popen streaming, observation endpoints, cancel behavior); clarified **§5.0** synchronous **200** vs **202**; renumbered call-graph **§5.3**. Updated **§7** to match current Streamlit pages (**Home**, **Run Matrix**, **Job Activity**, trends, **Configs**) and cross-links to **[`docs/UI_DESIGN.md`](docs/UI_DESIGN.md)**; **§10** workspace root label; **§6** `/api/run_solvers` row; **§1**/**§2** notes for Run Matrix / Job Activity.

## [Final Release Candidate] - 2026-04-15

Period covered: **2026-04-01 → 2026-04-15** (since **[0.0.0] - 2026-03-31**). **Git tag and release title:** **Final Release Candidate**. Canonical UI behavior and page names: **[`docs/UI_DESIGN.md`](docs/UI_DESIGN.md)**.

### Added

- **Run Matrix** — Solver × system checkbox grid (disallowed pairs show “—”), row/column select-all toggles, optional **session label**, live invocation status dots in cells with periodic refresh, **Run selected (N)** → background **`POST /api/run_solvers`** (**[`docs/UI_DESIGN.md`](docs/UI_DESIGN.md)** §5.2).
- **Job Activity** — Single page for **active invocations** and **stored runs**: unified job picker, solver/system filters for stored runs, live log viewer (follow / pause rules per scope), cancel, baseline, bulk delete, SLURM refresh when applicable (**§5.3**).
- **Global run-completion feedback** — Sidebar fragment polls invocations on every page; **toast** messages when work finishes, steering users to **Job Activity** (**§4.3**).
- **Long-Term Trends → Job Activity** — Supported Plotly **metric trends** clicks set session state and navigate to **Job Activity** with stored run pre-selection when resolved (**§4.4**).
- **OpenMM solver** example — Local and SLURM-oriented configuration under **`configs/solvers/openmm-solver/`** (**PR #91**).
- **GROMACS example** — MD simulation inputs, **`.gro`** asset, Docker-backed workflow hardening, stdout log lines for harness visibility, parser regex alignment (**PR #110**, **PR #117**).
- **Live logs (Job Activity)** — Refactored streaming viewer for in-flight invocations (**PR #111**).
- **Stable E2E hooks** — Primary pages expose `data-testid` attributes (**§4.5**); Playwright suite under **`src/ui/tests/e2e/`** updated for new names and flows (**PR #100**, **PR #103**).

### Changed

- **Streamlit navigation** — Sidebar **“Go to”** order is **Home** → **Run Matrix** → **Job Activity** → **Individual Trends** → **Long-Term Trends** → **Configs**; **default first page is Home** (**§§4.1, 5**). Legacy session labels (**Solvers**, **Run Solvers**, **Run History**, **Job History**) migrate to **Run Matrix** or **Job Activity** (**§4.2**).
- **Home** — Welcome and orientation only; per-solver metric line charts remain on **Individual Trends** (**§5.1**, **§5.4**).
- **Run Matrix workflow** — Layered environment composition for matrix runs; shared **CSS / layout** module for wide main column and grid presentation; **metrics / chart** refinements on trends views (matrix workflow and chart commits).
- **Long-Term Trends** — Less cramped layout; continued heatmap vs **metric trends** tab model (**§5.5**).
- **Configs** — **Read-only** YAML browse with syntax highlighting (**§5.6**); no in-UI save path (per UI spec non-goals).
- **Sci-SLURM / SLURM testing docs** — Clearer quickstart and extended operational notes (evolved into **`docs/TESTING_SLURM.md`**).
- **README** — Brought in line with Makefile / service and SLURM workflows.
- **Documentation layout** — Top-level guides under **`docs/`** use consistent **`UPPER_SNAKE_CASE`** filenames; **`UI_DESIGN.md`** supersedes the old **`design.md`** naming.

### Removed

- **Legacy Streamlit pages** — Redundant surfaces removed in favor of **Run Matrix** + **Job Activity**; the in-app **Tests** page is gone (use **`make test`** / **`make test-e2e`** from the repo). See **§9.2** non-goals in **`docs/UI_DESIGN.md`**.
- **`docs/demo/`** markdown — **`demo_plan.md`** and **`technical_review_plan.md`** removed (Chem 283 / MSSE presenter materials belong with course staff, not in this tree).
- **GROMACS multi-GPU** paths — Disabled in scripts until the stack can be tested end-to-end (**PR #117**).

### Fixed

- **GROMACS `run.sh`** — Scripting and logging fixes, log file naming, stdout echo for parser consumption (**PR #117**, **PR #110**).
- **Run Matrix / solver UI** — Page-jump and related interaction bugs; **invocation UI** edge case (**PR #106**); **retain user selection** across refreshes where applicable (**PR #109**).
- **Playwright** — Tests repaired and aligned with navigation, **`data-testid`**, and post-refactor page titles.

## [0.0.0] - 2026-03-31

Period covered: **2026-02-24 → 2026-03-31** (approximate).

### Added

- **Solver-first runs** — Configuration and UX center on solvers, not separate job YAML trees. Solver specs can carry **`default_system`** (and related fields where configured). Runs are built from **solver + system**. Harness job display names follow **`{solver}@{system}`** (e.g. `lammps-slurm@sci-slurm-lammps` in SLURM smoke tests).
- **`POST /api/run_solvers`** — Launch API replacing “run jobs by job id”. Supports optional **`batch_name`**, per-entry **`system`** overrides, and **`background`** mode (**202** with **one invocation per solver** when background is enabled).
- **Invocations, cancel, and execution status** — `GET /api/invocations`, `GET /api/invocations/{id}`, **`/execution_status`**. Unified cancellation for local subprocesses and **SLURM** (**`scancel`** when the environment allows). **`slurm_status`** endpoints for live scheduler state when SLURM E2E is enabled. Modules: `src/api/src/basic_restapi/invocations.py`, `slurm_tools.py` (see **`src/api/README.md`**).
- **Streamlit “Run Solvers”** — UI aligned with **`run_solvers`** and invocation monitoring.
- **SLURM + LAMMPS path** — Slurm stack integration (e.g. **sci_slurm**), LAMMPS-on-SLURM smoke testing, Makefile and **`docs/TESTING_SLURM.md`** updates, **`scripts/start-services-slurm.sh`** / related targets. Gated tests: **`RUN_SLURM_E2E=1`** (**PR #83**).
- **Job monitoring** — Improved monitoring with SLURM-oriented behavior that generalizes to other backends (**PR #84**).
- **Job batching** — Batch identities for grouped runs, API for listing job batch UUIDs, UI for batch views and metadata (**PR #77**).
- **Baseline workflow** — Backend and UI for baseline runs, heatmap behavior, copy/layout cleanup (**PR #76**).
- **Monitoring / cancel test solvers** — **`sleep-60-slurm`** (**`sci-slurm-lammps`**, `sbatch` sleep via **[`docker/slurm_sleep/`](docker/slurm_sleep/)**) and **`sleep-60-local`** (**`dev-system`**) for long-enough runs to exercise invocations and cancel.
- **`HARNESS_SOLVER_WALL_SECONDS`** — Optional line printed from inside **`sbatch`** jobs (sleep and LAMMPS templates) and from **`sleep-60-local`**; high-resolution wall time around the workload (GNU **`date +%s.%N`**, **`EPOCHREALTIME`** fallback, **`awk`** delta). The harness prefers it for **`runtime_seconds`** / metrics for **both** SLURM-backed and local runs when present, otherwise refines from **`sacct`** (**`Start`/`End`**, **`Elapsed`**, **`ElapsedRaw`**) when SLURM job ids exist. Implemented in **`src/core/src/harness/slurm_elapsed.py`** after subprocess completion (see **`refine_run_result_runtime_from_slurm`** in **`runner.py`**).

### Changed

- **Config model** — The **`configs/jobs/`** tree is removed; platform configuration is **resources**, **systems**, and **solvers** only.
- **`runtime_seconds` for SLURM** — No longer tied only to coarse integer **`ElapsedRaw`** or the full outer **`run.sh`** duration when batch or local scripts emit **`HARNESS_SOLVER_WALL_SECONDS`** or when **`sacct`** returns finer **Start/End** / **Elapsed** values.

### Removed

- **`/api/run_jobs`** and **`/api/jobs`**-style launch flows in favor of **`/api/run_solvers`**.

### Fixed

- **Metrics and charts** — Charts reorganized; line charts link into run history; heatmap and long-term metrics (spacing, columns, specification ranges, safer inputs, tests/lint) (**PR #69**, **PR #79**, related).
- **UX** — Run-history icon (**PR #61**); single-click exploration from charts (**PR #82**); assorted default/duplicate-figure fixes.

### Documentation and tests

- **Docs** — Architecture, user guide, glossary, and API README updated for the solver-first and invocation model; **[`docs/TESTING_SLURM.md`](docs/TESTING_SLURM.md)** and user guide sections for **`HARNESS_SOLVER_WALL_SECONDS`**, sacct fallback, and the sleep solvers.
- **Tests** — Core, API, and UI tests (including Playwright e2e) adjusted for **Run Solvers** and the new API; core tests for **`slurm_elapsed`** (parsing, sacct order, inner-wall vs local/SLURM).

### Breaking changes / migration

- **Config** — Stop relying on **`configs/jobs/*.yaml`**. Each solver YAML should define **`allowed_systems`** and **`default_system`** where you expect one-click or default runs.
- **API clients** — Use **`POST /api/run_solvers`** with a body like `{"solvers": [{"name": "...", "system": "..."}], ...}` instead of job-based endpoints.
- **Automation** — Update scripts and docs that still mention **`/api/run_jobs`** or legacy **“Run Jobs”** / **Solvers** UI labels to **`run_solvers`** and the **Run Matrix** / **Job Activity** pages (see **`docs/UI_DESIGN.md`**).
