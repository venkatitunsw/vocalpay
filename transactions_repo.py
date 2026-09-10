from datetime import datetime, timezone, timedelta
from uuid import uuid4

from db import get_conn

def create_pending_transaction(
    session_id: str,
    user_id: str,
    amount_cents: int,
    currency: str,
    payee_id: str | None,
) -> str:
    txn_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO transactions
            (txn_id, session_id, user_id, amount_cents, currency, payee_id, status, stripe_payment_intent_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, session_id, user_id, amount_cents, currency, payee_id, "created", None, now),
        )
        conn.commit()
        return txn_id
    finally:
        conn.close()

def update_transaction_status(txn_id: str, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE transactions SET status=? WHERE txn_id=?", (status, txn_id))
        conn.commit()
    finally:
        conn.close()

def get_transaction(txn_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (txn_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

from db import get_conn

def set_stripe_payment_intent(txn_id: str, payment_intent_id: str, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE transactions SET stripe_payment_intent_id=?, status=? WHERE txn_id=?",
            (payment_intent_id, status, txn_id),
        )
        conn.commit()
    finally:
        conn.close()

def set_receiver_evidence(
    txn_id: str,
    destination_account_id: str,
    stripe_transfer_id: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE transactions
            SET destination_account_id=?, stripe_transfer_id=?, receiver_confirmed_at=?
            WHERE txn_id=?
            """,
            (destination_account_id, stripe_transfer_id, now, txn_id),
        )
        conn.commit()
    finally:
        conn.close()
