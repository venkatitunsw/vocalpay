import os
import time
import stripe
from dotenv import load_dotenv

from payment_methods_repo import get_default_payment_method
from stripe_service import (
    init_stripe,
    create_payment_intent,
    create_test_payment_method,
    create_test_connected_account,
    get_account_balance,
)


from pathlib import Path as _Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uuid import uuid4

from models import PaymentIntentParsed
from intent_parser import parse_text_command

from db import init_db, DB_PATH
from audit import append_event, load_session_events, verify_session_chain

from users_repo import ensure_demo_user, DEMO_USER_ID

from policy import decide_next
from payees_repo import (
    payee_exists_for_user,
    find_payees_by_name,
    find_payee_by_phone,
    find_saved_contact_by_nickname,
    get_payee,
)
from payid_directory_repo import looks_like_payid, lookup_payid, seed_payid_directory, normalize_payid

from transactions_repo import (
    create_pending_transaction,
    update_transaction_status,
    get_transaction,
    set_receiver_evidence,
)
from confirmations_repo import create_confirmation, get_confirmation, is_confirmation_expired
from security import verify_pin

app = FastAPI(title="VocalPay")

# Dev-only permissive CORS so the frontend (served from a different port) can call
# the API directly. Tighten to a specific origin list before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()
    ensure_demo_user()
    seed_payid_directory()
    load_dotenv()
    init_stripe()


@app.get("/health")
def health():
    return {"status": "ok", "db_file": str(DB_PATH)}

class AuditAppendRequest(BaseModel):
    event_type: str
    payload: dict

@app.post("/session/new")
def new_session():
    session_id = str(uuid4())
    append_event(session_id, "SESSION_START", {"message": "Session created"})
    return {"session_id": session_id}

@app.post("/audit/{session_id}/append")
def audit_append(session_id: str, req: AuditAppendRequest):
    ev = append_event(session_id, req.event_type, req.payload)
    return ev

@app.get("/audit/{session_id}/events")
def audit_events(session_id: str):
    return {"session_id": session_id, "events": load_session_events(session_id)}

@app.get("/audit/{session_id}/verify")
def audit_verify(session_id: str):
    return verify_session_chain(session_id)

class TextCommandRequest(BaseModel):
    session_id: str
    text: str

