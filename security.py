import os
import hashlib
import hmac
import base64

def hash_pin(pin: str, salt: bytes | None = None) -> str:
    """
    PBKDF2 hash. Stored format: base64(salt)$base64(hash)
    """
    if salt is None:
        salt = os.urandom(16)
    pin_bytes = pin.encode("utf-8")
    dk = hashlib.pbkdf2_hmac("sha256", pin_bytes, salt, 200_000)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(dk_b64.encode())
    except Exception:
        return False

    pin_bytes = pin.encode("utf-8")
    actual = hashlib.pbkdf2_hmac("sha256", pin_bytes, salt, 200_000)
    return hmac.compare_digest(actual, expected)
