from datetime import datetime, timedelta, timezone

import db as db_module
import main
import support_chat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from intent_parser import parse_text_command


def _new_session(client):
    r = client.post("/session/new")
    assert r.status_code == 200
    return r.json()["session_id"]


def _add_payee(client, nickname="John"):
    """Legacy bare payee — no PayID/receiver account. Kept only to test that
    the policy engine blocks payment to payees without receiver tracking."""
    r = client.post("/payees/add", json={"nickname": nickname})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def _add_contact(client, nickname="John", phone_number="0400 000 111"):
    """A real contact: payee + mocked Stripe Connect account, so payments to
    them are allowed under the "must resolve to a PayID" policy."""
    r = client.post("/payees/add_contact", json={"nickname": nickname, "phone_number": phone_number})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def _add_default_payment_method(client, pm_id="pm_test_123"):
    r = client.post(
        "/payment_methods/add",
        json={"label": "Test Card", "stripe_payment_method_id": pm_id},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def _fake_payment_intent(**kwargs):
    return {"id": "pi_fake_123", "status": "succeeded"}


# --- Happy path: parse -> normal confirm -> execute -> audit verifies ---

def test_happy_path_full_flow(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    monkeypatch.setattr(main, "create_payment_intent", _fake_payment_intent)

    session_id = _new_session(client)
    _add_contact(client, "John")
    _add_default_payment_method(client)

    r = client.post(
        "/command/text",
        json={"session_id": session_id, "text": "Pay 12 to John for dinner"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["decision"]["decision"] == "PROCEED"
    assert body["decision"]["required_confirmation"] == "normal"
    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]

    r = client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"},
    )
    conf_body = r.json()
    assert conf_body["ok"] is True
    assert conf_body["status"] == "confirmed"

    r = client.post("/pay/execute", json={"session_id": session_id, "txn_id": txn_id})
    pay_body = r.json()
    assert pay_body["ok"] is True
    assert pay_body["final_status"] == "succeeded"
    assert pay_body["stripe_payment_intent_id"] == "pi_fake_123"

    r = client.get(f"/audit/{session_id}/verify")
    verify_body = r.json()
    assert verify_body["ok"] is True
    assert verify_body["events"] > 0


# --- Amount over the hard cap triggers STEP_UP (PIN), wrong PIN 3x locks the confirmation ---

def test_wrong_pin_lockout(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    _add_contact(client, "Alice")
    # Amount exceeds the 50 AUD hard cap -> STEP_UP with PIN required
    r = client.post(
        "/command/text",
        json={"session_id": session_id, "text": "Pay 75 to Alice"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["decision"]["decision"] == "STEP_UP"
    assert body["decision"]["required_confirmation"] == "pin"
    confirmation_id = body["confirmation"]["confirmation_id"]

    for _ in range(2):
        r = client.post(
            "/confirm/pin",
            json={"session_id": session_id, "confirmation_id": confirmation_id, "pin": "0000"},
        )
        assert r.json() == {"ok": False, "error": "Wrong PIN"}

    # Third wrong attempt hits max_attempts=3 -> rejected
    r = client.post(
        "/confirm/pin",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "pin": "0000"},
    )
    assert r.json() == {"ok": False, "error": "Too many attempts. Confirmation rejected."}

    # Further attempts (even correct PIN) are rejected since confirmation is no longer pending
    r = client.post(
        "/confirm/pin",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "pin": "1234"},
    )
    assert r.json() == {"ok": False, "error": "Confirmation is rejected"}


# --- Correct PIN on a high-value payment to a known contact succeeds ---

def test_correct_pin_confirms(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    _add_contact(client, "Bob")
    r = client.post(
        "/command/text",
        json={"session_id": session_id, "text": "Pay 75 to Bob"},
    )
    body = r.json()
    confirmation_id = body["confirmation"]["confirmation_id"]

    r = client.post(
        "/confirm/pin",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "pin": "1234"},
    )
    assert r.json()["ok"] is True
    assert r.json()["status"] == "confirmed"


# --- Expired confirmations are rejected and fail the transaction ---

def test_expired_confirmation_is_rejected(client):
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction

    session_id = _new_session(client)
    txn_id = create_pending_transaction(
        session_id=session_id,
        user_id="demo-user",
        amount_cents=1000,
        currency="AUD",
        payee_id=None,
    )
    conf = create_confirmation(
        txn_id=txn_id, user_id="demo-user", required_confirmation="normal", ttl_seconds=-1
    )

    r = client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "phrase": "CONFIRM"},
    )
    assert r.json() == {"ok": False, "error": "Confirmation expired"}

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    event_types = [e["event_type"] for e in events]
    assert "CONFIRMATION_EXPIRED" in event_types


# --- User-initiated cancellation of a pending confirmation ---

def test_confirm_cancel_pending_confirmation(client):
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction, get_transaction

    session_id = _new_session(client)
    txn_id = create_pending_transaction(
        session_id=session_id, user_id="demo-user", amount_cents=200, currency="AUD", payee_id=None,
    )
    conf = create_confirmation(txn_id=txn_id, user_id="demo-user", required_confirmation="normal")

    r = client.post(
        "/confirm/cancel",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"]},
    )
    assert r.json() == {"ok": True, "txn_id": txn_id}
    assert get_transaction(txn_id)["status"] == "failed"

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    assert "CONFIRM_CANCELLED" in [e["event_type"] for e in events]

    # Already cancelled -- a second attempt is a clear no-op, not a crash.
    r2 = client.post(
        "/confirm/cancel",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"]},
    )
    assert r2.json() == {"ok": False, "error": "Confirmation is cancelled"}

    # And since it's no longer pending, confirming it afterward is refused too.
    r3 = client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "phrase": "CONFIRM"},
    )
    assert r3.json() == {"ok": False, "error": "Confirmation is cancelled"}


