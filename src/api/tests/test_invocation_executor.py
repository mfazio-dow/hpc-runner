"""Tests for invocation executor configuration."""

from unittest.mock import patch

import basic_restapi.invocations as inv_mod


def test_max_workers_from_env_var():
    """HPC_MAX_BACKGROUND_WORKERS env var controls executor pool size."""
    # Ensure no cached executor
    inv_mod._executor = None
    try:
        with patch.dict("os.environ", {"HPC_MAX_BACKGROUND_WORKERS": "4"}):
            executor = inv_mod._get_executor()
            assert executor._max_workers == 4
    finally:
        inv_mod._executor = None


def test_max_workers_uses_default_without_env_var():
    """Without env var, uses _DEFAULT_MAX_BACKGROUND_WORKERS."""
    inv_mod._executor = None
    try:
        with patch.dict("os.environ", {}, clear=False):
            # Remove the var if present
            import os

            os.environ.pop("HPC_MAX_BACKGROUND_WORKERS", None)
            executor = inv_mod._get_executor()
            assert executor._max_workers == inv_mod._DEFAULT_MAX_BACKGROUND_WORKERS
    finally:
        inv_mod._executor = None
