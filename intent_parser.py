import re
from typing import Optional
from models import PaymentIntentParsed

VERB_RE = re.compile(r"^\s*(pay|send|transfer)\b", re.IGNORECASE)

AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")

# Payee/target triggers, tried in priority order so the parser understands
# several common phrasings instead of one rigid template:
#   "... to <target> [for <note>]"          e.g. "Pay 12 to John for dinner"
#   "... on <target>['s] account"           e.g. "Pay 30 AUD on Alice account"
#   "... for <target>"                      fallback when neither above appears
# The target character class includes ":" and parens so an explicit PayID
# qualifier attached to a name — "Ava Hill PayID: 0427 499 675" or
# "Ava Hill (0427 499 675)" — parses as one target instead of failing outright.
_TARGET_CHARS = r"[a-zA-Z0-9\s\.\-':()]"
_TARGET_STOP = r"(?=\s+for\b|[,.]|$)"
TO_TARGET_RE = re.compile(rf"\bto\s+(?P<target>[a-zA-Z0-9]{_TARGET_CHARS}*?){_TARGET_STOP}", re.IGNORECASE)
ON_ACCOUNT_RE = re.compile(rf"\bon\s+(?P<target>[a-zA-Z0-9]{_TARGET_CHARS}*?)(?:'s)?\s+account\b", re.IGNORECASE)
# The bare "for X" fallback also stops before a trailing amount (e.g. "for
# John 12 dollars"), unlike "to"/"on account" which must stay greedy enough
# to keep a spaced-out PayID ("to 0412 345 678") intact as one target.
FOR_TARGET_RE = re.compile(rf"\bfor\s+(?P<target>[a-zA-Z0-9]{_TARGET_CHARS}*?)(?=\s+for\b|\s+\d|[,.]|$)", re.IGNORECASE)
NOTE_RE = re.compile(r"\bfor\s+(?P<note>.+)$", re.IGNORECASE)
TRAILING_ACCOUNT_RE = re.compile(r"(?:'s)?\s+account\s*$", re.IGNORECASE)
# A name qualified with an explicit PayID — "Ava Hill PayID: 0427 499 675" or
# "Ava Hill (0427 499 675)" — should resolve by that number, not the name,
# since the number is unambiguous even when two contacts share a name.
EXPLICIT_PAYID_RE = re.compile(r"(?:\bpayid\b\s*[:\-]?\s*|\()(?P<number>\d[\d\s\-]{6,13}\d)\)?", re.IGNORECASE)


class ParseResult:
    def __init__(self, ok: bool, intent: Optional[PaymentIntentParsed] = None, error: str = ""):
        self.ok = ok
        self.intent = intent
        self.error = error


def parse_text_command(text: str) -> ParseResult:
    """
    Understands the *meaning* of a payment command rather than one fixed
    template. Handles:
      - "Pay 12 to John"
      - "Pay 12 dollars to John for dinner"
      - "Pay 30 AUD on Alice account"                (alternate "on ... account" phrasing)
      - "Pay 20, no 30 to Smith"                      (self-correction: last-mentioned amount wins)
      - "Pay 12 to 0412 345 678"                      (PayID instead of a name)
      - "Pay 4 to Ava Hill PayID: 0427 499 675"        (name + explicit PayID disambiguates which one)
      - "Pay 4 to Ava Hill (0427 499 675)"
      - "Send John 12 aud"                             (no preposition at all: verb, name, amount)
    """
    if not text or len(text.strip()) < 3:
        return ParseResult(False, error="Empty or too short command")

    raw = text.strip()

    if not VERB_RE.match(raw):
        return ParseResult(False, error="Could not parse command. Try: 'Pay 12 to John'")

    # 1) Resolve the target (payee name or PayID) using whichever trigger
    # phrase appears — "to X" first, then "on X('s) account", then a bare
    # "for X" as a last resort.
    target = None
    target_span = None
    for pattern in (TO_TARGET_RE, ON_ACCOUNT_RE):
        m = pattern.search(raw)
        if m:
            target, target_span = m.group("target").strip(), m.span()
            break
    if target is None:
        m = FOR_TARGET_RE.search(raw)
        if m:
            target, target_span = m.group("target").strip(), m.span()

    if target is None:
        # No preposition at all — "Send John 12 aud" / "Pay Smith 20". The
        # target is whatever sits between the verb and the first number.
        verb_m = VERB_RE.match(raw)
        after_verb = raw[verb_m.end():]
        digit_m = re.search(r"\d", after_verb)
        if digit_m and digit_m.start() > 0:
            candidate = after_verb[: digit_m.start()].strip(" ,$")
            if re.match(r"^[a-zA-Z]", candidate):
                target = candidate
                target_span = (verb_m.end(), verb_m.end() + digit_m.start())

    if not target:
        return ParseResult(False, error="Could not figure out who to pay. Try: 'Pay 12 to John'")

    # An explicit PayID qualifier attached to a name always wins — it's
    # unambiguous even when the name alone isn't (e.g. two saved "Ava Hill"s).
    payid_m = EXPLICIT_PAYID_RE.search(target)
    if payid_m:
        target = payid_m.group("number")
    else:
        # Strip a stray trailing "'s account"/"account" if the "to"/"for"
        # trigger swallowed it (e.g. "to Alice's account").
        target = TRAILING_ACCOUNT_RE.sub("", target).strip()

    if not target:
        return ParseResult(False, error="Could not figure out who to pay. Try: 'Pay 12 to John'")

    # 2) Note: "for <text>" appearing after the resolved target.
    note = ""
    note_m = NOTE_RE.search(raw[target_span[1]:])
    if note_m:
        note = note_m.group("note").strip().rstrip(".")

    # 3) Amount: numbers mentioned before the target trigger. When more than
    # one appears, the LAST one wins — this is what makes a self-correction
    # like "pay 20, no 30 to Smith" resolve to 30 without needing to detect
    # correction words explicitly.
    before_target = raw[: target_span[0]]
    amounts = [float(a) for a in AMOUNT_RE.findall(before_target)]
    if not amounts:
        # Less common phrasing where the amount trails the target instead,
        # e.g. "Pay to John 12 dollars" — fall back to scanning after it.
        amounts = [float(a) for a in AMOUNT_RE.findall(raw[target_span[1]:])]
    if not amounts:
        return ParseResult(False, error="Could not find an amount to pay. Try: 'Pay 12 to John'")

    amount = amounts[-1]
    currency = "AUD"  # MVP: force AUD

    intent = PaymentIntentParsed(amount=amount, currency=currency, payee_name=target, note=note)
    return ParseResult(True, intent=intent)
