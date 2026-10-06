import re

from db import get_conn

# Deterministic name pools used to generate a 200-entry mock PayID directory.
# This simulates an external bank-network registry: any number in here is a
# "real" PayID with a registered name, independent of whether the current
# user has saved that person as a contact.
_FIRST_NAMES = [
    "Olivia", "Liam", "Emma", "Noah", "Amelia", "Jack", "Charlotte", "William",
    "Mia", "James", "Isla", "Benjamin", "Ava", "Lucas", "Grace", "Henry",
    "Chloe", "Oliver", "Zoe", "Ethan", "Ruby", "Alexander", "Lily", "Mason",
    "Ella", "Samuel", "Sophia", "Thomas", "Harper", "Daniel",
]
_LAST_NAMES = [
    "Smith", "Jones", "Williams", "Brown", "Wilson", "Taylor", "Nguyen",
    "Anderson", "Lee", "Martin", "Clarke", "Walker", "Harris", "Young",
    "King", "Wright", "Hill", "Baker", "Chen", "Patel",
]


def normalize_payid(raw: str) -> str:
    """Strip everything but digits, so '0400 777 888' and '0400777888' match."""
    return re.sub(r"\D", "", raw or "")


def looks_like_payid(raw: str) -> bool:
    """A PayID here is an Australian-mobile-shaped number: 8-10 digits once
    spaces/dashes are stripped, after removing a leading '+61'/'0'."""
    digits = normalize_payid(raw)
    return 8 <= len(digits) <= 10


def _format_display(digits: str) -> str:
    # e.g. "0412345678" -> "0412 345 678"
    if len(digits) == 10:
        return f"{digits[0:4]} {digits[4:7]} {digits[7:10]}"
    return digits


def seed_payid_directory(count: int = 200) -> None:
    """Idempotent: safe to run on every startup. Fills in an email for each entry that lacks one."""
    conn = get_conn()
    try:
        rows = []
        seen = set()
        # Deterministic pseudo-random-looking mobile numbers: 04xx xxx xxx,
        # derived from an index so re-seeding always produces the same set.
        i = 0
        while len(rows) < count:
            i += 1
            digits = f"04{(17 * i + 23) % 100:02d}{(41 * i + 7) % 1000:03d}{(97 * i + 511) % 1000:03d}"
            if digits in seen:
                continue
            seen.add(digits)
            first = _FIRST_NAMES[i % len(_FIRST_NAMES)]
            last = _LAST_NAMES[(i * 3) % len(_LAST_NAMES)]
            email = f"{first.lower()}.{last.lower()}{i}@example.com"
            rows.append((digits, _format_display(digits), f"{first} {last}", email))

        conn.executemany(
            "INSERT INTO payid_directory (phone_number, display_number, registered_name, email) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (phone_number) DO UPDATE SET email = COALESCE(payid_directory.email, EXCLUDED.email)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def lookup_payid(raw_number: str) -> dict | None:
    digits = normalize_payid(raw_number)
    if not digits:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT phone_number, display_number, registered_name, email FROM payid_directory WHERE phone_number=?",
            (digits,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def lookup_payid_email(raw_email: str) -> dict | None:
    email = (raw_email or "").strip().lower()
    if not email:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT phone_number, display_number, registered_name, email FROM payid_directory WHERE LOWER(email)=?",
            (email,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
