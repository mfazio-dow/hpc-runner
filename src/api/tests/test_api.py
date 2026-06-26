# tests/test_api.py - API error handling and responses

from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from harness import RunResult, init_db, store_run

from basic_restapi.fastapi_app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_api_run_solvers_unknown_solver_returns_400(client):
    """POST /api/run_solvers with unknown solver returns 400."""
    response = client.post(
        "/api/run_solvers",
        json={"solvers": [{"name": "nonexistent-solver-xyz"}]},
    )
    assert response.status_code == 400
    data = response.json()
    assert "error" in data


def test_api_run_solvers_empty_list_returns_400(client):
    """POST /api/run_solvers with solvers: [] returns 400."""
    response = client.post("/api/run_solvers", json={"solvers": []})
    assert response.status_code == 400
    data = response.json()
    assert "error" in data


def test_api_config_error_returns_500(tmp_path):
    """API returns 500 with ConfigError detail when config is invalid."""
    (tmp_path / "resources").mkdir()
    (tmp_path / "systems").mkdir()
    (tmp_path / "solvers").mkdir()

    (tmp_path / "resources" / "r.yaml").write_text(
        yaml.safe_dump({"resources": [{"name": "r1"}]})
    )
    (tmp_path / "systems" / "s.yaml").write_text(
        yaml.safe_dump({"systems": [{"name": "s1", "resources": ["r1"]}]})
    )
    (tmp_path / "solvers" / "sol1").mkdir()
    (tmp_path / "solvers" / "sol1" / "solver.yaml").write_text(
        yaml.safe_dump(
            {"name": "sol1", "entrypoint": "run.sh", "allowed_systems": ["bogus"]}
        )
    )
    (tmp_path / "solvers" / "sol1" / "run.sh").write_text("#!/bin/bash\necho ok\n")

    with patch("basic_restapi.fastapi_app.CONFIG_DIR", tmp_path):
        test_client = TestClient(app)
        response = test_client.get("/api/solvers")

    assert response.status_code == 500
    data = response.json()
    assert "error" in data
    assert "Config load failed" in data["error"]
    assert "detail" in data


def test_api_solver_baseline_returns_404_when_no_baseline(client):
    """GET /api/solvers/{solver}/baseline returns 404 when solver has no baseline run."""
    response = client.get("/api/solvers/no-baseline-solver-xyz/baseline")
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data


def test_api_set_baseline_returns_404_when_run_not_found(client):
    """POST /api/runs/{run_id}/set_baseline returns 404 when run_id does not exist."""
    response = client.post("/api/runs/999999/set_baseline")
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data


def test_api_baseline_comparison_returns_list(client):
    """GET /api/baseline_comparison returns 200 and a list of solver comparison entries."""
    response = client.get("/api/baseline_comparison")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    for entry in data:
        assert "solver_name" in entry
        assert "baseline_run" in entry
        assert "other_runs" in entry
        assert "comparisons" in entry


def test_api_job_batch_uuids_returns_list(client):
    """GET /api/get_job_batch_uuids returns 200 and a list."""
    response = client.get("/api/get_job_batch_uuids")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_api_delete_runs(tmp_path):
    """DELETE /api/runs removes rows from the configured DB."""
    db = tmp_path / "del.db"
    init_db(db)
    with patch("basic_restapi.fastapi_app.DB_PATH", db):
        with TestClient(app) as tc:
            rid = store_run(
                RunResult(
                    job_name="j",
                    solver_name="sv",
                    system_name="sy",
                    returncode=0,
                    stdout="",
                    stderr="",
                    runtime_seconds=0.1,
                    timestamp="2026-01-01T00:00:00+00:00",
                    passed=True,
                    job_batch_uuid="batch-u",
                )
            )
            r = tc.request("DELETE", "/api/runs", json={"ids": [rid, 999001]})
            assert r.status_code == 200
            assert r.json()["deleted"] == 1
            assert tc.get(f"/api/runs/{rid}").status_code == 404


def test_api_delete_runs_empty_ids_returns_422(client):
    r = client.request("DELETE", "/api/runs", json={"ids": []})
    assert r.status_code == 422


def test_api_run_solvers_session_label_equivalent_to_batch_name(client):
    """POST with session_label only passes the same sub_batch string as batch_name-only."""
    captured: list[str] = []

    def fake_start(jl, sol, sys, res, bn, *, solver_name: str = "", job_names=None):
        captured.append(bn)
        return f"id-{solver_name or 'none'}"

    with patch(
        "basic_restapi.fastapi_app.invocations.start_background_run",
        side_effect=fake_start,
    ):
        r_sl = client.post(
            "/api/run_solvers",
            json={
                "solvers": [{"name": "echo-solver", "system": "dev-system"}],
                "background": True,
                "session_label": "my-session",
                "batch_name": "",
            },
        )
        r_bn = client.post(
            "/api/run_solvers",
            json={
                "solvers": [{"name": "echo-solver", "system": "dev-system"}],
                "background": True,
                "batch_name": "my-session",
                "session_label": "",
            },
        )
    assert r_sl.status_code == 202
    assert r_bn.status_code == 202
    assert captured[0] == captured[1] == "my-session:echo-solver"


