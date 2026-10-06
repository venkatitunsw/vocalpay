import re
from datetime import date, timedelta

from db import get_conn

# Mock service providers. Settlement is simulated (see /services/execute), so
# these are demo data, not real billers.
_PROVIDERS = [
    ("prov-sydney-water", "Sydney Water", "utility", "indigo"),
    ("prov-agl", "AGL Energy", "utility", "indigo"),
    ("prov-telstra", "Telstra", "telco", "indigo"),
    ("prov-dr-smith", "Dr Smith Dental", "health", "indigo"),
    ("prov-opal", "Opal", "transport", "indigo"),
]

# (invoice_id, provider_id, customer_ref, amount_cents, days_until_due)
_INVOICES = [
    ("inv-water-q3", "prov-sydney-water", "SW-449120", 13250, 14),
    ("inv-agl-oct", "prov-agl", "AGL-88230114", 18990, 9),
    ("inv-telstra-oct", "prov-telstra", "TEL-5521073", 6500, 21),
    ("inv-dental-check", "prov-dr-smith", "DS-0912", 12000, 30),
    ("inv-opal-top", "prov-opal", "OPAL-7741", 2840, 5),
]


def seed_services(user_id: str) -> None:
    conn = get_conn()
    try:
        conn.executemany(
            "INSERT INTO service_providers (provider_id, name, category, accent) VALUES (?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            _PROVIDERS,
        )
        today = date.today()
        conn.executemany(
            "INSERT INTO service_invoices (invoice_id, provider_id, user_id, customer_ref, amount_cents, "
            "currency, due_date, status, created_at) VALUES (?, ?, ?, ?, ?, 'AUD', ?, 'open', ?) "
            "ON CONFLICT DO NOTHING",
            [
                (inv_id, prov_id, user_id, ref, cents, (today + timedelta(days=days)).isoformat(), today.isoformat())
                for inv_id, prov_id, ref, cents, days in _INVOICES
            ],
        )
        conn.commit()
    finally:
        conn.close()


def list_providers() -> list[dict]:
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM service_providers ORDER BY name").fetchall()]
    finally:
        conn.close()


def list_invoices(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT i.*, p.name AS provider_name, p.category, p.accent FROM service_invoices i "
            "JOIN service_providers p ON p.provider_id = i.provider_id "
            "WHERE i.user_id=? ORDER BY i.due_date",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_invoice(invoice_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT i.*, p.name AS provider_name, p.accent FROM service_invoices i "
            "JOIN service_providers p ON p.provider_id = i.provider_id WHERE i.invoice_id=?",
            (invoice_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_open_invoice(user_id: str, text: str) -> dict | None:
    """Matches a provider named in free text (e.g. 'my Telstra bill') to that provider's open invoice."""
    lowered = text.lower()
    for invoice in list_invoices(user_id):
        if invoice["status"] != "open":
            continue
        name = invoice["provider_name"].lower()
        first_word = name.split()[0]
        if re.search(rf"\b{re.escape(name)}\b", lowered) or (len(first_word) >= 3 and re.search(rf"\b{re.escape(first_word)}\b", lowered)):
            return invoice
    return None


def mark_invoice_paid(invoice_id: str, txn_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE service_invoices SET status='paid', paid_txn_id=? WHERE invoice_id=?",
            (txn_id, invoice_id),
        )
        conn.commit()
    finally:
        conn.close()
