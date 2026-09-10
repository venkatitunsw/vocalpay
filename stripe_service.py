import os
import time
import stripe

def init_stripe():
    # Strip whitespace defensively — a trailing newline is a common artifact
    # of pasting a secret into a dashboard's env var field, and Stripe's HTTP
    # client rejects it outright ("Invalid header value") rather than
    # trimming it, which otherwise surfaces as a confusing network error deep
    # inside every Stripe call instead of at startup where the cause is clear.
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        raise RuntimeError("Missing STRIPE_SECRET_KEY in environment/.env")
    stripe.api_key = key

def create_test_payment_method():
    """
    Demo/test-mode only: creates a Stripe Customer and attaches Stripe's
    canned test Visa (`pm_card_visa`) to it, so the frontend can offer a
    "seed a test card" action without collecting real card data or
    embedding Stripe.js/Elements.
    """
    customer = stripe.Customer.create()
    pm = stripe.PaymentMethod.attach("pm_card_visa", customer=customer.id)
    stripe.Customer.modify(customer.id, invoice_settings={"default_payment_method": pm.id})
    return customer.id, pm.id


def create_payment_intent(
    amount_cents: int,
    currency: str,
    payment_method_id: str,
    customer_id: str | None = None,
    idempotency_key: str | None = None,
    destination_account_id: str | None = None,
):
    """
    Creates and confirms a PaymentIntent in Stripe test mode.

    When `destination_account_id` is set, this becomes a *destination charge*:
    the payer's card is charged as normal, and Stripe atomically creates a
    Transfer moving the funds to that connected account as part of the same
    charge. That's what lets us prove a specific payee actually received the
    money, instead of just proving the payer's card was charged.
    """
    params = {
        "amount": amount_cents,
        "currency": currency.lower(),
        "payment_method": payment_method_id,
        "confirm": True,
        "off_session": True,  # since we're not using Stripe Checkout UI in this MVP
    }
    if customer_id:
        params["customer"] = customer_id
    if destination_account_id:
        params["transfer_data"] = {"destination": destination_account_id}

    request_options = {}
    if idempotency_key:
        request_options["idempotency_key"] = idempotency_key

    return stripe.PaymentIntent.create(**params, **request_options)


def create_test_connected_account(display_name: str):
    """
    Demo/test-mode only: creates a Stripe Custom Connect account that becomes
    instantly verified and active (transfers capability) using Stripe's
    documented test-mode "magic values" — no hosted onboarding, no manual
    dashboard steps. This is the account that will actually receive funds for
    a given contact/payee, giving us a real second party to check for
    receipt, instead of a bare nickname in our own database.

    `display_name` is split into first/last name for Stripe's individual
    record; it does not need to be the contact's real legal name since this
    never goes through real KYC (test mode only).
    """
    parts = display_name.strip().split(maxsplit=1)
    first_name = parts[0] if parts else "Contact"
    last_name = parts[1] if len(parts) > 1 else "Payee"

    account = stripe.Account.create(
        type="custom",
        country="AU",
        business_type="individual",
        individual={
            "first_name": first_name,
            "last_name": last_name,
            "dob": {"day": 1, "month": 1, "year": 1902},  # magic value: instant verification
            "phone": "0000000000",  # magic value: instant verification
            "email": f"{first_name}.{last_name}.{int(time.time())}@vocalpay-demo.test".lower(),
            "address": {
                "line1": "address_full_match",  # magic value: instant verification
                "city": "Melbourne",
                "state": "VIC",
                "postal_code": "3000",
                "country": "AU",
            },
        },
        business_profile={"url": "https://accessible.stripe.com", "mcc": "4829"},
        external_account={
            "object": "bank_account",
            "country": "AU",
            "currency": "aud",
            "account_holder_name": display_name,
            "routing_number": "110000",   # magic value: instant payout verification
            "account_number": "000123456",  # magic value: instant payout verification
        },
        tos_acceptance={"date": int(time.time()), "ip": "127.0.0.1"},
        capabilities={"transfers": {"requested": True}},
    )
    return account.id


def get_account_balance(account_id: str):
    """
    Returns the connected account's own Stripe balance — this is the
    receiver-side evidence: it's Stripe's record of what that specific
    account holds, not something derived from our own database.
    """
    balance = stripe.Balance.retrieve(stripe_account=account_id)
    available = sum(b["amount"] for b in balance["available"])
    pending = sum(b["amount"] for b in balance["pending"])
    currency = balance["available"][0]["currency"] if balance["available"] else "aud"
    return {"available_cents": available, "pending_cents": pending, "currency": currency}
