import re
from uuid import uuid4
from datetime import datetime, timezone

from db import get_conn

# Demo/mock BPAY billers — BPAY has no Stripe test-mode equivalent, so this
# rail is simulated: validated and audited exactly like every other payment
# (see transactions_repo / audit.py), but settlement is recorded rather than
# sent over a real BPAY network. Seeded once, idempotently, same pattern as
# payid_directory_repo.seed_payid_directory().
_DEMO_BILLERS = [
    ("111999", "Origin Energy", "MOD10"),
    ("222888", "Telstra", "MOD10"),
    ("333777", "City of Melbourne Rates", "MOD10"),
    ("444666", "AGL Energy", "MOD10"),
]


def normalize_crn(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def validate_crn_mod10(crn: str) -> bool:
    """
    BPAY's standard CRN check-digit algorithm (a Luhn/Mod10 variant): the
    last digit is a check digit over the preceding digits, double-weighting
    every second digit from the right and summing digit-by-digit (classic
    Luhn). Returns False for anything too short to carry a check digit.
    """
    digits = normalize_crn(crn)
    if len(digits) < 2:
        return False

    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i == 0:
            total += d  # the check digit itself
            continue
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def seed_bpay_directory() -> None:
    conn = get_conn()
    try:
        existing = conn.execute("SELECT COUNT(*) AS n FROM bpay_directory").fetchone()["n"]
        if existing >= len(_DEMO_BILLERS):
            return
        conn.executemany(
            "INSERT INTO bpay_directory (biller_code, biller_name, crn_rule) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            _DEMO_BILLERS,
        )
        conn.commit()
    finally:
        conn.close()


def lookup_biller(biller_code: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bpay_directory WHERE biller_code=?", (biller_code.strip(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_biller_by_name(name: str) -> dict | None:
    """Resolves a free-text biller name (e.g. "Origin" from "Pay Origin BPAY
    110...") against the directory — exact first, then substring."""
    if not name:
        return None
    conn = get_conn()
    try:
        exact = conn.execute(
            "SELECT * FROM bpay_directory WHERE LOWER(biller_name)=LOWER(?)", (name.strip(),)
        ).fetchone()
        if exact:
            return dict(exact)
        rows = conn.execute("SELECT * FROM bpay_directory").fetchall()
    finally:
        conn.close()
    name_norm = name.strip().lower()
    for row in rows:
        if name_norm in row["biller_name"].lower() or row["biller_name"].lower().split()[0] == name_norm.split()[0]:
            return dict(row)
    return None


def find_saved_biller_by_nickname(user_id: str, nickname: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM saved_bpay_billers WHERE user_id=? AND LOWER(nickname)=LOWER(?)",
            (user_id, nickname.strip()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_biller(user_id: str, nickname: str, biller_code: str, crn: str) -> str:
    biller_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO saved_bpay_billers (biller_id, user_id, nickname, biller_code, crn, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (biller_id, user_id, nickname.strip(), biller_code.strip(), normalize_crn(crn), now),
        )
        conn.commit()
    finally:
        conn.close()
    return biller_id


def get_saved_biller(biller_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM saved_bpay_billers WHERE biller_id=?", (biller_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_saved_billers(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM saved_bpay_billers WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
