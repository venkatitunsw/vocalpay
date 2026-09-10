from dataclasses import dataclass
from typing import Literal, Optional
from models import PaymentIntentParsed

DecisionType = Literal["PROCEED", "STEP_UP", "CLARIFY", "BLOCK"]

@dataclass
class Decision:
    decision: DecisionType
    reason: str
    required_confirmation: str  # "normal" | "pin" | "webauthn" (later)
    risk_level: str             # "low" | "medium" | "high"

DEFAULT_HARD_CAP_AUD = 50.0

def decide_next(
    intent: PaymentIntentParsed,
    payee_exists: bool,
    payee_has_receiver_tracking: bool = False,
    hard_cap_aud: float = DEFAULT_HARD_CAP_AUD,
) -> Decision:
    """
    Very clear, beginner-friendly rules.
    You can expand later with more rules (rate limiting, time-of-day risk, etc.).
    """

    # Basic sanity
    if intent.amount <= 0:
        return Decision("BLOCK", "Amount must be > 0", "none", "high")

    # Unknown name => refuse outright rather than charging a card against a
    # label nobody can prove is a real recipient. The caller must add them
    # as a contact (with a PayID) first.
    if not payee_exists:
        return Decision(
            "BLOCK",
            f"\"{intent.payee_name}\" is not a saved contact. Add them as a contact with a "
            f"PayID in Setup before paying.",
            "none",
            "high",
        )

    # Known nickname but no PayID/receiver account on file (e.g. added via the
    # legacy bare /payees/add) => still refuse. Money must be paid to a
    # specific contact's PayID, not just a label in our own database.
    if not payee_has_receiver_tracking:
        return Decision(
            "BLOCK",
            f"{intent.payee_name} has no PayID on file. Add them as a contact with a phone "
            f"number/PayID in Setup before paying.",
            "none",
            "high",
        )

    # Amount cap
    if intent.amount > hard_cap_aud:
        return Decision("STEP_UP", f"Amount exceeds hard cap {hard_cap_aud} AUD", "pin", "high")

    return Decision("PROCEED", "Within cap and payee known", "normal", "low")
