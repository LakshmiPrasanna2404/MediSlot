
"""MediSlot API Gateway Lambda authorizer.

Validates HS256 access JWTs and checks token revocation.
"""
import base64
import hashlib
import hmac
import json
import os
import time

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
SSM = boto3.client("ssm", region_name=REGION)
DDB = boto3.resource("dynamodb", region_name=REGION)

PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
TABLE = DDB.Table(os.environ["TOKEN_REVOCATIONS_TABLE"])

# Optional claim checks. Configure these environment variables only when
# the token issuer uses matching values.
EXPECTED_ISSUER = os.environ.get("JWT_ISSUER")
EXPECTED_AUDIENCE = os.environ.get("JWT_AUDIENCE")

_cache = None


def secret():
    """Load the signing secret once per Lambda execution environment."""
    global _cache

    if _cache is None:
        response = SSM.get_parameter(
            Name=PARAMETER,
            WithDecryption=True,
        )
        _cache = response["Parameter"]["Value"].encode("utf-8")

    return _cache


def _b64url_decode(value):
    """Decode a base64url string, including omitted padding."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def decode(token):
    """Return validated access-token claims, or None if invalid."""
    if not isinstance(token, str) or not token:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        head, body, signature = parts

        header = json.loads(_b64url_decode(head))
        claims = json.loads(_b64url_decode(body))

        if not isinstance(header, dict) or not isinstance(claims, dict):
            return None

        # Accept only the expected signing algorithm.
        if header.get("alg") != "HS256":
            return None

        expected = base64.urlsafe_b64encode(
            hmac.new(
                secret(),
                f"{head}.{body}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        ).rstrip(b"=").decode("ascii")

        if not hmac.compare_digest(expected, signature):
            return None

        # Require an access token with a valid expiration.
        if claims.get("typ") != "access":
            return None

        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None

        if exp <= int(time.time()):
            return None

        subject = claims.get("sub")
        jti = claims.get("jti")

        if not isinstance(subject, str) or not subject.strip():
            return None

        if not isinstance(jti, str) or not jti.strip():
            return None

        email = claims.get("email")
        if email is not None and not isinstance(email, str):
            return None

        role = claims.get("role", "patient")
        if role not in ("patient", "doctor", "admin"):
            return None

        # Enforce issuer/audience only when explicitly configured.
        if EXPECTED_ISSUER and claims.get("iss") != EXPECTED_ISSUER:
            return None

        if EXPECTED_AUDIENCE:
            audience = claims.get("aud")
            if isinstance(audience, str):
                valid_audience = audience == EXPECTED_AUDIENCE
            elif isinstance(audience, list):
                valid_audience = EXPECTED_AUDIENCE in audience
            else:
                valid_audience = False

            if not valid_audience:
                return None

        return claims

    except Exception:
        # Do not expose token contents or secrets in logs.
        return None


def policy(principal, effect, resource, claims=None):
    """Build an API Gateway Lambda authorizer response."""
    result = {
        "principalId": str(principal) or "unauthorized",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource or "*",
                }
            ],
        },
    }

    if claims:
        result["context"] = {
            "userId": str(claims["sub"]),
            "email": str(claims.get("email", "")),
            "role": str(claims.get("role", "patient")),
        }

    return result


def lambda_handler(event, context):
    """Validate the Authorization header and token revocation status."""
    headers = {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }

    auth = headers.get("authorization", "")
    resource = (
        event.get("methodArn")
        or event.get("routeArn")
        or "*"
    )

    if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
        return policy("unauthorized", "Deny", resource)

    parts = auth.split(None, 1)
    if len(parts) != 2 or not parts[1].strip():
        return policy("unauthorized", "Deny", resource)

    claims = decode(parts[1].strip())
    if not claims:
        return policy("unauthorized", "Deny", resource)

    try:
        response = TABLE.get_item(
            Key={"jti": claims["jti"]},
            ConsistentRead=True,
        )

        if response.get("Item"):
            return policy("unauthorized", "Deny", resource)

    except Exception:
        # Fail closed: do not authorize when revocation status is unknown.
        print("Revocation check failed; denying request")
        return policy("unauthorized", "Deny", resource)

    return policy(claims["sub"], "Allow", resource, claims)
