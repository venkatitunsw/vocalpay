from db import get_conn

def get_default_payment_method(user_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT pm_id, user_id, label, stripe_customer_id, stripe_payment_method_id, is_default
            FROM payment_methods
            WHERE user_id=? AND is_default=1
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def set_default_payment_method(user_id: str, pm_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE payment_methods SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE payment_methods SET is_default=1 WHERE user_id=? AND pm_id=?", (user_id, pm_id))
        conn.commit()
    finally:
        conn.close()
