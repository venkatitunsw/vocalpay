import json
import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from db import get_conn

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def canonical_json(data: dict) -> str:
    """
    Stable JSON so hashing is deterministic (same input => same string).
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def get_last_event_hash(session_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE session_id=? ORDER BY ts DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["event_hash"] if row else None
    finally:
        conn.close()

def append_event(session_id: str, event_type: str, payload: dict) -> dict:
    """
    Append-only audit event.
    Returns the inserted event (ids + hashes).
    """
    event_id = str(uuid4())
    ts = utc_now_iso()

    payload_str = canonical_json(payload)
    prev_hash = get_last_event_hash(session_id)

    # Hash includes prev_hash to form a chain
    chain_input = canonical_json({
        "event_id": event_id,
        "session_id": session_id,
        "ts": ts,
        "event_type": event_type,
        "payload": payload,     # will be canonicalized
        "prev_hash": prev_hash
    })
    event_hash = sha256_hex(chain_input)

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO audit_events (event_id, session_id, ts, event_type, payload_json, prev_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, session_id, ts, event_type, payload_str, prev_hash, event_hash),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "event_id": event_id,
        "session_id": session_id,
        "ts": ts,
        "event_type": event_type,
        "payload": payload,
        "prev_hash": prev_hash,
        "event_hash": event_hash,
    }

def load_session_events(session_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT event_id, session_id, ts, event_type, payload_json, prev_hash, event_hash
            FROM audit_events
            WHERE session_id=?
            ORDER BY ts ASC
            """,
            (session_id,),
        ).fetchall()

        events = []
        for r in rows:
            events.append({
                "event_id": r["event_id"],
                "session_id": r["session_id"],
                "ts": r["ts"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"]),
                "prev_hash": r["prev_hash"],
                "event_hash": r["event_hash"],
            })
        return events
    finally:
        conn.close()

def verify_session_chain(session_id: str) -> dict:
    """
    Recompute each event_hash and ensure:
    - prev_hash matches previous event's hash
    - computed hash matches stored hash
    """
    events = load_session_events(session_id)
    if not events:
        return {"ok": True, "session_id": session_id, "events": 0, "message": "No events"}

    prev = None
    for i, ev in enumerate(events):
        # Check prev_hash linkage
        expected_prev_hash = prev["event_hash"] if prev else None
        if ev["prev_hash"] != expected_prev_hash:
            return {
                "ok": False,
                "session_id": session_id,
                "events": len(events),
                "broken_at_index": i,
                "reason": "prev_hash mismatch",
                "expected_prev_hash": expected_prev_hash,
                "found_prev_hash": ev["prev_hash"],
                "event_id": ev["event_id"],
            }

        # Recompute hash
        chain_input = canonical_json({
            "event_id": ev["event_id"],
            "session_id": ev["session_id"],
            "ts": ev["ts"],
            "event_type": ev["event_type"],
            "payload": ev["payload"],
            "prev_hash": ev["prev_hash"],
        })
        computed = sha256_hex(chain_input)

        if computed != ev["event_hash"]:
            return {
                "ok": False,
                "session_id": session_id,
                "events": len(events),
                "broken_at_index": i,
                "reason": "event_hash mismatch",
                "computed": computed,
                "stored": ev["event_hash"],
                "event_id": ev["event_id"],
            }

        prev = ev

    return {"ok": True, "session_id": session_id, "events": len(events)}
