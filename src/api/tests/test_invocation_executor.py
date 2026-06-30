"""Tests for invocation executor configuration."""

import basic_restapi.invocations as inv_mod


def test_max_workers_from_env_var(monkeypatch):
    """HPC_MAX_BACKGROUND_WORKERS env var controls executor pool size."""
    monkeypatch.setattr(inv_mod, "_executor", None)
    monkeypatch.setenv("HPC_MAX_BACKGROUND_WORKERS", "4")
    executor = inv_mod._get_executor()
    assert executor._max_workers == 4
    executor.shutdown(wait=False)


def test_max_workers_uses_default_without_env_var(monkeypatch):
    """Without env var, uses _DEFAULT_MAX_BACKGROUND_WORKERS."""
    monkeypatch.setattr(inv_mod, "_executor", None)
    monkeypatch.delenv("HPC_MAX_BACKGROUND_WORKERS", raising=False)
    executor = inv_mod._get_executor()
    assert executor._max_workers == inv_mod._DEFAULT_MAX_BACKGROUND_WORKERS
    executor.shutdown(wait=False)
