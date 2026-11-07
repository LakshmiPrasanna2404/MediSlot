"""MediSlot Lambda authorizer (architecture box 4, TOKEN type, cache TTL 0).

1. Read the JWT signing secret from SSM (SecureString) at cold start.
2. Verify the HS256 signature (algorithm pinned) and the issuer.
3. Check expiry.
4. Check the jti against the token-revocations denylist in DynamoDB.
5. Check the role is allowed on the route: a doctor-only route rejects a
   patient token and vice versa.
6. Return an IAM policy with userId, role, jti and exp in the context.

Outcomes:
    bad, expired, tampered, wrongly signed or logged-out token -> 401
    valid token, wrong role for the route                       -> 403 (Deny policy)

The authorizer cache TTL is 0: with any caching a token used after logout
would still be accepted until the cache entry expired.
"""

import os
import re

from medislot_common import config, logs, tokens

# Most specific first. Paths come from the methodArn, with real values in
# place of path parameters (/doctors/me versus /doctors/<uuid>).
ROLE_RULES = (
    (re.compile(r"^/auth/logout$"), frozenset({"doctor", "patient"})),
    (re.compile(r"^/doctors/me(/.*)?$"), frozenset({"doctor"})),
    (re.compile(r"^/patients/me(/.*)?$"), frozenset({"patient"})),
    (re.compile(r"^/doctors(/[^/]+(/slots)?)?$"), frozenset({"patient"})),
    (re.compile(r"^/appointments(/[^/]+)?$"), frozenset({"patient"})),
    (re.compile(r"^/slots/[^/]+/waitlist$"), frozenset({"patient"})),
    (re.compile(r"^/waitlist-offers/[^/]+/accept$"), frozenset({"patient"})),
)


def allowed_roles(path):
    for pattern, roles in ROLE_RULES:
        if pattern.match(path):
            return roles
    return frozenset()


def path_from_method_arn(method_arn):
    """arn:aws:execute-api:region:account:apiId/stage/METHOD/res/path -> /res/path"""
    try:
        resource = method_arn.split(":", 5)[5]
        parts = resource.split("/")
        return "/" + "/".join(parts[3:])
    except (AttributeError, IndexError):
        return ""


def policy(principal_id, effect, method_arn, context=None):
    result = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": method_arn}],
        },
    }
    if context:
        result["context"] = context
    return result


def reject(reason):
    """API Gateway turns this exact exception message into 401 Unauthorized."""
    logs.metric("TokenValidationRejections", Reason=reason)
    logs.info("token rejected", reason=reason)
    raise Exception("Unauthorized")  # noqa: TRY002 - the message is the API Gateway contract


def bearer_token(event):
    value = event.get("authorizationToken")
    if value is None:
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        value = headers.get("authorization")
    if not isinstance(value, str):
        return None
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def signing_secret():
    secret = config.get_parameter(os.environ["JWT_SECRET_PARAMETER"], decrypt=True).encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("JWT signing secret must be at least 32 bytes")
    return secret


def issuer():
    return config.get_parameter(f"/medislot/{config.environment()}/auth/jwt-issuer")


def lambda_handler(event, context):
    logs.set_correlation_id(getattr(context, "aws_request_id", None), getattr(context, "aws_request_id", None))
    method_arn = event.get("methodArn") or ""
    token = bearer_token(event)
    if not token or not method_arn:
        reject("missing")
    try:
        claims = tokens.verify(token, signing_secret(), issuer())
    except tokens.InvalidToken as exc:
        reject(str(exc))

    try:
        revoked = config.table("TOKEN_REVOCATIONS_TABLE").get_item(Key={"jti": claims["jti"]}, ConsistentRead=True).get("Item")
    except Exception:  # noqa: BLE001 - fail closed if the denylist cannot be read
        logs.exception("revocation lookup failed")
        reject("denylist_unavailable")
    if revoked:
        reject("revoked")

    path = path_from_method_arn(method_arn)
    if claims["role"] not in allowed_roles(path):
        logs.metric("TokenValidationRejections", Reason="wrong_role")
        logs.info("role not allowed on route", role=claims["role"], path=path)
        return policy(claims["sub"], "Deny", method_arn)

    return policy(
        claims["sub"],
        "Allow",
        method_arn,
        {"userId": claims["sub"], "role": claims["role"], "jti": claims["jti"], "exp": str(claims["exp"])},
    )
