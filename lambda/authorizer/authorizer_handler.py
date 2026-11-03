"""MediSlot API Gateway Lambda authorizer.

Validates the five-claim HS256 access JWT issued by auth_handler.py,
checks the token-revocations DynamoDB table, and returns the IAM policy
and authenticated user context required by API Gateway payload format 1.0.

Required environment variables:
    JWT_SECRET_PARAMETER
    TOKEN_REVOCATIONS_TABLE
    ENVIRONMENT

SSM setting:
    /medislot/{ENVIRONMENT}/auth/access-token-seconds

DynamoDB schema:
    token-revocations partition key: jti (String)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time

import boto3
from botocore.config import Config


REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT = os.environ["ENVIRONMENT"]
CONFIG_PREFIX = f"/medislot/{ENVIRONMENT}/auth"

CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=4,
    retries={"total_max_attempts": 2},
)

SSM = boto3.client("ssm", region_name=REGION, config=CLIENT_CONFIG)
DDB = boto3.resource("dynamodb", region_name=REGION, config=CLIENT_CONFIG)

JWT_SECRET_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
TOKEN_REVOCATIONS_TABLE = DDB.Table(os.environ["TOKEN_REVOCATIONS_TABLE"])

_secret_cache = None
_access_token_seconds_cache = None


def secret():
    """Load the JWT signing secret from SSM SecureString once per container."""
    global _secret_cache

    if _secret_cache is None:
        response = SSM.get_parameter(
            Name=JWT_SECRET_PARAMETER,
            WithDecryption=True,
        )
        value = response.get("Parameter", {}).get("Value")

        if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
            raise RuntimeError("JWT signing secret must be at least 32 bytes")

        _secret_cache = value.encode("utf-8")

    return _secret_cache


def access_token_seconds():
    """Read the same configurable access-token lifetime used by auth_handler."""
    global _access_token_seconds_cache

    if _access_token_seconds_cache is None:
        response = SSM.get_parameter(
            Name=f"{CONFIG_PREFIX}/access-token-seconds",
            WithDecryption=False,
        )
        value = response.get("Parameter", {}).get("Value")

        try:
            configured_seconds = int(value)
        except (TypeError, ValueError):
            raise RuntimeError("Invalid access-token-seconds setting")

        if configured_seconds <= 0:
            raise RuntimeError("access-token-seconds must be positive")

        _access_token_seconds_cache = configured_seconds

    return _access_token_seconds_cache


def b64url_decode(value):
    """Decode a strictly formatted base64url JWT segment."""
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid JWT segment")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url characters")

    padded = value + ("=" * (-len(value) % 4))
    return base64.b64decode(
        padded.encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


def decode(token):
    """Return valid access-token claims, or None when the token is invalid."""
    if not isinstance(token, str) or not token:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_part, payload_part, signature_part = parts
        header = json.loads(b64url_decode(header_part))
        claims = json.loads(b64url_decode(payload_part))

        if not isinstance(header, dict) or not isinstance(claims, dict):
            return None

        # auth_handler.py signs tokens with HS256 and header typ=JWT.
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None

        expected_signature = base64.urlsafe_b64encode(
            hmac.new(
                secret(),
                f"{header_part}.{payload_part}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        ).rstrip(b"=").decode("ascii")

        if not hmac.compare_digest(expected_signature, signature_part):
            return None

        # auth_handler.py intentionally issues exactly these five claims:
        # sub, role, iat, exp, jti. Do not require a separate typ/iss/aud claim.
        if set(claims.keys()) != {"sub", "role", "iat", "exp", "jti"}:
            return None

        subject = claims.get("sub")
        role = claims.get("role")
        token_id = claims.get("jti")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")

        if not isinstance(subject, str) or not subject.strip():
            return None
        if not isinstance(token_id, str) or not token_id.strip():
            return None
        if role not in ("patient", "doctor"):
            return None
        if type(issued_at) is not int or type(expires_at) is not int:
            return None

        now = int(time.time())
        if issued_at > now or expires_at <= now:
            return None

        lifetime = access_token_seconds()
        if expires_at - issued_at != lifetime:
            return None

        return claims

    except Exception:
        # Never log or expose token contents, claims, or signing secrets.
        return None


def build_policy(principal_id, effect, resource, claims=None):
    """Build an API Gateway IAM policy response for payload format 1.0."""
    result = {
        "principalId": str(principal_id),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
    }

    if claims is not None:
        result["context"] = {
            "userId": claims["sub"],
            "role": claims["role"],
        }

    return result


def unauthorized():
    """API Gateway maps this Lambda authorizer exception to HTTP 401."""
    raise Exception("Unauthorized")


def lambda_handler(event, context):
    """Authenticate the bearer token and check its revocation status."""
    if not isinstance(event, dict):
        unauthorized()

    # HTTP API Lambda authorizer payload format 1.0 supplies methodArn.
    # Do not fall back to a wildcard resource.
    resource = event.get("methodArn")
    if not isinstance(resource, str) or not resource:
        unauthorized()

    headers = {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }

    authorization = headers.get("authorization")
    if not isinstance(authorization, str):
        unauthorized()

    parts = authorization.strip().split(None, 1)
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not parts[1].strip()
    ):
        unauthorized()

    claims = decode(parts[1].strip())
    if claims is None:
        unauthorized()

    try:
        # Final token-revocations table schema: partition key jti (String).
        result = TOKEN_REVOCATIONS_TABLE.get_item(
            Key={"jti": claims["jti"]},
            ConsistentRead=True,
        )

        if result.get("Item"):
            unauthorized()

    except Exception as exc:
        # Fail closed if revocation status cannot be established.
        if str(exc) == "Unauthorized":
            raise
        print(f"Revocation lookup failed: {type(exc).__name__}")
        unauthorized()

    return build_policy(
        principal_id=claims["sub"],
        effect="Allow",
        resource=resource,
        claims=claims,
    )
    