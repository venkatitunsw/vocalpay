import re

# Deliberately a separate parser from intent_parser.py's parse_text_command —
# BPAY commands have a different slot shape (biller, CRN, amount) and this
# keeps the already-tested PayID/name payment parsing completely unaffected.

BPAY_KEYWORD_RE = re.compile(r"\bbpay\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
BILLER_CODE_RE = re.compile(r"biller\s*code\s*[:\-]?\s*(?P<code>\d+)", re.IGNORECASE)
CRN_RE = re.compile(r"(?:reference|crn|ref)\s*[:\-]?\s*(?P<crn>\d[\d\s]*\d|\d)", re.IGNORECASE)
BILLER_NAME_RE = re.compile(r"\b(?:pay|send|transfer)\s+(?P<name>[a-zA-Z][a-zA-Z\s]*?)\s+bpay\b", re.IGNORECASE)


class BpayParseResult:
    def __init__(self, ok, biller_name=None, biller_code=None, crn=None, amount=None, missing=None, error=""):
        self.ok = ok
        self.biller_name = biller_name
        self.biller_code = biller_code
        self.crn = crn
        self.amount = amount
        self.missing = missing or []
        self.error = error


def is_bpay_command(text: str) -> bool:
    return bool(BPAY_KEYWORD_RE.search(text or ""))


def parse_bpay_command(text: str) -> BpayParseResult:
    """
    Extracts whatever BPAY slots are present in free text: a biller name
    and/or explicit code, a CRN/reference, and an amount. Doesn't resolve the
    biller name against the directory itself (that needs the DB — see
    bpay_repo.lookup_biller / find callers in main.py) — `missing` here only
    reflects what's absent from the text itself; the endpoint computes the
    final missing-slot list once the biller name/code has been resolved.
    """
    if not is_bpay_command(text):
        return BpayParseResult(False, error="Not a BPAY command")

    biller_code_m = BILLER_CODE_RE.search(text)
    crn_m = CRN_RE.search(text)
    name_m = BILLER_NAME_RE.search(text)

    # Scrub the labelled biller-code/CRN numbers out before scanning for the
    # amount, so "biller code 12345" / "reference 9876543210" never get
    # mistaken for the payment amount.
    scrubbed = BILLER_CODE_RE.sub(" ", text)
    scrubbed = CRN_RE.sub(" ", scrubbed)
    amounts = [float(a) for a in AMOUNT_RE.findall(scrubbed)]

    biller_name = name_m.group("name").strip() if name_m else None
    biller_code = biller_code_m.group("code") if biller_code_m else None
    crn = re.sub(r"\s+", "", crn_m.group("crn")) if crn_m else None
    amount = amounts[-1] if amounts else None

    missing = []
    if not biller_code and not biller_name:
        missing.append("biller")
    if not crn:
        missing.append("crn")
    if amount is None:
        missing.append("amount")

    return BpayParseResult(
        True, biller_name=biller_name, biller_code=biller_code, crn=crn, amount=amount, missing=missing
    )
