"""Shared fixtures for API tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harness import init_db
from harness.storage.db import db_writer_session

from basic_restapi.fastapi_app import app


@pytest.fixture
def api_client(tmp_path: Path):
    """TestClient with a properly isolated DB writer.

    Starts the writer for the tmp_path DB *before* entering the TestClient
    lifespan, and patches start_writer/stop_writer to no-ops so the lifespan
    doesn't start a second writer for a different path.
    """
    db = tmp_path / "test.db"
    init_db(db)
    with db_writer_session(db):
        with patch("basic_restapi.fastapi_app.DB_PATH", db):
            with patch("basic_restapi.fastapi_app.start_writer"):
                with patch("basic_restapi.fastapi_app.stop_writer"):
                    with TestClient(app) as tc:
                        yield tc, db
