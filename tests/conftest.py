import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
import pytest
from fastapi.testclient import TestClient

import db as db_module
import support_chat


@pytest.fixture()
def client(monkeypatch):
    """
    Fresh Postgres schema per test. Every get_conn() call sets search_path to
    this schema, so tables and seeds never leak between tests. The schema is
    dropped afterwards.
    """
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(db_module.DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(db_module, "DB_SCHEMA", schema)

    import main  # noqa: PLC0415 (import after DB_SCHEMA patch, before app startup)

    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        support_chat.reset_graph()
        with psycopg.connect(db_module.DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