def test_confirm_cancel_already_approved_is_rejected(client):
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction

    session_id = _new_session(client)
    txn_id = create_pending_transaction(
        session_id=session_id, user_id="demo-user", amount_cents=200, currency="AUD", payee_id=None,
    )
    conf = create_confirmation(txn_id=txn_id, user_id="demo-user", required_confirmation="normal")
    client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "phrase": "CONFIRM"},
    )

    r = client.post(
        "/confirm/cancel",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"]},
    )
    assert r.json() == {"ok": False, "error": "Confirmation is approved"}


def test_confirm_cancel_expired_confirmation_is_rejected(client):
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction

    session_id = _new_session(client)
    txn_id = create_pending_transaction(
        session_id=session_id, user_id="demo-user", amount_cents=200, currency="AUD", payee_id=None,
    )
    conf = create_confirmation(txn_id=txn_id, user_id="demo-user", required_confirmation="normal", ttl_seconds=-1)
    client.post("/confirm/normal", json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "phrase": "CONFIRM"})

    r = client.post(
        "/confirm/cancel",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"]},
    )
    assert r.json() == {"ok": False, "error": "Confirmation is expired"}


def test_confirm_cancel_unknown_confirmation_id(client):
    session_id = _new_session(client)
    r = client.post("/confirm/cancel", json={"session_id": session_id, "confirmation_id": "nonexistent"})
    assert r.json() == {"ok": False, "error": "Confirmation not found"}


# --- Audit hash chain: tampering with a stored event breaks verification ---

def test_audit_chain_detects_tampering(client):
    session_id = _new_session(client)
    client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to Carol"})

    r = client.get(f"/audit/{session_id}/verify")
    assert r.json()["ok"] is True

    conn = db_module.get_conn()
    try:
        row = conn.execute(
            "SELECT event_id FROM audit_events WHERE session_id=? ORDER BY ts ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            ('{"tampered":true}', row["event_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/audit/{session_id}/verify")
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "event_hash mismatch"


# --- Declined card: transaction is marked failed, no crash, no double-charge risk ---

def test_execute_handles_card_decline(client, monkeypatch):
    import stripe

    def _declined(**kwargs):
        raise stripe.error.CardError(
            message="Your card was declined.", param="payment_method", code="card_declined"
        )

    _mock_connect_flow(monkeypatch)
    monkeypatch.setattr(main, "create_payment_intent", _declined)

    session_id = _new_session(client)
    _add_contact(client, "Dave")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 10 to Dave"})
    body = r.json()
    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]

    client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"},
    )

    r = client.post("/pay/execute", json={"session_id": session_id, "txn_id": txn_id})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Card declined"

    from transactions_repo import get_transaction
    assert get_transaction(txn_id)["status"] == "failed"


# --- Transient Stripe/network failure also marks the transaction failed ---

def test_execute_handles_transient_failure(client, monkeypatch):
    import stripe

    def _unreachable(**kwargs):
        raise stripe.error.APIConnectionError(message="Could not connect to Stripe")

    _mock_connect_flow(monkeypatch)
    monkeypatch.setattr(main, "create_payment_intent", _unreachable)

    session_id = _new_session(client)
    _add_contact(client, "Erin")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 10 to Erin"})
    body = r.json()
    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]

    client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"},
    )

    r = client.post("/pay/execute", json={"session_id": session_id, "txn_id": txn_id})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Payment service temporarily unavailable, please retry"

    from transactions_repo import get_transaction
    assert get_transaction(txn_id)["status"] == "failed"


# --- Setup endpoints: list/add payees, list payment methods, seed a test card ---

def test_setup_endpoints_list_and_seed(client, monkeypatch):
    assert client.get("/payees").json() == {"payees": []}
    assert client.get("/payment_methods").json() == {"payment_methods": []}

    _add_payee(client, "Frank")
    payees = client.get("/payees").json()["payees"]
    assert len(payees) == 1
    assert payees[0]["nickname"] == "Frank"

    monkeypatch.setattr(main, "create_test_payment_method", lambda: ("cus_fake", "pm_fake"))
    r = client.post("/payment_methods/seed_test_card")
    body = r.json()
    assert body["ok"] is True
    assert body["is_default"] is True

    pms = client.get("/payment_methods").json()["payment_methods"]
    assert len(pms) == 1
    assert pms[0]["is_default"] == 1
    assert pms[0]["stripe_payment_method_id"] == "pm_fake"


def test_seed_test_card_handles_stripe_error(client, monkeypatch):
    import stripe

    def _boom():
        raise stripe.error.StripeError("Stripe is down")

    monkeypatch.setattr(main, "create_test_payment_method", _boom)
    r = client.post("/payment_methods/seed_test_card")
    body = r.json()
    assert body["ok"] is False
    assert client.get("/payment_methods").json() == {"payment_methods": []}


# --- Contacts / receiver-evidence: pay a contact, prove funds reached THEIR account ---

def _mock_connect_flow(monkeypatch, account_id="acct_fake", transfer_id="tr_fake", pending_cents=1200):
    import stripe

    monkeypatch.setattr(main, "create_test_connected_account", lambda display_name: account_id)
    monkeypatch.setattr(
        main,
        "create_payment_intent",
        lambda **kwargs: {"id": "pi_fake_123", "status": "succeeded", "latest_charge": "ch_fake_123"},
    )
    monkeypatch.setattr(
        stripe.Charge, "retrieve", staticmethod(lambda charge_id: {"transfer": transfer_id})
    )
    monkeypatch.setattr(
        main,
        "get_account_balance",
        lambda acct: {"available_cents": 0, "pending_cents": pending_cents, "currency": "aud"},
    )


