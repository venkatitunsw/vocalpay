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


def init_db() -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Missing schema file: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_conn()
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()
