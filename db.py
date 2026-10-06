import os
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path("db") / "schema.sql"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://vocalpay:vocalpay@localhost:5433/vocalpay",  # docker-compose.yml
)

# Tests point this at a throwaway schema per test; None means the default search_path.
DB_SCHEMA = None

_QMARK_PARAM = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


def _to_pg_sql(sql: str) -> str:
    return _QMARK_PARAM.sub("%s", sql)


class _Conn:
    """
    Thin wrapper over a psycopg connection exposing the execute / executemany /
    executescript / commit / close surface the repo files already use. Repos keep
    writing `?` placeholders; they are rewritten to `%s` here.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        return self._raw.execute(_to_pg_sql(sql), tuple(params) if params else None)

    def executemany(self, sql, seq_of_params):
        with self._raw.cursor() as cur:
            cur.executemany(_to_pg_sql(sql), [tuple(p) for p in seq_of_params])

    def executescript(self, sql):
        self._raw.execute(sql)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def get_conn():
    kwargs = {"row_factory": dict_row}
    if DB_SCHEMA:
        kwargs["options"] = f"-c search_path={DB_SCHEMA}"
    return _Conn(psycopg.connect(DATABASE_URL, **kwargs))


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
        "rail": "TEXT NOT NULL DEFAULT 'payid'",
        "bpay_biller_code": "TEXT",
        "bpay_crn": "TEXT",
        "service_invoice_id": "TEXT",
    },
    "confirmations": {
        "challenge": "TEXT",
    },
}


def _run_migrations(conn) -> None:
    for table, columns in _MIGRATIONS.items():
        for column, col_type in columns.items():
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
    conn.execute(
        "INSERT INTO payee_payids (payid_id, payee_id, user_id, pay_id_type, value_normalized, is_primary, created_at) "
        "SELECT payee_id || ':mobile', payee_id, user_id, 'mobile', regexp_replace(phone_number, '[^0-9]', '', 'g'), 1, created_at "
        "FROM payees WHERE phone_number IS NOT NULL AND phone_number <> '' "
        "ON CONFLICT DO NOTHING"
    )


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
