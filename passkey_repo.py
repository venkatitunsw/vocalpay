from datetime import datetime, timezone
import json

from db import get_conn


def save_credential(
    user_id: str,
    credential_id: str,
    public_key_cbor: bytes,
    sign_count: int,
    transports: list[str] | None,
    label: str | None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO passkey_credentials
            (credential_id, user_id, public_key_cbor, sign_count, transports_json, label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential_id, user_id, public_key_cbor, sign_count,
                json.dumps(transports or []), label, datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_credential(credential_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM passkey_credentials WHERE credential_id=?", (credential_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_credentials(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT credential_id, label, transports_json, created_at FROM passkey_credentials "
            "WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_sign_count(credential_id: str, new_count: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE passkey_credentials SET sign_count=? WHERE credential_id=?", (new_count, credential_id)
        )
        conn.commit()
    finally:
        conn.close()
