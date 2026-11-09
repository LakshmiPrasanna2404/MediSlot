"""HS256 access tokens, hand-built (no managed identity service, no JWT library).

Payload: exactly five claims, as the frozen rules require:
    sub (userId), role ("doctor" | "patient"), iat, exp, jti

The issuer is checked on every request. Because the payload must carry
exactly those five claims, the issuer travels in the signed JWT header
("iss", RFC 7519 section 5.3 allows replicating a claim as a header
parameter). The header is part of the signing input, so it cannot be changed
without breaking the signature.

The algorithm is pinned to HS256: a token that says "none" or anything else
is rejected before the signature is even computed.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid

CLAIMS = frozenset({"sub", "role", "iat", "exp", "jti"})
ROLES = ("doctor", "patient")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidToken(Exception):
    """Raised with a short reason; the reason is logged, never returned."""


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment):
    if not segment or not _SEGMENT.match(segment):
        raise InvalidToken("malformed")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _sign(secret, signing_input):
    return _b64url(hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest())


def issue(user_id, role, secret, issuer, lifetime_seconds, now=None):
    """Return (token, claims)."""
    if role not in ROLES:
        raise ValueError("role must be doctor or patient")
    issued_at = int(now if now is not None else time.time())
    claims = {
        "sub": str(user_id),
        "role": role,
        "iat": issued_at,
        "exp": issued_at + int(lifetime_seconds),
        "jti": str(uuid.uuid4()),
    }
    header = {"alg": "HS256", "typ": "JWT", "iss": issuer}
    head = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{head}.{body}.{_sign(secret, f'{head}.{body}')}", claims


def verify(token, secret, issuer, now=None):
    """Return the claims of a valid token, or raise InvalidToken(reason)."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidToken("malformed")
    head, body, signature = token.split(".")
    try:
        header = json.loads(_b64url_decode(head))
        claims = json.loads(_b64url_decode(body))
    except (ValueError, UnicodeDecodeError):
        raise InvalidToken("malformed") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidToken("malformed")
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise InvalidToken("algorithm")
    if not _SEGMENT.match(signature or "") or not hmac.compare_digest(_sign(secret, f"{head}.{body}"), signature):
        raise InvalidToken("signature")
    if header.get("iss") != issuer:
        raise InvalidToken("issuer")
    if set(claims) != CLAIMS:
        raise InvalidToken("claims")
    if claims["role"] not in ROLES:
        raise InvalidToken("role")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise InvalidToken("subject")
    if not isinstance(claims["jti"], str) or not claims["jti"]:
        raise InvalidToken("jti")
    if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
        raise InvalidToken("timestamps")
    current = int(now if now is not None else time.time())
    if claims["exp"] <= current:
        raise InvalidToken("expired")
    if claims["iat"] > current + 60 or claims["exp"] <= claims["iat"]:
        raise InvalidToken("timestamps")
    return claims


def unverified_claims(token):
    """Read the payload WITHOUT verifying it. Only for UI routing in the web app."""
    try:
        return json.loads(_b64url_decode(token.split(".")[1]))
    except (InvalidToken, ValueError, IndexError, AttributeError):
        return {}


def refresh_token_hash(raw_token):
    """Refresh tokens are stored only as SHA-256 hashes."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
