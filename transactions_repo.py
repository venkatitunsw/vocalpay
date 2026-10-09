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


def create_pending_bpay_transaction(
    session_id: str,
    user_id: str,
    amount_cents: int,
    currency: str,
    biller_code: str,
    crn: str,
) -> str:
    """Same lifecycle as create_pending_transaction (confirmation -> execute
    -> succeeded/failed), but for the BPAY rail: no payee_id (there's no
    saved-contact concept for a biller), biller_code/crn recorded instead."""
    txn_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO transactions
            (txn_id, session_id, user_id, amount_cents, currency, payee_id, status,
             stripe_payment_intent_id, rail, bpay_biller_code, bpay_crn, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, 'bpay', ?, ?, ?)
            """,
            (txn_id, session_id, user_id, amount_cents, currency, "created", biller_code, crn, now),
        )
        conn.commit()
        return txn_id
    finally:
        conn.close()

def create_pending_service_transaction(session_id: str, user_id: str, amount_cents: int, currency: str, invoice_id: str) -> str:
    """Same lifecycle as the BPAY rail, for an open service-provider invoice (rail='service')."""
    txn_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO transactions
            (txn_id, session_id, user_id, amount_cents, currency, payee_id, status,
             stripe_payment_intent_id, rail, service_invoice_id, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, 'service', ?, ?)
            """,
            (txn_id, session_id, user_id, amount_cents, currency, "created", invoice_id, now),
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


def list_recent_transactions(user_id: str, limit: int = 5) -> list[dict]:
    """Most recent transactions for a user, with the payee's nickname joined
    in — used by the support chatbot (support_chat.py) to answer questions
    like "what was my last payment" without it inventing an answer."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT t.txn_id, t.amount_cents, t.currency, t.status, t.stripe_payment_intent_id,
                   t.created_at, p.nickname AS payee_nickname
            FROM transactions t
            LEFT JOIN payees p ON p.payee_id = t.payee_id
            WHERE t.user_id=?
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def spending_summary(user_id: str, days: int = 30) -> dict:
    """
    Total of everything actually paid (status='succeeded' -- a pending or
    failed attempt never moved money, so it doesn't count as spending) in
    the last `days`, broken down by rail. Used by the support chatbot's
    transactions specialist to answer "how much have I spent this month"
    without it inventing a number -- a real SUM over the real ledger, not
    an LLM's guess.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT rail, COUNT(*) AS n, SUM(amount_cents) AS total_cents, currency
            FROM transactions
            WHERE user_id=? AND status='succeeded' AND created_at>=?
            GROUP BY rail, currency
            """,
            (user_id, cutoff),
        ).fetchall()
        by_rail = [dict(r) for r in rows]
        total_cents = sum(r["total_cents"] for r in by_rail)
        count = sum(r["n"] for r in by_rail)
        return {"days": days, "total_cents": total_cents, "count": count, "by_rail": by_rail}
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


def find_recent_duplicate(user_id: str, payee_id: str, amount_cents: int, within_minutes: int = 5) -> dict | None:
    """
    The most recent payment to this exact payee for this exact amount, if one
    was created within the last `within_minutes` -- regardless of its status,
    since even a since-failed attempt means the user just tried this and it's
    worth asking "did you mean to send it again?" rather than silently
    letting a second identical payment through. Used as a safety prompt
    (CLARIFY), never a hard block -- re-sending the same amount to the same
    person twice in a row is unusual but not inherently wrong (a genuine
    split bill, a correction, etc.).
    """
    if not payee_id:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT txn_id, status, created_at FROM transactions
            WHERE user_id=? AND payee_id=? AND amount_cents=? AND created_at>=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, payee_id, amount_cents, cutoff),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
