import os
import sqlite3
from pathlib import Path

DB_PATH = Path("vocalpay.db")
SCHEMA_PATH = Path("db") / "schema.sql"

# When set (production/Render), storage moves from the local SQLite file to a
# Turso (libSQL) database, so data survives redeploys and cold starts instead
# of resetting with the container's ephemeral disk. Unset locally/in tests,
# where the local SQLite file behaves exactly as before.
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

_turso_client = None


def _get_turso_client():
    """
    One shared libsql client for the whole process. Each client owns a
    background thread + event loop, so creating a fresh one per call (the way
    sqlite3.connect() is cheap to do locally) would be wasteful and slow —
    reuse it instead, the same way a real connection pool would.
    """
    global _turso_client
    if _turso_client is None:
        import libsql_client
        _turso_client = libsql_client.create_client_sync(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    return _turso_client


class _LibsqlCursor:
    """
    Adapts a libsql ResultSet to the sqlite3-cursor shape this project relies
    on everywhere: .fetchone()/.fetchall() returning dict-like rows, so
    `dict(row)` and `row["col"]` keep working unchanged in every repo file.
    """

    def __init__(self, result_set):
        self._rows = [row.asdict() for row in result_set.rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _LibsqlConn:
    """
    Adapts the shared libsql client to the small slice of sqlite3.Connection
    this codebase actually uses (execute/executemany/executescript/commit/
    close), so every repo file works unchanged whether get_conn() returns a
    local SQLite connection (dev/tests) or this Turso-backed wrapper (prod).
    commit()/close() are no-ops: libsql commits each statement as it runs,
    and the underlying client is a shared, long-lived process resource
    rather than something opened and torn down per call.
    """

    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        return _LibsqlCursor(self._client.execute(sql, tuple(params) if params else None))

    def executemany(self, sql, seq_of_params):
        statements = [(sql, tuple(p)) for p in seq_of_params]
        if statements:
            self._client.batch(statements)

    def executescript(self, sql):
        # sqlite3.executescript() splits multi-statement SQL itself; libsql
        # needs each statement executed individually. Strip `--` line
        # comments *before* splitting on ";" — schema.sql's comments
        # sometimes contain a literal ";" themselves (e.g. "1 = saved
        # contact; 0 = auto-provisioned"), which would otherwise fool a
        # naive split into cutting a statement in the wrong place.
        cleaned_lines = []
        for line in sql.splitlines():
            idx = line.find("--")
            cleaned_lines.append(line[:idx] if idx != -1 else line)
        cleaned_sql = "\n".join(cleaned_lines)

        for statement in cleaned_sql.split(";"):
            statement = statement.strip()
            if statement:
                self._client.execute(statement)

    def commit(self):
        pass

    def close(self):
        pass


def get_conn():
    """
    Returns a connection-like object with a consistent execute/fetchone/
    fetchall/commit/close interface — a real sqlite3.Connection locally, or a
    thin wrapper around a shared Turso client in production (TURSO_DATABASE_URL
    set). Every caller in this codebase already only relies on that shared
    interface, so nothing else needs to change based on which backend is active.
    """
    if TURSO_DATABASE_URL:
        return _LibsqlConn(_get_turso_client())
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


def _run_migrations(conn) -> None:
    """
    CREATE TABLE IF NOT EXISTS in schema.sql only helps brand-new databases.
    For an already-existing database (local file or Turso), add any columns
    introduced since it was created. Safe to run every startup: it only adds
    columns that are still missing.
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