def test_api_run_solvers_session_label_wins_when_both_set(client):
    captured: list[str] = []

    def fake_start(jl, sol, sys, res, bn, *, solver_name: str = "", job_names=None):
        captured.append(bn)
        return "id"

    with patch(
        "basic_restapi.fastapi_app.invocations.start_background_run",
        side_effect=fake_start,
    ):
        r = client.post(
            "/api/run_solvers",
            json={
                "solvers": [{"name": "echo-solver", "system": "dev-system"}],
                "background": True,
                "session_label": "preferred",
                "batch_name": "ignored",
            },
        )
    assert r.status_code == 202
    assert captured == ["preferred:echo-solver"]


def test_api_run_solvers_background_one_invocation_per_solver(client):
    """background starts one invocation per solver."""
    calls: list[tuple[str, list[str]]] = []

    def fake_start(jl, sol, sys, res, bn, *, solver_name: str = "", job_names=None):
        calls.append((solver_name or "", [j.name for j in jl]))
        return f"id-{solver_name or 'none'}"

    with patch(
        "basic_restapi.fastapi_app.invocations.start_background_run",
        side_effect=fake_start,
    ):
        r = client.post(
            "/api/run_solvers",
            json={
                "solvers": [
                    {"name": "echo-solver", "system": "dev-system"},
                    {"name": "python-solver", "system": "dev-system"},
                ],
                "background": True,
            },
        )
    assert r.status_code == 202
    data = r.json()
    assert len(data["invocations"]) == 2
    assert len(calls) == 2
    by_solver = {c[0]: c[1] for c in calls}
    assert set(by_solver.keys()) == {"echo-solver", "python-solver"}


def test_api_invocations_list_get_enriched_and_slurm_404(client):
    """GET /api/invocations and GET /api/invocations/{id} expose batch and live control fields."""
    from basic_restapi import invocations

    invocations.REGISTRY.clear()
    try:
        rec = invocations.InvocationRecord(
            id="deadbeef", status="running", batch_name="batch-a"
        )
        rec.control.slurm_job_ids.append("999")
        rec.control.submit_container = "host"
        rec.control.jobs_total = 3
        rec.control.jobs_completed = 2
        invocations.REGISTRY["deadbeef"] = rec

        r = client.get("/api/invocations")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        assert any(x.get("invocation_id") == "deadbeef" for x in rows)
        detail = client.get("/api/invocations/deadbeef").json()
        assert detail["status"] == "running"
        assert detail["batch_name"] == "batch-a"
        assert detail["session_label"] == "batch-a"
        assert detail["scheduler_job_ids"] == ["999"]
        assert detail["submit_container"] == "host"
        assert detail["jobs_total"] == 3
        assert detail["jobs_completed"] == 2
        assert detail.get("solver_name") == ""
        assert detail.get("system_names") == []
        assert detail.get("job_names") == []
        assert detail.get("execution", {}).get("backend") == "slurm"
        assert "live_stdout" in detail
        assert detail["live_stdout"] == ""

        r_active = client.get("/api/invocations", params={"active_only": True})
        assert r_active.status_code == 200
        assert all(x["status"] in ("queued", "running") for x in r_active.json())

        assert client.get("/api/invocations/nonesuch/slurm_status").status_code == 404
    finally:
        invocations.REGISTRY.clear()


def test_api_solver_summaries(tmp_path):
    db = tmp_path / "sum.db"
    init_db(db)
    with patch("basic_restapi.fastapi_app.DB_PATH", db):
        with TestClient(app) as tc:
            store_run(
                RunResult(
                    job_name="j",
                    solver_name="s-mon",
                    system_name="sy",
                    returncode=0,
                    stdout="",
                    stderr="",
                    runtime_seconds=1.0,
                    timestamp="2026-01-01T00:00:00+00:00",
                    passed=True,
                    job_batch_uuid="b",
                )
            )
            r = tc.get("/api/solver_summaries")
            assert r.status_code == 200
            data = r.json()
            assert isinstance(data, list)
            assert any(x.get("solver_name") == "s-mon" for x in data)


def test_api_matrix_presets_crud(tmp_path):
    """GET/PUT/GET/DELETE /api/matrix_presets persist in DB."""
    db = tmp_path / "matrix_presets.db"
    init_db(db)
    with patch("basic_restapi.fastapi_app.DB_PATH", db):
        with TestClient(app) as tc:
            assert tc.get("/api/matrix_presets").json() == []
            put = tc.put(
                "/api/matrix_presets/My-Smoke",
                json={"cells": [{"name": "sol-a", "system": "sys-1"}]},
            )
            assert put.status_code == 200
            data = put.json()
            assert data["label"] == "my-smoke"
            assert data["cells"] == [{"name": "sol-a", "system": "sys-1"}]
            listed = tc.get("/api/matrix_presets").json()
            assert len(listed) == 1
            assert listed[0]["label"] == "my-smoke"
            one = tc.get("/api/matrix_presets/My-Smoke").json()
            assert one["cells"] == [{"name": "sol-a", "system": "sys-1"}]
            assert tc.delete("/api/matrix_presets/my-smoke").status_code == 200
            assert tc.get("/api/matrix_presets/my-smoke").status_code == 404
            assert tc.delete("/api/matrix_presets/nonesuch").status_code == 404
