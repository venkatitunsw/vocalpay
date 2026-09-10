import os
import sqlite3
from pathlib import Path

DB_PATH = Path("vocalpay.db")
SCHEMA_PATH = Path("db") / "schema.sql"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

_MIGRATIONS = {
    "payees": {
        "phone_number": "TEXT",
        "stripe_connected_account_id": "TEXT",
        "is_contact": "INTEGER NOT NULL DEFAULT 1",
        "linked_contact_id": "TEXT",
    },
    "transactions": {
        "stripe_transfer_id": "TEXT",
        "destination_account_id": "TEXT",
        "receiver_confirmed_at": "TEXT",
    },
}


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    CREATE TABLE IF NOT EXISTS in schema.sql only helps brand-new databases.
    For an already-existing vocalpay.db, add any columns introduced since it
    was created. Safe to run every startup: it only adds columns that are
    still missing.
    """
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, col_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db() -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Missing schema file: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_conn()
    try:
        conn.executescript(schema_sql)
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()