def test_add_contact_creates_payee_with_connect_account(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    r = client.post("/payees/add_contact", json={"nickname": "Grace", "phone_number": "0400 999 888"})
    body = r.json()
    assert body["ok"] is True
    assert body["stripe_connected_account_id"] == "acct_fake"

    payees = client.get("/payees").json()["payees"]
    assert payees[0]["nickname"] == "Grace"
    assert payees[0]["phone_number"] == "0400 999 888"
    assert payees[0]["stripe_connected_account_id"] == "acct_fake"


def test_add_contact_requires_nickname_and_phone(client):
    r = client.post("/payees/add_contact", json={"nickname": "", "phone_number": ""})
    assert r.json() == {"ok": False, "error": "nickname and phone_number are required"}


def test_add_contact_handles_stripe_error(client, monkeypatch):
    import stripe

    def _boom(display_name):
        raise stripe.error.StripeError("Connect not enabled")

    monkeypatch.setattr(main, "create_test_connected_account", _boom)
    r = client.post("/payees/add_contact", json={"nickname": "Grace", "phone_number": "0400 999 888"})
    body = r.json()
    assert body["ok"] is False
    assert client.get("/payees").json() == {"payees": []}


def test_seed_demo_contacts_creates_variety_and_skips_duplicates(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    r = client.post("/payees/seed_demo_contacts")
    body = r.json()
    assert len(body["created"]) == 3
    assert body["skipped"] == []

    r2 = client.post("/payees/seed_demo_contacts")
    body2 = r2.json()
    assert body2["created"] == []
    assert len(body2["skipped"]) == 3


def test_pay_to_contact_returns_receiver_evidence(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_grace", transfer_id="tr_grace", pending_cents=1200)

    session_id = _new_session(client)
    contact = client.post("/payees/add_contact", json={"nickname": "Grace", "phone_number": "0400 999 888"})
    payee_id = contact.json()["payee_id"]
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 12 to Grace"})
    body = r.json()
    assert body["payee"]["has_receiver_tracking"] is True
    assert "PayID: 0400 999 888" in body["read_back"]
    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]

    client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"},
    )

    r = client.post("/pay/execute", json={"session_id": session_id, "txn_id": txn_id})
    body = r.json()
    assert body["ok"] is True
    ev = body["receiver_evidence"]
    assert ev["confirmed"] is True
    assert ev["destination_account_id"] == "acct_grace"
    assert ev["stripe_transfer_id"] == "tr_grace"
    assert ev["pending_cents"] == 1200

    bal = client.get(f"/payees/{payee_id}/balance").json()
    assert bal["ok"] is True
    assert bal["pending_cents"] == 1200

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    assert "RECEIVER_BALANCE_CONFIRMED" in [e["event_type"] for e in events]


def test_pay_to_payee_without_payid_is_blocked(client):
    session_id = _new_session(client)
    _add_payee(client, "Plain John")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 12 to Plain John"})
    body = r.json()
    assert body["ok"] is False
    assert body["decision"]["decision"] == "BLOCK"
    assert "no PayID on file" in body["decision"]["reason"]


def test_pay_to_unknown_name_is_blocked(client):
    session_id = _new_session(client)
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 12 to Nobody"})
    body = r.json()
    assert body["ok"] is False
    assert body["decision"]["decision"] == "BLOCK"
    assert "not a saved contact" in body["decision"]["reason"]


def test_balance_endpoint_rejects_payee_without_connect_account(client):
    _add_payee(client, "Plain John")
    payees = client.get("/payees").json()["payees"]
    payee_id = payees[0]["payee_id"]
    r = client.get(f"/payees/{payee_id}/balance")
    assert r.json() == {"ok": False, "error": "This payee has no receiver account to check"}


# --- Duplicate contact names: same person (multiple numbers) vs different person ---

def test_add_contact_with_colliding_name_returns_conflict(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_ava1")
    client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0427 499 675"})

    _mock_connect_flow(monkeypatch, account_id="acct_ava2")
    r = client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0447 959 495"})
    body = r.json()
    assert body["ok"] is False
    assert body["conflict"] == "duplicate_name"
    assert body["existing_contact"]["phone_number"] == "0427 499 675"


def test_add_contact_same_person_links_and_resolves_unambiguously(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_ava1")
    first = client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0427 499 675"})
    first_id = first.json()["payee_id"]

    _mock_connect_flow(monkeypatch, account_id="acct_ava2")
    second = client.post(
        "/payees/add_contact",
        json={"nickname": "Ava Hill", "phone_number": "0447 959 495", "resolution": "same_person"},
    )
    body = second.json()
    assert body["ok"] is True

    payees = client.get("/payees").json()["payees"]
    assert len(payees) == 2
    linked = [p for p in payees if p["payee_id"] != first_id][0]
    assert linked["linked_contact_id"] == first_id

    # Paying "Ava" by name is no longer ambiguous now that they're linked.
    session_id = _new_session(client)
    _add_default_payment_method(client)
    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to Ava"})
    body = r.json()
    assert body["ok"] is True
    assert body["decision"]["decision"] == "PROCEED"


def test_add_contact_different_person_stays_ambiguous(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_ava1")
    client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0427 499 675"})

    _mock_connect_flow(monkeypatch, account_id="acct_ava2")
    r = client.post(
        "/payees/add_contact",
        json={"nickname": "Ava Hill", "phone_number": "0447 959 495", "resolution": "different_person"},
    )
    assert r.json()["ok"] is True

    # Still two distinct, unlinked "Ava Hill" contacts -> paying by that exact
    # name is genuinely ambiguous and must ask, now qualified by PayID.
    session_id = _new_session(client)
    r2 = client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to Ava Hill"})
    body2 = r2.json()
    assert body2["ok"] is False
    assert body2["decision"]["decision"] == "CLARIFY"
    assert set(body2["candidates"]) == {"Ava Hill (0427 499 675)", "Ava Hill (0447 959 495)"}


def test_pay_specific_linked_number_via_explicit_payid_qualifier(client, monkeypatch):
    # Same scenario as the CLARIFY/linking tests above, but this time the two
    # "Ava Hill"s are linked as the same person, so paying by bare name
    # resolves to a default — an explicit "Name PayID: number" qualifier must
    # still be able to target the *other* linked number specifically.
    _mock_connect_flow(monkeypatch, account_id="acct_ava1")
    first = client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0487 879 135"})
    first_id = first.json()["payee_id"]

    _mock_connect_flow(monkeypatch, account_id="acct_ava2")
    client.post(
        "/payees/add_contact",
        json={"nickname": "Ava Hill", "phone_number": "0427 499 675", "resolution": "same_person"},
    )

    session_id = _new_session(client)
    _add_default_payment_method(client)
    r = client.post(
        "/command/text",
        json={"session_id": session_id, "text": "pay 4 to Ava Hill PayID: 0427 499 675"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["payee"]["phone_number"] == "0427 499 675"
    assert body["payee"]["payee_id"] != first_id


def test_save_as_contact_with_colliding_name_returns_conflict_and_resolves(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_ava1")
    client.post("/payees/add_contact", json={"nickname": "Ava Hill", "phone_number": "0427 499 675"})

    # Auto-provision a second "Ava Hill" via a direct PayID payment (not saved yet).
    monkeypatch.setattr(main, "create_test_connected_account", lambda display_name: "acct_ava2")
    _insert_directory_entry(phone_number="0299998888", display_number="0299 998 888", registered_name="Ava Hill")
    session_id = _new_session(client)
    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to 0299998888"})
    payee_id = r.json()["payee"]["payee_id"]

    save = client.post(f"/payees/{payee_id}/save_as_contact")
    body = save.json()
    assert body["ok"] is False
    assert body["conflict"] == "duplicate_name"

    save2 = client.post(f"/payees/{payee_id}/save_as_contact", json={"resolution": "same_person"})
    assert save2.json()["ok"] is True

    payees = client.get("/payees").json()["payees"]
    saved = [p for p in payees if p["payee_id"] == payee_id][0]
    assert saved["linked_contact_id"] is not None


# --- Fuzzy contact-name resolution: "Alice" should find "Alice Wonderland" ---

def test_shorthand_name_resolves_to_full_contact(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_alice", transfer_id="tr_alice", pending_cents=3000)

    session_id = _new_session(client)
    client.post("/payees/add_contact", json={"nickname": "Alice Wonderland", "phone_number": "0400 111 222"})
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 30 to Alice"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["payee_name"] == "Alice Wonderland"
    assert body["decision"]["decision"] == "PROCEED"
    assert body["decision"]["required_confirmation"] == "normal"
    assert "PayID: 0400 111 222" in body["read_back"]


def test_ambiguous_shorthand_name_returns_clarify(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    client.post("/payees/add_contact", json={"nickname": "Alice Wonderland", "phone_number": "0400 111 222"})
    client.post("/payees/add_contact", json={"nickname": "Alice Cooper", "phone_number": "0400 777 888"})

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to Alice"})
    body = r.json()
    assert body["ok"] is False
    assert body["decision"]["decision"] == "CLARIFY"
    assert set(body["candidates"]) == {"Alice Wonderland (0400 111 222)", "Alice Cooper (0400 777 888)"}


# --- Flexible phrasing: the parser understands intent, not just one template ---

def test_parser_understands_on_account_phrasing():
    result = parse_text_command("Pay 30 AUD on Alice account")
    assert result.ok is True
    assert result.intent.amount == 30.0
    assert result.intent.payee_name == "Alice"


def test_parser_understands_possessive_account_phrasing():
    result = parse_text_command("Pay 30 AUD on Alice's account")
    assert result.ok is True
    assert result.intent.amount == 30.0
    assert result.intent.payee_name == "Alice"


def test_parser_self_correction_stated_after_payee():
    # "Pay 20 to Dave... wait make it 35 for the pizza" -- a correction
    # stated AFTER the payee, not just before it.
    result = parse_text_command("Pay 20 bucks to Dave... wait make it 35 for the pizza")
    assert result.ok is True
    assert result.intent.amount == 35.0
    assert result.intent.payee_name == "Dave"
    assert result.intent.note == "the pizza"


def test_parser_note_numbers_dont_get_mistaken_for_correction():
    result = parse_text_command("Pay 12 to John for 2 drinks")
    assert result.ok is True
    assert result.intent.amount == 12.0
    assert result.intent.note == "2 drinks"


def test_parser_self_correction_last_amount_wins():
    result = parse_text_command("Pay 20, no 30 to Smith")
    assert result.ok is True
    assert result.intent.amount == 30.0
    assert result.intent.payee_name == "Smith"


def test_parser_amount_after_target_still_found():
    result = parse_text_command("Pay for John 12 dollars")
    assert result.ok is True
    assert result.intent.amount == 12.0
    assert result.intent.payee_name == "John"


def test_parser_understands_explicit_payid_qualifier_after_name():
    result = parse_text_command("pay 4 to Ava Hill PayID: 0427 499 675")
    assert result.ok is True
    assert result.intent.amount == 4.0
    assert result.intent.payee_name == "0427 499 675"


def test_parser_understands_parenthetical_payid_qualifier():
    result = parse_text_command("Pay 4 to Ava Hill (0427 499 675)")
    assert result.ok is True
    assert result.intent.amount == 4.0
    assert result.intent.payee_name == "0427 499 675"


def test_parser_understands_no_preposition_phrasing():
    result = parse_text_command("Send John 12 aud")
    assert result.ok is True
    assert result.intent.amount == 12.0
    assert result.intent.payee_name == "John"


def test_parser_no_preposition_with_self_correction():
    result = parse_text_command("Send John 12, no 15 aud")
    assert result.ok is True
    assert result.intent.amount == 15.0
    assert result.intent.payee_name == "John"


def test_flexible_phrasing_no_preposition_works_end_to_end(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    _add_contact(client, "John")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Send John 12 aud"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["amount"] == 12.0
    assert body["intent"]["payee_name"] == "John"


def test_parser_still_handles_original_template():
    result = parse_text_command("Pay 12 to John for dinner")
    assert result.ok is True
    assert result.intent.amount == 12.0
    assert result.intent.payee_name == "John"
    assert result.intent.note == "dinner"


def test_flexible_phrasing_works_end_to_end(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    _add_contact(client, "Alice")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 30 AUD on Alice account"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["amount"] == 30.0
    assert body["intent"]["payee_name"] == "Alice"


# --- Paying by PayID number instead of a name ---

def test_pay_by_payid_matches_own_contact(client, monkeypatch):
    _mock_connect_flow(monkeypatch, account_id="acct_hank", transfer_id="tr_hank", pending_cents=1200)

    session_id = _new_session(client)
    _add_contact(client, "Hank", phone_number="0400 555 111")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 34 to 0400555111"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["payee_name"] == "Hank"
    assert body["decision"]["decision"] == "PROCEED"
    assert "PayID: 0400 555 111" in body["read_back"]


def _insert_directory_entry(phone_number="0412345678", display_number="0412 345 678", registered_name="Priya Kapoor"):
    conn = db_module.get_conn()
    try:
        conn.execute(
            "INSERT INTO payid_directory (phone_number, display_number, registered_name) VALUES (?, ?, ?)",
            (phone_number, display_number, registered_name),
        )
        conn.commit()
    finally:
        conn.close()


def test_pay_by_unsaved_registered_payid_auto_provisions_and_proceeds(client, monkeypatch):
    # Real-world PayID behaviour: a registered-but-unsaved PayID can be paid
    # immediately — saving as a contact is optional, offered afterwards.
    monkeypatch.setattr(main, "create_test_connected_account", lambda display_name: "acct_priya")
    _insert_directory_entry()

    session_id = _new_session(client)
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 34 to 0412345678"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["payee_name"] == "Priya Kapoor"
    assert body["decision"]["decision"] == "PROCEED"
    assert body["payee"]["has_receiver_tracking"] is True
    assert body["payee"]["is_saved_contact"] is False
    payee_id = body["payee"]["payee_id"]

    # Not saved yet -> doesn't show up in the Setup contacts list.
    payees = client.get("/payees").json()["payees"]
    assert all(p["payee_id"] != payee_id for p in payees)

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    assert "PAYID_AUTO_PROVISIONED" in [e["event_type"] for e in events]

    # But it's still a real payee row with a receiver account, so the payment
    # itself proceeds and is fully trackable end to end.
    monkeypatch.setattr(
        main, "create_payment_intent",
        lambda **kwargs: {"id": "pi_priya_123", "status": "succeeded", "latest_charge": "ch_priya_123"},
    )
    import stripe
    monkeypatch.setattr(stripe.Charge, "retrieve", staticmethod(lambda charge_id: {"transfer": "tr_priya"}))
    monkeypatch.setattr(main, "get_account_balance", lambda acct: {"available_cents": 0, "pending_cents": 3400, "currency": "aud"})

    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]
    client.post("/confirm/normal", json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"})
    r2 = client.post("/pay/execute", json={"session_id": session_id, "txn_id": txn_id})
    pay_body = r2.json()
    assert pay_body["ok"] is True
    assert pay_body["receiver_evidence"]["confirmed"] is True
    assert pay_body["receiver_evidence"]["destination_account_id"] == "acct_priya"

    # Now explicitly save them — one click, no re-entering name/phone.
    save = client.post(f"/payees/{payee_id}/save_as_contact")
    assert save.json() == {"ok": True, "payee_id": payee_id, "nickname": "Priya Kapoor"}
    payees_after = client.get("/payees").json()["payees"]
    assert any(p["payee_id"] == payee_id for p in payees_after)


def test_pay_by_already_auto_provisioned_payid_reuses_same_payee(client, monkeypatch):
    monkeypatch.setattr(main, "create_test_connected_account", lambda display_name: "acct_priya")
    _insert_directory_entry()

    session_id = _new_session(client)
    r1 = client.post("/command/text", json={"session_id": session_id, "text": "Pay 10 to 0412345678"})
    payee_id_1 = r1.json()["payee"]["payee_id"]

    r2 = client.post("/command/text", json={"session_id": session_id, "text": "Pay 20 to 0412345678"})
    payee_id_2 = r2.json()["payee"]["payee_id"]

    assert payee_id_1 == payee_id_2


def test_pay_by_unregistered_payid_is_blocked(client):
    session_id = _new_session(client)
    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 34 to 0499000000"})
    body = r.json()
    assert body["ok"] is False
    assert body["decision"]["decision"] == "BLOCK"
    assert "not a valid, registered PayID" in body["decision"]["reason"]


def test_payid_directory_is_seeded_with_200_entries(client):
    conn = db_module.get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM payid_directory").fetchone()["n"]
    finally:
        conn.close()
    assert count == 200


def test_exact_name_match_wins_over_ambiguous_shorthand(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    session_id = _new_session(client)
    client.post("/payees/add_contact", json={"nickname": "Alice", "phone_number": "0400 000 000"})
    client.post("/payees/add_contact", json={"nickname": "Alice Cooper", "phone_number": "0400 777 888"})
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 5 to Alice"})
    body = r.json()
    assert body["ok"] is True
    assert body["intent"]["payee_name"] == "Alice"


# --- Support chatbot: LangChain + Gemini agent, mocked (no real API calls) ---

def test_support_chat_returns_reply(client, monkeypatch):
    monkeypatch.setattr(support_chat, "get_support_reply", lambda user_id, message: "You have no contacts yet.")

    session_id = _new_session(client)
    r = client.post("/support/chat", json={"session_id": session_id, "message": "Who are my contacts?"})
    body = r.json()
    assert body["ok"] is True
    assert body["reply"] == "You have no contacts yet."

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    event_types = [e["event_type"] for e in events]
    assert "SUPPORT_CHAT_MESSAGE" in event_types
    assert "SUPPORT_CHAT_REPLY" in event_types


def test_support_chat_rejects_empty_message(client):
    session_id = _new_session(client)
    r = client.post("/support/chat", json={"session_id": session_id, "message": "   "})
    assert r.json() == {"ok": False, "error": "Message is empty"}


def test_support_chat_missing_api_key_surfaces_clear_error(client, monkeypatch):
    def _boom(user_id, message):
        raise RuntimeError("Missing GEMINI_API_KEY in environment/.env")

    monkeypatch.setattr(support_chat, "get_support_reply", _boom)

    session_id = _new_session(client)
    r = client.post("/support/chat", json={"session_id": session_id, "message": "hi"})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Missing GEMINI_API_KEY in environment/.env"

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    assert "SUPPORT_CHAT_FAILED" in [e["event_type"] for e in events]


# --- Recurring/scheduled payments ---

def test_create_payid_schedule_and_process_due(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    _add_contact(client, "Nora")
    _add_default_payment_method(client)
    payees = client.get("/payees").json()["payees"]
    payee_id = payees[0]["payee_id"]

    r = client.post(
        "/recurring/schedules",
        json={"payment_rail": "payid", "target_identifier": payee_id, "amount_cents": 2000, "cadence": "weekly"},
    )
    body = r.json()
    assert body["ok"] is True
    schedule_id = body["schedule_id"]

    schedules = client.get("/recurring/schedules").json()["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["status"] == "active"

    session_id = _new_session(client)
    r2 = client.post("/recurring/process_due", json={"session_id": session_id})
    body2 = r2.json()
    assert body2["ok"] is True
    assert len(body2["processed"]) == 1
    assert body2["processed"][0]["ok"] is True

    from transactions_repo import get_transaction
    txn = get_transaction(body2["processed"][0]["txn_id"])
    assert txn["status"] == "succeeded"

    # next_run_at advanced -> immediately re-processing finds nothing due.
    r3 = client.post("/recurring/process_due", json={"session_id": session_id})
    assert r3.json()["processed"] == []


def test_create_bpay_schedule_and_process_due(client):
    client.post("/bpay/billers/save", json={"nickname": "My Power", "biller_code": "111999", "crn": "79927398713"})
    billers = client.get("/bpay/billers").json()["billers"]
    biller_id = billers[0]["biller_id"]

    r = client.post(
        "/recurring/schedules",
        json={"payment_rail": "bpay", "target_identifier": biller_id, "amount_cents": 5000, "cadence": "monthly"},
    )
    assert r.json()["ok"] is True

    session_id = _new_session(client)
    r2 = client.post("/recurring/process_due", json={"session_id": session_id})
    body2 = r2.json()
    assert len(body2["processed"]) == 1
    assert body2["processed"][0]["ok"] is True


def test_schedule_with_unknown_payee_is_rejected(client):
    r = client.post(
        "/recurring/schedules",
        json={"payment_rail": "payid", "target_identifier": "nonexistent", "amount_cents": 1000, "cadence": "weekly"},
    )
    assert r.json() == {"ok": False, "error": "Unknown payee_id"}


def test_schedule_pause_stops_processing(client, monkeypatch):
    _mock_connect_flow(monkeypatch)
    _add_contact(client, "Omar")
    _add_default_payment_method(client)
    payee_id = client.get("/payees").json()["payees"][0]["payee_id"]

    schedule_id = client.post(
        "/recurring/schedules",
        json={"payment_rail": "payid", "target_identifier": payee_id, "amount_cents": 1500, "cadence": "weekly"},
    ).json()["schedule_id"]

    client.post(f"/recurring/schedules/{schedule_id}/status", json={"status": "paused"})

    session_id = _new_session(client)
    r = client.post("/recurring/process_due", json={"session_id": session_id})
    assert r.json()["processed"] == []


def test_normalize_spoken_numbers_converts_digit_word_runs():
    from voice_service import normalize_spoken_numbers
    assert normalize_spoken_numbers("Send 15 to oh four one two, three four five, six seven eight") == "Send 15 to 0412345678"
    assert normalize_spoken_numbers("I have one apple and two oranges") == "I have one apple and two oranges"


# --- Local voice transcription (faster-whisper) -- model mocked, no real download/inference ---

def test_voice_transcribe_returns_text(client, monkeypatch):
    import voice_service
    monkeypatch.setattr(voice_service, "transcribe_audio_bytes", lambda audio_bytes, suffix=".wav": "pay twelve to john")

    session_id = _new_session(client)
    r = client.post(
        "/voice/transcribe",
        data={"session_id": session_id},
        files={"audio": ("clip.wav", b"fake-wav-bytes", "audio/wav")},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["text"] == "pay twelve to john"

    events = client.get(f"/audit/{session_id}/events").json()["events"]
    assert "VOICE_TRANSCRIBE_SUCCESS" in [e["event_type"] for e in events]


def test_voice_transcribe_rejects_empty_audio(client):
    session_id = _new_session(client)
    r = client.post(
        "/voice/transcribe",
        data={"session_id": session_id},
        files={"audio": ("clip.wav", b"", "audio/wav")},
    )
    assert r.json() == {"ok": False, "error": "No audio received"}


def test_voice_transcribe_handles_backend_failure(client, monkeypatch):
    import voice_service

    def _boom(audio_bytes, suffix=".wav"):
        raise RuntimeError("model failed to load")

    monkeypatch.setattr(voice_service, "transcribe_audio_bytes", _boom)
    session_id = _new_session(client)
    r = client.post(
        "/voice/transcribe",
        data={"session_id": session_id},
        files={"audio": ("clip.wav", b"fake-wav-bytes", "audio/wav")},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Transcription failed"


def test_llm_provider_switches_to_ollama_without_api_key(monkeypatch):
    monkeypatch.setattr(support_chat, "LLM_PROVIDER", "ollama")
    model = support_chat._build_model()
    assert type(model).__name__ == "ChatOllama"
    assert model.base_url == support_chat.OLLAMA_BASE_URL
    assert model.model == support_chat.OLLAMA_MODEL


def test_ollama_model_has_server_side_timeout(monkeypatch):
    # A hung/slow Ollama call must fail on a bound, server-side -- otherwise
    # it ties up a FastAPI sync-thread-pool worker indefinitely, which (with
    # enough of them) can stall unrelated endpoints sharing that pool.
    monkeypatch.setattr(support_chat, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(support_chat, "OLLAMA_TIMEOUT_SECONDS", 42.0)
    model = support_chat._build_model()
    assert model._client._client.timeout.connect == 42.0


def test_llm_provider_unknown_value_raises_clear_error(monkeypatch):
    monkeypatch.setattr(support_chat, "LLM_PROVIDER", "something-else")
    try:
        support_chat._build_model()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "something-else" in str(e)


def test_extract_text_handles_stringified_content_blocks():
    # Observed live: langchain-google-genai sometimes returns AIMessage.content
    # as a real list of typed blocks, and sometimes (inconsistently) as an
    # already-stringified repr of that same list -- both must resolve to just
    # the human-readable text, never leaking internal signature/metadata.
    real_list = [{"type": "text", "text": "Hello there!", "extras": {"signature": "abc"}}]
    stringified = str(real_list)
    assert support_chat._extract_text(real_list) == "Hello there!"
    assert support_chat._extract_text(stringified) == "Hello there!"
    assert support_chat._extract_text("Plain string reply") == "Plain string reply"


class _CountingModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "counting-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        human_turns = sum(1 for m in messages if m.type == "human")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"turn {human_turns}"))])


def test_support_chat_memory_survives_graph_restart(client, monkeypatch):
    from users_repo import DEMO_USER_ID

    monkeypatch.setattr(support_chat, "_build_model", lambda: _CountingModel())

    assert support_chat.get_support_reply(DEMO_USER_ID, "My bank is CBA") == "turn 1"

    support_chat.reset_graph()

    assert support_chat.get_support_reply(DEMO_USER_ID, "What bank did I say?") == "turn 2"


# --- BPAY rail: parsing, CRN validation, and the full command -> confirm -> execute flow ---

def test_bpay_crn_mod10_validation():
    from bpay_repo import validate_crn_mod10
    assert validate_crn_mod10("79927398713") is True
    assert validate_crn_mod10("79927398710") is False
    assert validate_crn_mod10("5") is False
    assert validate_crn_mod10("") is False


def test_bpay_parser_extracts_full_command():
    from bpay_parser import parse_bpay_command
    result = parse_bpay_command("Pay Origin BPAY 110 dollars, biller code 111999, reference 79927398713")
    assert result.ok is True
    assert result.biller_name == "Origin"
    assert result.biller_code == "111999"
    assert result.crn == "79927398713"
    assert result.amount == 110.0
    assert result.missing == []


def test_bpay_parser_flags_missing_slots():
    from bpay_parser import parse_bpay_command
    result = parse_bpay_command("Pay my electricity bill with BPAY")
    assert result.ok is True
    assert "crn" in result.missing
    assert "amount" in result.missing


def test_bpay_command_with_invalid_crn_is_clarify(client):
    session_id = _new_session(client)
    r = client.post(
        "/bpay/command",
        json={"session_id": session_id, "text": "Pay Origin BPAY 30 dollars, biller code 111999, reference 123"},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["decision"]["decision"] == "CLARIFY"
    assert "crn" in body["missing"]


def test_bpay_command_with_unknown_biller_is_clarify(client):
    session_id = _new_session(client)
    r = client.post(
        "/bpay/command",
        json={"session_id": session_id, "text": "Pay 30 BPAY biller code 000000 reference 79927398713"},
    )
    body = r.json()
    assert body["ok"] is False
    assert "biller_code" in body["missing"]


def test_bpay_full_flow_command_confirm_execute(client):
    session_id = _new_session(client)
    r = client.post(
        "/bpay/command",
        json={"session_id": session_id, "text": "Pay Origin BPAY 30 dollars, biller code 111999, reference 79927398713"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["biller"]["biller_name"] == "Origin Energy"
    assert body["decision"]["required_confirmation"] == "normal"
    txn_id = body["txn_id"]
    confirmation_id = body["confirmation"]["confirmation_id"]

    r2 = client.post(
        "/confirm/normal",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "phrase": "CONFIRM"},
    )
    assert r2.json()["ok"] is True

    r3 = client.post("/bpay/execute", json={"session_id": session_id, "txn_id": txn_id})
    body3 = r3.json()
    assert body3["ok"] is True
    assert body3["final_status"] == "succeeded"
    assert body3["simulated_reference"].startswith("BPAY-SIM-")

    from transactions_repo import get_transaction
    txn = get_transaction(txn_id)
    assert txn["rail"] == "bpay"
    assert txn["status"] == "succeeded"
    assert txn["bpay_biller_code"] == "111999"


def test_bpay_amount_over_cap_requires_pin(client):
    session_id = _new_session(client)
    r = client.post(
        "/bpay/command",
        json={"session_id": session_id, "text": "Pay Origin BPAY 75 dollars, biller code 111999, reference 79927398713"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["decision"]["required_confirmation"] == "pin"


def test_bpay_save_and_list_billers(client):
    r = client.post(
        "/bpay/billers/save",
        json={"nickname": "My Electricity", "biller_code": "111999", "crn": "79927398713"},
    )
    assert r.json()["ok"] is True

    billers = client.get("/bpay/billers").json()["billers"]
    assert len(billers) == 1
    assert billers[0]["nickname"] == "My Electricity"


def test_bpay_save_biller_rejects_invalid_crn(client):
    r = client.post(
        "/bpay/billers/save",
        json={"nickname": "Bad CRN", "biller_code": "111999", "crn": "123"},
    )
    body = r.json()
    assert body["ok"] is False
    assert "CRN" in body["error"]


# --- Passkeys (WebAuthn/FIDO2): an additional step-up method alongside PIN ---
# Real browser cryptography can't run headlessly, so these mock webauthn's
# verify_* calls directly -- exercising this app's own wiring (challenge
# binding per confirmation, sign-count tracking, replay/clone detection)
# rather than the webauthn library itself.

def _mock_verified_registration(monkeypatch, credential_id=b"cred-1", public_key=b"pubkey-bytes", sign_count=0):
    from webauthn.registration.verify_registration_response import VerifiedRegistration
    from webauthn.helpers.structs import AttestationFormat, PublicKeyCredentialType, CredentialDeviceType

    fake = VerifiedRegistration(
        credential_id=credential_id, credential_public_key=public_key, sign_count=sign_count,
        aaguid="", fmt=AttestationFormat.NONE, credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True, attestation_object=b"", credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )
    import passkey_service
    monkeypatch.setattr(passkey_service.webauthn, "verify_registration_response", lambda **kwargs: fake)


def _mock_verified_authentication(monkeypatch, new_sign_count=1):
    from webauthn.authentication.verify_authentication_response import VerifiedAuthentication
    from webauthn.helpers.structs import CredentialDeviceType

    fake = VerifiedAuthentication(
        credential_id=b"cred-1", new_sign_count=new_sign_count,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE, credential_backed_up=False, user_verified=True,
    )
    import passkey_service
    monkeypatch.setattr(passkey_service.webauthn, "verify_authentication_response", lambda **kwargs: fake)


def _register_fake_passkey(client, monkeypatch, credential_id=b"cred-1"):
    import base64
    _mock_verified_registration(monkeypatch, credential_id=credential_id)
    begin = client.post("/webauthn/register/begin").json()
    assert begin["ok"] is True
    cred_id_b64url = base64.urlsafe_b64encode(credential_id).decode().rstrip("=")
    finish = client.post(
        "/webauthn/register/finish",
        json={"credential": {"id": cred_id_b64url, "response": {}}},
    )
    body = finish.json()
    assert body["ok"] is True
    return body["credential_id"]


def test_webauthn_register_begin_returns_valid_options(client):
    r = client.post("/webauthn/register/begin")
    body = r.json()
    assert body["ok"] is True
    assert body["options"]["rp"]["id"] == "localhost"
    assert "challenge" in body["options"]


def test_webauthn_register_finish_without_begin_fails(client):
    import passkey_service
    from users_repo import DEMO_USER_ID
    passkey_service._pending_registration_challenges.pop(DEMO_USER_ID, None)  # avoid leakage from other tests

    r = client.post("/webauthn/register/finish", json={"credential": {"id": "x", "response": {}}})
    body = r.json()
    assert body["ok"] is False
    assert "begin_registration" in body["error"]


def test_webauthn_register_and_list_credentials(client, monkeypatch):
    cred_id = _register_fake_passkey(client, monkeypatch)
    creds = client.get("/webauthn/credentials").json()["credentials"]
    assert len(creds) == 1
    assert creds[0]["credential_id"] == cred_id


def test_confirm_passkey_full_flow_approves_high_value_payment(client, monkeypatch):
    cred_id = _register_fake_passkey(client, monkeypatch)
    _mock_connect_flow(monkeypatch)

    session_id = _new_session(client)
    _add_contact(client, "Priya")
    _add_default_payment_method(client)

    r = client.post("/command/text", json={"session_id": session_id, "text": "Pay 75 to Priya"})
    body = r.json()
    assert body["decision"]["required_confirmation"] == "pin"  # over the 50 AUD cap
    confirmation_id = body["confirmation"]["confirmation_id"]
    txn_id = body["txn_id"]

    begin = client.post("/webauthn/authenticate/begin", json={"confirmation_id": confirmation_id})
    assert begin.json()["ok"] is True

    _mock_verified_authentication(monkeypatch, new_sign_count=1)
    r2 = client.post(
        "/confirm/passkey",
        json={"session_id": session_id, "confirmation_id": confirmation_id, "credential": {"id": cred_id}},
    )
    body2 = r2.json()
    assert body2["ok"] is True
    assert body2["status"] == "confirmed"

    from transactions_repo import get_transaction
    assert get_transaction(txn_id)["status"] == "confirmed"


def test_confirm_passkey_without_begin_is_rejected(client, monkeypatch):
    cred_id = _register_fake_passkey(client, monkeypatch)
    session_id = _new_session(client)
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction
    txn_id = create_pending_transaction(session_id=session_id, user_id="demo-user", amount_cents=7500, currency="AUD", payee_id=None)
    conf = create_confirmation(txn_id=txn_id, user_id="demo-user", required_confirmation="pin")

    r = client.post(
        "/confirm/passkey",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "credential": {"id": cred_id}},
    )
    body = r.json()
    assert body["ok"] is False
    assert "challenge" in body["error"]


def test_confirm_passkey_rejects_stale_sign_count_as_cloned(client, monkeypatch):
    cred_id = _register_fake_passkey(client, monkeypatch)
    session_id = _new_session(client)
    from confirmations_repo import create_confirmation
    from transactions_repo import create_pending_transaction
    txn_id = create_pending_transaction(session_id=session_id, user_id="demo-user", amount_cents=7500, currency="AUD", payee_id=None)
    conf = create_confirmation(txn_id=txn_id, user_id="demo-user", required_confirmation="pin")
    client.post("/webauthn/authenticate/begin", json={"confirmation_id": conf["confirmation_id"]})

    # Bump the stored sign count up first, to simulate a device that has
    # already authenticated once...
    import passkey_repo
    passkey_repo.update_sign_count(cred_id, 5)

    # ...then an assertion claiming a sign count that didn't increase beyond
    # that should be rejected as a possible cloned authenticator.
    _mock_verified_authentication(monkeypatch, new_sign_count=5)
    r = client.post(
        "/confirm/passkey",
        json={"session_id": session_id, "confirmation_id": conf["confirmation_id"], "credential": {"id": cred_id}},
    )
    body = r.json()
    assert body["ok"] is False
    assert "cloned" in body["error"]