@app.post("/command/text")
def command_text(req: TextCommandRequest):
    # 1) log raw command
    append_event(req.session_id, "COMMAND_TEXT_RECEIVED", {"text": req.text})

    # 2) parse
    result = parse_text_command(req.text)

    if not result.ok:
        append_event(req.session_id, "INTENT_PARSE_FAILED", {"error": result.error})
        return {
            "ok": False,
            "error": result.error,
        }

    intent: PaymentIntentParsed = result.intent
    append_event(req.session_id, "INTENT_PARSED", intent.model_dump())

    payee_record = None
    exists = False

    if looks_like_payid(intent.payee_name):
        # The user typed a number, not a name — resolve it as a PayID rather
        # than a nickname. Real-world PayID networks (and things like Zelle)
        # let you pay any *registered* PayID immediately — saving the person
        # as a contact is a convenience for next time, never a precondition
        # to pay them. So: check the user's own contacts first (by phone,
        # regardless of formatting); if that misses, check the external
        # PayID directory and, if it's a real registered number, provision a
        # receiver account for it on the spot so the payment can proceed and
        # still be trackable — without forcing a "save contact" step.
        payid_raw = intent.payee_name
        payee_record = find_payee_by_phone(DEMO_USER_ID, payid_raw)
        exists = payee_record is not None

        if not payee_record:
            directory_match = lookup_payid(payid_raw)
            append_event(req.session_id, "PAYEE_LOOKUP", {
                "payee_name": payid_raw, "exists": False, "by": "payid", "directory_match": directory_match,
            })

            if not directory_match:
                reason = f"\"{payid_raw}\" is not a valid, registered PayID."
                decision_info = {"decision": "BLOCK", "reason": reason, "required_confirmation": "none", "risk_level": "high"}
                append_event(req.session_id, "DECISION", decision_info)
                return {
                    "ok": False,
                    "intent": intent.model_dump(),
                    "payee_exists": False,
                    "decision": decision_info,
                }

            try:
                connected_account_id = create_test_connected_account(directory_match["registered_name"])
            except stripe.error.StripeError as e:
                reason = "Could not set up this PayID's receiving account. Please try again shortly."
                decision_info = {"decision": "BLOCK", "reason": reason, "required_confirmation": "none", "risk_level": "high"}
                append_event(req.session_id, "PAYID_PROVISION_FAILED", {"phone_number": payid_raw, "error": str(e)})
                append_event(req.session_id, "DECISION", decision_info)
                return {
                    "ok": False,
                    "intent": intent.model_dump(),
                    "payee_exists": False,
                    "decision": decision_info,
                }

            new_payee_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn = get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO payees
                    (payee_id, user_id, nickname, type, identifier, phone_number, stripe_connected_account_id, is_contact, created_at)
                    VALUES (?, ?, ?, 'stripe', ?, ?, ?, 0, ?)
                    """,
                    (
                        new_payee_id, DEMO_USER_ID, directory_match["registered_name"],
                        directory_match["display_number"], directory_match["display_number"],
                        connected_account_id, now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            payee_record = get_payee(new_payee_id)
            exists = True
            append_event(req.session_id, "PAYID_AUTO_PROVISIONED", {
                "payee_id": new_payee_id,
                "phone_number": directory_match["display_number"],
                "registered_name": directory_match["registered_name"],
                "stripe_connected_account_id": connected_account_id,
            })

        intent.payee_name = payee_record["nickname"]
        append_event(req.session_id, "PAYEE_LOOKUP", {"payee_name": intent.payee_name, "exists": True, "by": "payid"})
    else:
        # Resolve the spoken/typed name against saved contacts — exact match first,
        # then first-name (so "Alice" finds "Alice Wonderland"), then substring.
        # More than one match at the same tier is genuinely ambiguous: ask rather
        # than silently guessing which contact was meant.
        matches = find_payees_by_name(DEMO_USER_ID, intent.payee_name)
        if len(matches) > 1:
            # Qualify each candidate with its PayID so two entries sharing a
            # name (e.g. two "Ava Hill"s with different numbers) are never
            # shown as indistinguishable duplicates — the user can then say
            # the full name+number, or go to Setup and mark them as the same
            # person if that's what they actually are.
            candidate_labels = [
                f"{m['nickname']} ({m['phone_number']})" if m.get("phone_number") else m["nickname"]
                for m in matches
            ]
            append_event(req.session_id, "PAYEE_LOOKUP", {
                "payee_name": intent.payee_name, "exists": True, "ambiguous": True, "candidates": candidate_labels,
            })
            reason = (
                f"Multiple contacts match \"{intent.payee_name}\": {', '.join(candidate_labels)}. "
                f"Say the PayID number to pick one, or open Setup and mark them as the same person if they are."
            )
            append_event(req.session_id, "DECISION", {
                "decision": "CLARIFY", "reason": reason, "required_confirmation": "none", "risk_level": "low",
            })
            return {
                "ok": False,
                "intent": intent.model_dump(),
                "payee_exists": True,
                "decision": {
                    "decision": "CLARIFY",
                    "reason": reason,
                    "required_confirmation": "none",
                    "risk_level": "low",
                },
                "candidates": candidate_labels,
                "candidate_payees": [
                    {"payee_id": m["payee_id"], "nickname": m["nickname"], "phone_number": m.get("phone_number")}
                    for m in matches
                ],
            }

        payee_record = matches[0] if matches else None
        exists = payee_record is not None
        if payee_record:
            # Show/confirm against the resolved contact's full name, not whatever
            # shorthand the user typed — same as a phone contacts lookup would.
            intent.payee_name = payee_record["nickname"]
        append_event(req.session_id, "PAYEE_LOOKUP", {"payee_name": intent.payee_name, "exists": exists})

    has_receiver_tracking = bool(payee_record.get("stripe_connected_account_id")) if payee_record else False
    decision = decide_next(
        intent,
        payee_exists=exists,
        payee_has_receiver_tracking=has_receiver_tracking,
        hard_cap_aud=50.0,
    )
    append_event(req.session_id, "DECISION", {
        "decision": decision.decision,
        "reason": decision.reason,
        "required_confirmation": decision.required_confirmation,
        "risk_level": decision.risk_level
    })
    # If decision is CLARIFY/BLOCK, stop here (no pending transaction)
    if decision.decision in ["CLARIFY", "BLOCK"]:
        return {
            "ok": False,
            "intent": intent.model_dump(),
            "payee_exists": exists,
            "decision": {
                "decision": decision.decision,
                "reason": decision.reason,
                "required_confirmation": decision.required_confirmation,
                "risk_level": decision.risk_level,
            },
        }

    # Create a pending transaction record
    payee_id = payee_record["payee_id"] if payee_record else None
    amount_cents = int(round(intent.amount * 100))

    txn_id = create_pending_transaction(
        session_id=req.session_id,
        user_id=DEMO_USER_ID,
        amount_cents=amount_cents,
        currency=intent.currency,
        payee_id=payee_id,
    )
    append_event(req.session_id, "TXN_CREATED", {"txn_id": txn_id, "amount_cents": amount_cents, "currency": intent.currency})

    # Create a confirmation request
    conf = create_confirmation(txn_id=txn_id, user_id=DEMO_USER_ID, required_confirmation=decision.required_confirmation)
    append_event(req.session_id, "CONFIRMATION_CREATED", conf)

    payee_info = None
    payid_suffix = ""
    if payee_record:
        has_receiver_tracking = bool(payee_record.get("stripe_connected_account_id"))
        payee_info = {
            "payee_id": payee_record["payee_id"],
            "nickname": payee_record["nickname"],
            "phone_number": payee_record.get("phone_number"),
            "has_receiver_tracking": has_receiver_tracking,
            "is_saved_contact": bool(payee_record.get("is_contact", 1)),
        }
        if payee_record.get("phone_number"):
            payid_suffix = f" (PayID: {payee_record['phone_number']})"

    read_back = f"Confirm: Pay {intent.currency} {intent.amount:.2f} to {intent.payee_name}{payid_suffix}. " \
                f"Required: {decision.required_confirmation.upper()}"

    return {
        "ok": True,
        "intent": intent.model_dump(),
        "payee_exists": exists,
        "payee": payee_info,
        "decision": {
            "decision": decision.decision,
            "reason": decision.reason,
            "required_confirmation": decision.required_confirmation,
            "risk_level": decision.risk_level,
        },
        "txn_id": txn_id,
        "confirmation": conf,
        "read_back": read_back,
    }



class AddPayeeRequest(BaseModel):
    nickname: str
    type: str = "payid"
    identifier: str = "demo"

from datetime import datetime, timezone
from uuid import uuid4
from db import get_conn

@app.post("/payees/add")
def add_payee(req: AddPayeeRequest):
    payee_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO payees (payee_id, user_id, nickname, type, identifier, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payee_id, DEMO_USER_ID, req.nickname.strip(), req.type, req.identifier, now),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "payee_id": payee_id}


@app.get("/payees")
def list_payees():
    """
    Deliberately saved contacts only (is_contact=1). Recipients auto-provisioned
    from a direct PayID payment (§4a) don't clutter this list until the user
    explicitly saves them via POST /payees/{payee_id}/save_as_contact — but
    they're still fully payable and trackable in the meantime.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT payee_id, nickname, type, identifier, phone_number, stripe_connected_account_id,
                   linked_contact_id, created_at
            FROM payees WHERE user_id=? AND is_contact=1 ORDER BY created_at
            """,
            (DEMO_USER_ID,),
        ).fetchall()
        return {"payees": [dict(r) for r in rows]}
    finally:
        conn.close()


def _duplicate_name_conflict(existing: dict, nickname: str) -> dict:
    return {
        "ok": False,
        "conflict": "duplicate_name",
        "existing_contact": {
            "payee_id": existing["payee_id"],
            "nickname": existing["nickname"],
            "phone_number": existing.get("phone_number"),
        },
        "error": (
            f"You already have a contact named \"{nickname}\" ({existing.get('phone_number') or 'no PayID'}). "
            f"Is this the same person (add as another number for them), or someone different (save under a "
            f"different name)?"
        ),
    }


class SaveContactRequest(BaseModel):
    resolution: str | None = None  # "same_person" | "different_person", only needed on a name conflict
    nickname: str | None = None    # rename, used with resolution == "different_person"


@app.post("/payees/{payee_id}/save_as_contact")
def save_as_contact(payee_id: str, req: SaveContactRequest = SaveContactRequest()):
    """
    One-click "save this PayID for next time" — for a payee that was
    auto-provisioned from a direct PayID payment (is_contact=0), just flips
    the flag so they show up in the Setup contacts list. No re-entry of
    name/phone needed since we already resolved both when the payment ran.

    If the nickname collides with an already-saved contact under a different
    number, this is genuinely ambiguous in the real world (two different
    people can share a name; one person can have two numbers) — so instead of
    silently creating a confusing duplicate, it returns a `duplicate_name`
    conflict and asks the caller to resolve it explicitly via `resolution`.
    """
    payee = get_payee(payee_id)
    if not payee:
        return {"ok": False, "error": "Payee not found"}

    nickname = (req.nickname or payee["nickname"]).strip()
    linked_contact_id = payee.get("linked_contact_id")

    existing = find_saved_contact_by_nickname(DEMO_USER_ID, nickname)
    is_same_number = existing and normalize_payid(existing.get("phone_number") or "") == normalize_payid(payee.get("phone_number") or "")
    if existing and existing["payee_id"] != payee_id and not is_same_number:
        if req.resolution == "same_person":
            linked_contact_id = existing.get("linked_contact_id") or existing["payee_id"]
        elif req.resolution == "different_person":
            linked_contact_id = None
        else:
            return _duplicate_name_conflict(existing, nickname)

    conn = get_conn()
    try:
        conn.execute(
            "UPDATE payees SET is_contact=1, nickname=?, linked_contact_id=? WHERE payee_id=?",
            (nickname, linked_contact_id, payee_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "payee_id": payee_id, "nickname": nickname}


class AddContactRequest(BaseModel):
    nickname: str
    phone_number: str
    resolution: str | None = None  # "same_person" | "different_person", only needed on a name conflict


@app.post("/payees/add_contact")
def add_contact(req: AddContactRequest):
    """
    Creates a payee AND a real (test-mode) Stripe Connect account for them, so
    payments to this contact can later be proven to have actually reached
    someone, not just charged from the payer's card. See
    stripe_service.create_test_connected_account for how instant test-mode
    verification works.

    A nickname that collides with an already-saved contact under a different
    number is genuinely ambiguous (two different people can share a name; one
    person can have a second number) — instead of silently creating a
    confusing duplicate, this returns a `duplicate_name` conflict and asks
    the caller to resolve it explicitly via `resolution`.
    """
    nickname = req.nickname.strip()
    phone_number = req.phone_number.strip()
    if not nickname or not phone_number:
        return {"ok": False, "error": "nickname and phone_number are required"}

    linked_contact_id = None
    existing = find_saved_contact_by_nickname(DEMO_USER_ID, nickname)
    is_same_number = existing and normalize_payid(existing.get("phone_number") or "") == normalize_payid(phone_number)
    if existing and not is_same_number:
        if req.resolution == "same_person":
            linked_contact_id = existing.get("linked_contact_id") or existing["payee_id"]
        elif req.resolution == "different_person":
            linked_contact_id = None
        else:
            return _duplicate_name_conflict(existing, nickname)

    try:
        connected_account_id = create_test_connected_account(nickname)
    except stripe.error.StripeError as e:
        return {"ok": False, "error": "Could not create receiver account", "details": str(e)}

    payee_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO payees
            (payee_id, user_id, nickname, type, identifier, phone_number, stripe_connected_account_id,
             linked_contact_id, created_at)
            VALUES (?, ?, ?, 'stripe', ?, ?, ?, ?, ?)
            """,
            (payee_id, DEMO_USER_ID, nickname, phone_number, phone_number, connected_account_id, linked_contact_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "payee_id": payee_id,
        "phone_number": phone_number,
        "stripe_connected_account_id": connected_account_id,
    }


