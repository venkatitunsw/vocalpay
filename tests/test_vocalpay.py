from datetime import datetime, timedelta, timezone

import db as db_module
import main
import support_chat
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


def test_support_chat_history_persists_per_user_not_per_session(client):
    from users_repo import DEMO_USER_ID

    support_chat._save_message(DEMO_USER_ID, "human", "What's my balance?")
    support_chat._save_message(DEMO_USER_ID, "ai", "You have no payment method on file yet.")

    history = support_chat._load_history(DEMO_USER_ID)
    assert len(history) == 2
    assert history[0].content == "What's my balance?"
    assert history[0].type == "human"
    assert history[1].content == "You have no payment method on file yet."
    assert history[1].type == "ai"
