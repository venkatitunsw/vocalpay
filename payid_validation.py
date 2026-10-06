import re

_MOBILE_RE = re.compile(r"^(?:\+?61|0)?4\d{8}$")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


class PayIDValidationError(ValueError):
    pass


def _digits(raw: str) -> str:
    return re.sub(r"[\s\-()]", "", raw)


def normalize_mobile(raw: str) -> str:
    compact = _digits(raw)
    if compact.startswith("+"):
        compact = compact[1:]
    if re.fullmatch(r"61\d+", compact) and len(compact) == 11:
        compact = "0" + compact[2:]
    if not re.fullmatch(r"\d+", compact):
        raise PayIDValidationError("A mobile PayID can only contain digits, spaces, or a leading +61.")
    if len(compact) != 10:
        raise PayIDValidationError(f"That looks like a phone number with {len(compact)} digits. An Australian mobile has 10.")
    if not compact.startswith("04"):
        raise PayIDValidationError("An Australian mobile PayID starts with 04 (or +61 4).")
    return compact


def normalize_email(raw: str) -> str:
    value = raw.strip().lower()
    if " " in value or value.count("@") != 1:
        raise PayIDValidationError("An email PayID needs exactly one @ and no spaces.")
    if not _EMAIL_RE.match(value):
        raise PayIDValidationError("That email address doesn't look valid (check the domain, e.g. name@example.com).")
    return value


def validate_abn(raw: str) -> str:
    digits = _digits(raw)
    if not re.fullmatch(r"\d{11}", digits):
        raise PayIDValidationError("An ABN PayID must be exactly 11 digits.")
    adjusted = [int(digits[0]) - 1] + [int(d) for d in digits[1:]]
    if sum(w * d for w, d in zip(_ABN_WEIGHTS, adjusted)) % 89 != 0:
        raise PayIDValidationError("That ABN fails the check-digit test, so it isn't a valid ABN.")
    return digits


def classify_payid(raw: str) -> tuple[str, str]:
    """Returns (pay_id_type, normalized_value). Raises PayIDValidationError with a reason."""
    text = (raw or "").strip()
    if not text:
        raise PayIDValidationError("Enter a mobile number, email address, or ABN.")
    if "@" in text:
        return "email", normalize_email(text)
    compact = _digits(text)
    if re.fullmatch(r"\d{11}", compact) and not compact.startswith(("04", "61")):
        return "abn", validate_abn(compact)
    return "mobile", normalize_mobile(text)