DEMO_CONTACTS = [
    ("Alice Wonderland", "0400 111 222"),
    ("Bob Marley", "0400 333 444"),
    ("Charlie Chaplin", "0400 555 666"),
]


@app.post("/payees/seed_demo_contacts")
def seed_demo_contacts():
    """
    Creates a small variety of demo contacts (each with its own real,
    test-mode Stripe Connect account) in one call, so you can pay different
    people and compare receiver-side evidence across them.
    """
    created = []
    skipped = []
    for nickname, phone_number in DEMO_CONTACTS:
        if payee_exists_for_user(DEMO_USER_ID, nickname):
            skipped.append(nickname)
            continue
        result = add_contact(AddContactRequest(nickname=nickname, phone_number=phone_number))
        if result["ok"]:
            created.append({"nickname": nickname, **result})
        else:
            skipped.append(nickname)

    return {"ok": True, "created": created, "skipped": skipped}


@app.get("/payees/{payee_id}/balance")
def payee_balance(payee_id: str):
    """
    On-demand receiver-side evidence: queries the contact's own Stripe
    balance directly, so the frontend can show "yes, they actually have the
    money" independent of whatever we recorded at execute-time.
    """
    payee = get_payee(payee_id)
    if not payee:
        return {"ok": False, "error": "Payee not found"}
    if not payee.get("stripe_connected_account_id"):
        return {"ok": False, "error": "This payee has no receiver account to check"}

    try:
        balance = get_account_balance(payee["stripe_connected_account_id"])
    except stripe.error.StripeError as e:
        return {"ok": False, "error": "Could not fetch receiver balance", "details": str(e)}

    return {"ok": True, "payee_id": payee_id, **balance}


