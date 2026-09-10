from db import get_conn
from datetime import datetime, timezone
from security import hash_pin

DEMO_USER_ID = "demo-user"
DEMO_USER_PIN = "1234"  # demo only (document this!)

def ensure_demo_user() -> str:
    conn = get_conn()
    try:
        row = conn.execute("SELECT user_id, pin_hash FROM users WHERE user_id=?", (DEMO_USER_ID,)).fetchone()
        if row:
            # If pin_hash missing, set it
            if row["pin_hash"] is None:
                conn.execute(
                    "UPDATE users SET pin_hash=? WHERE user_id=?",
                    (hash_pin(DEMO_USER_PIN), DEMO_USER_ID),
                )
                conn.commit()
            return DEMO_USER_ID

        conn.execute(
            "INSERT INTO users (user_id, name, pin_hash, created_at) VALUES (?, ?, ?, ?)",
            (DEMO_USER_ID, "Demo User", hash_pin(DEMO_USER_PIN), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return DEMO_USER_ID
    finally:
        conn.close()
