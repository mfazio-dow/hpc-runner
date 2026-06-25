"""Run Matrix preset storage (SQLite)."""

import pytest

from harness.storage.db import (
    init_db,
    db_writer_session,
    delete_matrix_preset,
    get_matrix_preset,
    list_matrix_presets,
    upsert_matrix_preset,
)


@pytest.fixture(autouse=True)
def _db_writer(tmp_path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    with db_writer_session(db_path):
        yield db_path


def test_matrix_presets_upsert_list_get_delete(tmp_path):
    db = tmp_path / "p.db"
    cells = [{"name": "s1", "system": "sys-a"}, {"name": "s2", "system": "sys-b"}]
    upsert_matrix_preset("Nightly", cells)

    rows = list_matrix_presets(db)
    assert len(rows) == 1
    assert rows[0]["label"] == "nightly"
    assert rows[0]["cells"] == cells
    assert rows[0]["updated_at"]

    one = get_matrix_preset(db, "NIGHTLY")
    assert one is not None
    assert one["label"] == "nightly"
    assert one["cells"] == cells

    assert delete_matrix_preset("nightly") == 1
    assert get_matrix_preset(db, "nightly") is None
    assert list_matrix_presets(db) == []


def test_matrix_presets_replace(tmp_path):
    db = tmp_path / "p.db"
    upsert_matrix_preset("x", [{"name": "a", "system": "b"}])
    upsert_matrix_preset("X", [{"name": "c", "system": "d"}])
    assert len(list_matrix_presets(db)) == 1
    assert get_matrix_preset(db, "x")["cells"] == [{"name": "c", "system": "d"}]


def test_matrix_presets_empty_label_delete():
    assert delete_matrix_preset("   ") == 0
