from datetime import datetime, timezone
from uuid import uuid4

from db import get_conn
from payid_validation import classify_payid


def add_payid(user_id: str, payee_id: str, raw: str, label: str | None = None) -> dict:
    """Validates and stores a PayID for a contact. Returns ok/error and, on a clash, who already holds it."""
    try:
        pay_id_type, value = classify_payid(raw)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    conn = get_conn()
    try:
        owner = conn.execute(
            "SELECT p.payee_id, p.nickname FROM payee_payids pp JOIN payees p ON p.payee_id = pp.payee_id "
            "WHERE pp.value_normalized=?",
            (value,),
        ).fetchone()
        if owner:
            if owner["payee_id"] == payee_id:
                return {"ok": False, "error": f"{value} is already saved to this contact."}
            return {
                "ok": False,
                "error": f"{value} already belongs to {owner['nickname']}. A PayID can only be saved to one contact.",
                "conflict": {"payee_id": owner["payee_id"], "nickname": owner["nickname"]},
            }

        existing = conn.execute("SELECT COUNT(*) AS n FROM payee_payids WHERE payee_id=?", (payee_id,)).fetchone()["n"]
        payid_id = str(uuid4())
        conn.execute(
            "INSERT INTO payee_payids (payid_id, payee_id, user_id, pay_id_type, value_normalized, label, is_primary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (payid_id, payee_id, user_id, pay_id_type, value, label, 1 if existing == 0 else 0,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"ok": True, "payid_id": payid_id, "pay_id_type": pay_id_type, "value": value}
    finally:
        conn.close()


def list_payids(payee_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM payee_payids WHERE payee_id=? ORDER BY is_primary DESC, created_at", (payee_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_payee_by_payid_value(user_id: str, value: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT p.* FROM payee_payids pp JOIN payees p ON p.payee_id = pp.payee_id "
            "WHERE pp.user_id=? AND pp.value_normalized=?",
            (user_id, value),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
