from db import get_conn
from payid_directory_repo import normalize_payid


def find_payee_by_phone(user_id: str, raw_number: str) -> dict | None:
    """
    Resolves a PayID/phone number typed as the payment target against this
    user's own saved contacts, comparing digits-only so formatting
    ('0400 777 888' vs '0400777888') doesn't matter.
    """
    digits = normalize_payid(raw_number)
    if not digits:
        return None
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM payees WHERE user_id=? AND phone_number IS NOT NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        if normalize_payid(row["phone_number"]) == digits:
            return dict(row)
    return None


def payee_exists_for_user(user_id: str, nickname: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM payees WHERE user_id=? AND LOWER(nickname)=LOWER(?) LIMIT 1",
            (user_id, nickname),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def find_saved_contact_by_nickname(user_id: str, nickname: str) -> dict | None:
    """
    Exact, case-insensitive match among the user's *deliberately saved*
    contacts only (is_contact=1) — used to detect a name collision before
    saving a new contact, e.g. two different PayIDs both registered as
    "Ava Hill". Auto-provisioned (unsaved) rows don't count as a collision
    here since they aren't a contact the user has committed to yet.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM payees WHERE user_id=? AND is_contact=1 AND LOWER(nickname)=LOWER(?) LIMIT 1",
            (user_id, nickname.strip()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _group_anchor_id(payee: dict) -> str:
    """Every payee in a "same person, different number" group points at one
    anchor payee_id (the first one saved) — either its own id (it IS the
    anchor) or its linked_contact_id (it points at the anchor)."""
    return payee.get("linked_contact_id") or payee["payee_id"]


def _resolve_name_matches(matches: list[dict]) -> list[dict]:
    """
    Multiple rows sharing a nickname are only genuinely ambiguous if the user
    hasn't already told us they're the same person. If every match belongs to
    the same linked-contact group, collapse them to one representative
    (preferring a saved contact, then the group's anchor row) instead of
    forcing a CLARIFY every time.
    """
    if len(matches) <= 1:
        return matches
    anchors = {_group_anchor_id(m) for m in matches}
    if len(anchors) != 1:
        return matches  # genuinely different identities (or not yet resolved) -> stays ambiguous

    saved = [m for m in matches if m.get("is_contact")]
    if len(saved) == 1:
        return [saved[0]]
    anchor_rows = [m for m in matches if not m.get("linked_contact_id")]
    return [anchor_rows[0]] if anchor_rows else [matches[0]]


def find_payees_by_name(user_id: str, name: str) -> list[dict]:
    """
    Resolves a spoken/typed name to saved payees, the way a phone contacts
    lookup would: an exact nickname match always wins outright; otherwise
    fall back to matching the first word of the nickname (so "Alice" finds
    "Alice Wonderland"), then to a substring match anywhere in the nickname.
    Each tier is tried only if the previous one found nothing. More than one
    match at a tier is only returned as-is (ambiguous) if they aren't already
    linked as the same person — see _resolve_name_matches().
    """
    name_norm = name.strip().lower()
    conn = get_conn()
    try:
        exact = conn.execute(
            "SELECT * FROM payees WHERE user_id=? AND LOWER(nickname)=?",
            (user_id, name_norm),
        ).fetchall()
        if exact:
            return _resolve_name_matches([dict(r) for r in exact])

        rows = [dict(r) for r in conn.execute("SELECT * FROM payees WHERE user_id=?", (user_id,)).fetchall()]
    finally:
        conn.close()

    first_word_matches = [r for r in rows if r["nickname"].strip().lower().split()[0] == name_norm]
    if first_word_matches:
        return _resolve_name_matches(first_word_matches)

    return _resolve_name_matches([r for r in rows if name_norm in r["nickname"].lower()])


def get_payee(payee_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM payees WHERE payee_id=?", (payee_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