def _expire_confirmation(session_id: str, conf: dict) -> dict:
    from confirmations_repo import set_confirmation_status
    set_confirmation_status(conf["confirmation_id"], "expired")
    update_transaction_status(conf["txn_id"], "failed")
    append_event(session_id, "CONFIRMATION_EXPIRED", {
        "confirmation_id": conf["confirmation_id"],
        "txn_id": conf["txn_id"],
        "expires_at": conf["expires_at"],
    })
    return {"ok": False, "error": "Confirmation expired"}


class ConfirmNormalRequest(BaseModel):
    session_id: str
    confirmation_id: str
    phrase: str  # must be "CONFIRM"

@app.post("/confirm/normal")
def confirm_normal(req: ConfirmNormalRequest):
    append_event(req.session_id, "CONFIRM_NORMAL_ATTEMPT", {"confirmation_id": req.confirmation_id})

    conf = get_confirmation(req.confirmation_id)
    if not conf:
        append_event(req.session_id, "CONFIRM_NORMAL_FAILED", {"reason": "confirmation not found"})
        return {"ok": False, "error": "Confirmation not found"}

    if conf["status"] != "pending":
        return {"ok": False, "error": f"Confirmation is {conf['status']}"}

    if is_confirmation_expired(conf):
        return _expire_confirmation(req.session_id, conf)

    if req.phrase.strip().upper() != "CONFIRM":
        append_event(req.session_id, "CONFIRM_NORMAL_FAILED", {"reason": "wrong phrase"})
        return {"ok": False, "error": "Wrong phrase"}

    # Approve
    from confirmations_repo import set_confirmation_status
    set_confirmation_status(req.confirmation_id, "approved")
    update_transaction_status(conf["txn_id"], "confirmed")

    append_event(req.session_id, "CONFIRM_APPROVED", {"confirmation_id": req.confirmation_id, "txn_id": conf["txn_id"]})
    return {"ok": True, "txn_id": conf["txn_id"], "status": "confirmed"}


