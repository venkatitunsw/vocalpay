import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import db as db_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """
    Fresh, isolated SQLite DB per test. db.get_conn() reads db.DB_PATH at
    call time, so monkeypatching the module attribute redirects every repo
    function without touching their code.
    """
    db_file = tmp_path / "test_vocalpay.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_file)

    import main  # noqa: PLC0415 (import after DB_PATH patch, before app runs startup)

    with TestClient(main.app) as c:
        yield c
