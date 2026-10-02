import base64
import os

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from passkey_repo import get_credential, list_credentials, save_credential, update_sign_count

# Must match the domain actually serving the frontend: "localhost" for local
# dev, the real hostname (e.g. "vocalpay.onrender.com", no scheme/port) in
# production. A mismatch here is the #1 cause of WebAuthn failures.
RP_ID = os.getenv("WEBAUTHN_RP_ID", "localhost")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "VocalPay")
# The exact origin(s) the browser sends, scheme+host[+port], no trailing slash.
ORIGIN = os.getenv("WEBAUTHN_ORIGIN", "http://localhost:8001")

# Registration challenges, keyed by user_id -- short-lived, single-process.
# Known limitation (documented, not hidden): this resets on restart and
# wouldn't survive multiple server workers. A production deployment would
# persist this server-side (e.g. a short-lived table) the same way
# authentication challenges already are (see confirmations.challenge).
_pending_registration_challenges: dict[str, bytes] = {}


def begin_registration(user_id: str, user_name: str) -> dict:
    existing = list_credentials(user_id)
    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_id.encode("utf-8"),
        user_name=user_name,
        user_display_name=user_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["credential_id"] + "=="))
            for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _pending_registration_challenges[user_id] = options.challenge
    return webauthn.helpers.options_to_json(options)


def finish_registration(user_id: str, credential: dict, label: str | None = None) -> dict:
    challenge = _pending_registration_challenges.pop(user_id, None)
    if challenge is None:
        return {"ok": False, "error": "No pending registration for this user -- call begin_registration first"}

    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
        )
    except Exception as e:
        return {"ok": False, "error": f"Registration verification failed: {e}"}

    credential_id_b64url = base64.urlsafe_b64encode(verified.credential_id).decode().rstrip("=")
    save_credential(
        user_id=user_id,
        credential_id=credential_id_b64url,
        public_key_cbor=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=credential.get("response", {}).get("transports") if isinstance(credential, dict) else None,
        label=label,
    )
    return {"ok": True, "credential_id": credential_id_b64url}


def begin_authentication(user_id: str) -> tuple[dict, str]:
    """Returns (options_json, challenge_b64url) -- the caller persists the
    challenge against the specific confirmation this assertion is for."""
    credentials = list_credentials(user_id)
    if not credentials:
        raise ValueError("No passkey registered for this user")

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["credential_id"] + "=="))
            for c in credentials
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    challenge_b64url = base64.urlsafe_b64encode(options.challenge).decode().rstrip("=")
    return webauthn.helpers.options_to_json(options), challenge_b64url


def finish_authentication(credential: dict, expected_challenge_b64url: str) -> dict:
    cred_id_raw = credential["id"] if isinstance(credential, dict) else credential.id
    stored = get_credential(cred_id_raw)
    if not stored:
        return {"ok": False, "error": "Unknown credential -- was it registered?"}

    expected_challenge = base64.urlsafe_b64decode(expected_challenge_b64url + "==")
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=stored["public_key_cbor"],
            credential_current_sign_count=stored["sign_count"],
        )
    except Exception as e:
        return {"ok": False, "error": f"Passkey verification failed: {e}"}

    # Cloned-authenticator detection: a legitimate authenticator's sign count
    # only ever increases. A count that didn't grow means either a resident
    # key authenticator (count legitimately stays 0) or a cloned credential
    # replaying an old assertion -- only the latter is actually suspicious,
    # so only flag it when the stored count was already nonzero.
    if verified.new_sign_count <= stored["sign_count"] and stored["sign_count"] > 0:
        return {"ok": False, "error": "Sign count did not increase -- possible cloned authenticator"}

    update_sign_count(cred_id_raw, verified.new_sign_count)
    return {"ok": True}
