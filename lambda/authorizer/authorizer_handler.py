"""MediSlot Lambda authorizer (architecture box 4).

HTTP API Lambda authorizer: REQUEST type, payload format 2.0, simple
responses, no caching (TTL 0) and no identity source, so this function sees
every request, including ones without an Authorization header.

1. Read the JWT signing secret from SSM (SecureString) at cold start.
2. Verify the HS256 signature (algorithm pinned) and the issuer.
3. Check expiry.
4. Check the jti is on the active-tokens list in DynamoDB. A token that is
   not on it (logged out, replaced by a refresh, account removed) is refused.
5. Check the role is allowed on the route: a doctor-only route rejects a
   patient token and vice versa.
6. Return {"isAuthorized": true} with userId, role, jti and exp as context.

Outcomes:
    missing, bad, expired, tampered, wrongly signed or inactive token -> 401
    valid token, wrong role for the route                             -> 403 (isAuthorized false)

The cache TTL is 0: with caching, a token used after logout would still be
accepted until the cache entry expired.

REST API (TOKEN authorizer) events are still answered with an IAM policy, so
requests keep working while a deployment switches the API from REST to HTTP.
"""

import os
import re

from medislot_common import active_tokens, config, logs, tokens

# Most specific first. HTTP API: the path of the route key, with path
# parameters as written in the route (/doctors/{doctorId}). REST API: the path
# from the methodArn, with real values (/doctors/<uuid>). Both match.
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


def route_path(event):
    """The path the role rules are checked against."""
    route = event.get("routeKey")
    if route:
        return route.split(" ", 1)[1] if " " in route else route
    return path_from_method_arn(event.get("methodArn") or "")


def allow(event, claims, context):
    if event.get("methodArn"):
        return policy(claims["sub"], "Allow", event["methodArn"], context)
    return {"isAuthorized": True, "context": context}


def deny(event, claims):
    if event.get("methodArn"):
        return policy(claims["sub"], "Deny", event["methodArn"])
    return {"isAuthorized": False}


def reject(reason):
    """API Gateway turns this exact exception message into 401 Unauthorized."""
    logs.metric("TokenValidationRejections", Reason=reason)
    logs.warning("token rejected", reason=reason)
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
    path = route_path(event)
    token = bearer_token(event)
    if not token or not path:
        reject("missing")
    try:
        claims = tokens.verify(token, signing_secret(), issuer())
    except tokens.InvalidToken as exc:
        reject(str(exc))

    try:
        active = active_tokens.is_active(claims)
    except Exception:  # noqa: BLE001 - fail closed if the list cannot be read
        logs.exception("active token lookup failed")
        reject("active_list_unavailable")
    if not active:
        reject("not_active")

    logs.set_context(userId=claims["sub"], role=claims["role"], path=path)
    if claims["role"] not in allowed_roles(path):
        logs.metric("TokenValidationRejections", Reason="wrong_role")
        logs.warning("role not allowed on route", allowedRoles=sorted(allowed_roles(path)))
        return deny(event, claims)
    logs.info("token accepted", jti=claims["jti"], expiresAt=claims["exp"])
    context_values = {"userId": claims["sub"], "role": claims["role"], "jti": claims["jti"], "exp": str(claims["exp"])}
    return allow(event, claims, context_values)
