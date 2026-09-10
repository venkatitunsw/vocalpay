from datetime import datetime, timezone, timedelta
from uuid import uuid4
from db import get_conn


def is_confirmation_expired(conf: dict) -> bool:
    expires_at = datetime.fromisoformat(conf["expires_at"])
    return datetime.now(timezone.utc) >= expires_at

def create_confirmation(txn_id: str, user_id: str, required_confirmation: str, ttl_seconds: int = 120) -> dict:
    confirmation_id = str(uuid4())
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO confirmations
            (confirmation_id, txn_id, user_id, required_confirmation, pin_attempts, max_attempts, expires_at, created_at, status)
            VALUES (?, ?, ?, ?, 0, 3, ?, ?, 'pending')
            """,
            (confirmation_id, txn_id, user_id, required_confirmation, expires_at, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "confirmation_id": confirmation_id,
        "txn_id": txn_id,
        "user_id": user_id,
        "required_confirmation": required_confirmation,
        "expires_at": expires_at,
        "status": "pending",
    }

def get_confirmation(confirmation_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def increment_attempts(confirmation_id: str) -> int:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE confirmations SET pin_attempts = pin_attempts + 1 WHERE confirmation_id=?",
            (confirmation_id,),
        )
        conn.commit()
        row = conn.execute("SELECT pin_attempts FROM confirmations WHERE confirmation_id=?", (confirmation_id,)).fetchone()
        return int(row["pin_attempts"])
    finally:
        conn.close()

def set_confirmation_status(confirmation_id: str, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE confirmations SET status=? WHERE confirmation_id=?", (status, confirmation_id))
        conn.commit()
    finally:
        conn.close()