class ConfirmPinRequest(BaseModel):
    session_id: str
    confirmation_id: str
    pin: str

@app.post("/confirm/pin")
def confirm_pin(req: ConfirmPinRequest):
    append_event(req.session_id, "CONFIRM_PIN_ATTEMPT", {"confirmation_id": req.confirmation_id})

    conf = get_confirmation(req.confirmation_id)
    if not conf:
        append_event(req.session_id, "CONFIRM_PIN_FAILED", {"reason": "confirmation not found"})
        return {"ok": False, "error": "Confirmation not found"}

    if conf["status"] != "pending":
        return {"ok": False, "error": f"Confirmation is {conf['status']}"}

    if is_confirmation_expired(conf):
        return _expire_confirmation(req.session_id, conf)

    # Load user's stored pin_hash
    from db import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT pin_hash FROM users WHERE user_id=?", (conf["user_id"],)).fetchone()
        pin_hash = row["pin_hash"] if row else None
    finally:
        conn.close()

    if not pin_hash:
        append_event(req.session_id, "CONFIRM_PIN_FAILED", {"reason": "pin not set"})
        return {"ok": False, "error": "PIN not set for user"}

    if not verify_pin(req.pin, pin_hash):
        from confirmations_repo import increment_attempts, set_confirmation_status
        attempts = increment_attempts(req.confirmation_id)
        append_event(req.session_id, "CONFIRM_PIN_FAILED", {"reason": "wrong pin", "attempts": attempts})

        if attempts >= int(conf["max_attempts"]):
            set_confirmation_status(req.confirmation_id, "rejected")
            update_transaction_status(conf["txn_id"], "failed")
            append_event(req.session_id, "CONFIRM_REJECTED", {"confirmation_id": req.confirmation_id, "txn_id": conf["txn_id"]})
            return {"ok": False, "error": "Too many attempts. Confirmation rejected."}

        return {"ok": False, "error": "Wrong PIN"}

    # Approve
    from confirmations_repo import set_confirmation_status
    set_confirmation_status(req.confirmation_id, "approved")
    update_transaction_status(conf["txn_id"], "confirmed")

    append_event(req.session_id, "CONFIRM_APPROVED", {"confirmation_id": req.confirmation_id, "txn_id": conf["txn_id"]})
    return {"ok": True, "txn_id": conf["txn_id"], "status": "confirmed"}

