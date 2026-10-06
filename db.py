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

# Tests set this to a unique prefix per test so every domain schema is throwaway.
# None means production names: payments, directory, services, passkeys, memory.
DB_SCHEMA = None

DOMAIN_TABLES = {
    "payments": [
        "users", "payees", "payee_payids", "payment_methods", "transactions", "audit_events",
        "confirmations", "saved_bpay_billers", "recurring_schedules",
    ],
    "directory": ["payid_directory", "bpay_directory"],
    "services": ["service_providers", "service_invoices"],
    "passkeys": ["passkey_credentials"],
    "memory": [],  # LangGraph's PostgresSaver creates its own tables here
}
TABLE_DOMAIN = {table: domain for domain, tables in DOMAIN_TABLES.items() for table in tables}

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")
_CREATE_INDEX_RE = re.compile(r"(CREATE INDEX IF NOT EXISTS \w+ ON )(\w+)(\s*\()")
_QMARK_PARAM = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


def domain_schema(domain: str) -> str:
    return f"{DB_SCHEMA}_{domain}" if DB_SCHEMA else domain


def search_path() -> str:
    return ",".join([domain_schema(d) for d in DOMAIN_TABLES] + ["public"])


def _to_pg_sql(sql: str) -> str:
    return _QMARK_PARAM.sub("%s", sql)


def _qualify_ddl(sql: str) -> str:
    def create_table(m):
        table = m.group(1)
        return f"CREATE TABLE IF NOT EXISTS {domain_schema(TABLE_DOMAIN[table])}.{table}"

    def create_index(m):
        table = m.group(2)
        return f"{m.group(1)}{domain_schema(TABLE_DOMAIN[table])}.{table}{m.group(3)}"

    sql = _CREATE_TABLE_RE.sub(create_table, sql)
    return _CREATE_INDEX_RE.sub(create_index, sql)


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
    return _Conn(psycopg.connect(DATABASE_URL, row_factory=dict_row, options=f"-c search_path={search_path()}"))


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
    "payid_directory": {
        "email": "TEXT",
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


def _move_legacy_public_tables(conn) -> None:
    """Databases created before domain schemas existed keep their tables in public; move them, data intact."""
    for table, domain in TABLE_DOMAIN.items():
        if conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()["t"] is None:
            continue
        if conn.execute("SELECT to_regclass(%s) AS t", (f"{domain_schema(domain)}.{table}",)).fetchone()["t"] is not None:
            continue
        conn.execute(f"ALTER TABLE public.{table} SET SCHEMA {domain_schema(domain)}")


def init_db() -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Missing schema file: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_conn()
    try:
        for domain in DOMAIN_TABLES:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{domain_schema(domain)}"')
        if DB_SCHEMA is None:
            _move_legacy_public_tables(conn)
        conn.executescript(_qualify_ddl(schema_sql))
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()