from datetime import datetime, timezone
from uuid import uuid4
from db import get_conn

class AddPaymentMethodRequest(BaseModel):
    label: str = "Demo Card"
    stripe_customer_id: str | None = None
    stripe_payment_method_id: str

@app.post("/payment_methods/add")
def add_payment_method(req: AddPaymentMethodRequest):
    pm_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        # Make it default (set others to 0)
        conn.execute("UPDATE payment_methods SET is_default=0 WHERE user_id=?", (DEMO_USER_ID,))
        conn.execute(
            """
            INSERT INTO payment_methods
            (pm_id, user_id, label, stripe_customer_id, stripe_payment_method_id, is_default, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (pm_id, DEMO_USER_ID, req.label, req.stripe_customer_id, req.stripe_payment_method_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "pm_id": pm_id, "is_default": True}


@app.get("/payment_methods")
def list_payment_methods():
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT pm_id, label, stripe_customer_id, stripe_payment_method_id, is_default, created_at
            FROM payment_methods WHERE user_id=? ORDER BY created_at
            """,
            (DEMO_USER_ID,),
        ).fetchall()
        return {"payment_methods": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/payment_methods/seed_test_card")
def seed_test_card():
    """
    Demo/test-mode only: creates a Stripe Customer + attaches Stripe's canned
    test Visa, then saves it as the default payment method. Lets the frontend
    offer a one-click "add a test card" action without collecting real card
    data or embedding Stripe.js/Elements.
    """
    try:
        stripe_customer_id, stripe_pm_id = create_test_payment_method()
    except stripe.error.StripeError as e:
        return {"ok": False, "error": "Could not create test card", "details": str(e)}

    pm_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        conn.execute("UPDATE payment_methods SET is_default=0 WHERE user_id=?", (DEMO_USER_ID,))
        conn.execute(
            """
            INSERT INTO payment_methods
            (pm_id, user_id, label, stripe_customer_id, stripe_payment_method_id, is_default, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (pm_id, DEMO_USER_ID, "Test Visa", stripe_customer_id, stripe_pm_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "pm_id": pm_id, "is_default": True, "label": "Test Visa"}


from transactions_repo import set_stripe_payment_intent

class ExecutePaymentRequest(BaseModel):
    session_id: str
    txn_id: str

@app.post("/pay/execute")
def pay_execute(req: ExecutePaymentRequest):
    # 1) Load transaction
    txn = get_transaction(req.txn_id)
    if not txn:
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {"reason": "txn not found", "txn_id": req.txn_id})
        return {"ok": False, "error": "Transaction not found"}

    # 2) Must be confirmed
    if txn["status"] != "confirmed":
        append_event(req.session_id, "PAY_EXECUTE_BLOCKED", {"reason": "txn not confirmed", "status": txn["status"], "txn_id": req.txn_id})
        return {"ok": False, "error": f"Transaction status must be confirmed (found {txn['status']})"}

    # 3) Get default payment method
    pm = get_default_payment_method(txn["user_id"])
    if not pm or not pm.get("stripe_payment_method_id"):
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {"reason": "no default payment method"})
        return {"ok": False, "error": "No default payment method set"}

    # 3b) If the payee has a receiver (Connect) account, use a destination
    # charge so the funds actually move there instead of just leaving the
    # payer's card with no verifiable second party.
    payee = get_payee(txn["payee_id"]) if txn.get("payee_id") else None
    destination_account_id = payee.get("stripe_connected_account_id") if payee else None

    append_event(req.session_id, "PAY_EXECUTE_START", {
        "txn_id": req.txn_id,
        "amount_cents": txn["amount_cents"],
        "currency": txn["currency"],
        "payment_method_label": pm["label"],
        "destination_account_id": destination_account_id,
    })

    # 4) Call Stripe (test)
    try:
        pi = create_payment_intent(
            amount_cents=int(txn["amount_cents"]),
            currency=txn["currency"],
            payment_method_id=pm["stripe_payment_method_id"],
            customer_id=pm.get("stripe_customer_id"),
            idempotency_key=f"vocalpay_txn_{req.txn_id}",
            destination_account_id=destination_account_id,
        )
    except stripe.error.CardError as e:
        set_stripe_payment_intent(req.txn_id, payment_intent_id="", status="failed")
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {
            "txn_id": req.txn_id, "error_type": "card_error",
            "code": e.code, "message": e.user_message or str(e),
        })
        return {"ok": False, "error": "Card declined", "details": e.user_message or str(e)}
    except (stripe.error.APIConnectionError, stripe.error.RateLimitError) as e:
        set_stripe_payment_intent(req.txn_id, payment_intent_id="", status="failed")
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {
            "txn_id": req.txn_id, "error_type": "transient", "message": str(e),
        })
        return {"ok": False, "error": "Payment service temporarily unavailable, please retry", "details": str(e)}
    except stripe.error.StripeError as e:
        set_stripe_payment_intent(req.txn_id, payment_intent_id="", status="failed")
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {
            "txn_id": req.txn_id, "error_type": "stripe_error", "message": str(e),
        })
        return {"ok": False, "error": "Stripe payment failed", "details": str(e)}
    except Exception as e:
        set_stripe_payment_intent(req.txn_id, payment_intent_id="", status="failed")
        append_event(req.session_id, "PAY_EXECUTE_FAILED", {"txn_id": req.txn_id, "error_type": "unknown", "error": str(e)})
        return {"ok": False, "error": "Stripe payment failed", "details": str(e)}

    # 5) Store result
    status = "succeeded" if pi["status"] in ["succeeded", "requires_capture"] else pi["status"]
    set_stripe_payment_intent(req.txn_id, pi["id"], status=status)

    append_event(req.session_id, "PAY_EXECUTE_SUCCESS", {
        "txn_id": req.txn_id,
        "stripe_payment_intent_id": pi["id"],
        "stripe_status": pi["status"]
    })

    # 6) Receiver-side evidence: if this was a destination charge, confirm the
    # funds actually landed in the payee's own Stripe account by reading
    # *their* balance back — this is Stripe's record, not ours.
    receiver_evidence = None
    if destination_account_id and status == "succeeded":
        try:
            transfer_id = None
            charge_id = pi.get("latest_charge")

            # Stripe attaches the Transfer/balance update to a destination
            # charge asynchronously — usually within ~1-2s even in test mode,
            # so a bare "check right now" can read a balance of 0 despite the
            # charge having already succeeded. Retry briefly rather than
            # report a false negative.
            balance = {"available_cents": 0, "pending_cents": 0, "currency": txn["currency"].lower()}
            confirmed = False
            for _ in range(4):
                if charge_id and not transfer_id:
                    charge = stripe.Charge.retrieve(charge_id)
                    transfer_id = charge.get("transfer")
                balance = get_account_balance(destination_account_id)
                if transfer_id or balance["available_cents"] + balance["pending_cents"] > 0:
                    confirmed = True
                    break
                time.sleep(0.75)

            set_receiver_evidence(req.txn_id, destination_account_id, transfer_id)
            append_event(req.session_id, "RECEIVER_BALANCE_CONFIRMED" if confirmed else "RECEIVER_BALANCE_PENDING", {
                "txn_id": req.txn_id,
                "destination_account_id": destination_account_id,
                "stripe_transfer_id": transfer_id,
                **balance,
            })
            receiver_evidence = {
                "payee_nickname": payee["nickname"],
                "destination_account_id": destination_account_id,
                "stripe_transfer_id": transfer_id,
                "confirmed": confirmed,
                **balance,
            }
        except stripe.error.StripeError as e:
            append_event(req.session_id, "RECEIVER_BALANCE_CHECK_FAILED", {"txn_id": req.txn_id, "error": str(e)})

    return {
        "ok": True,
        "txn_id": req.txn_id,
        "stripe_payment_intent_id": pi["id"],
        "stripe_status": pi["status"],
        "final_status": status,
        "receiver_evidence": receiver_evidence,
    }


# Serve the frontend from this same FastAPI process/origin — one deployed
# service, one URL, and no cross-origin requests to worry about. Mounted last
# so it never shadows an API route registered above (StaticFiles with
# html=True falls back to index.html for any path it doesn't recognize as a
# file, which is what a single-page app needs).
_FRONTEND_DIR = _Path(__file__).resolve().parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
