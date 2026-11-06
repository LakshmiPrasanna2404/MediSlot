"""
MediSlot Authentication Lambda.
Doctor/patient registration and login, opaque refresh-token rotation,
and logout.
Passwords use bcrypt.
Authentication settings and secrets are loaded from SSM.
Refresh tokens are stored only as SHA-256 hashes in DynamoDB.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import traceback
import uuid
import boto3
import bcrypt
import pymysql
from botocore.config import Config
from botocore.exceptions import ClientError
# ---------------------------------------------------------
# AWS CONFIGURATION
# ---------------------------------------------------------
REGION = os.environ["AWS_REGION"]
ENVIRONMENT = os.environ["ENVIRONMENT"]
CONFIG_PREFIX = f"/medislot/{ENVIRONMENT}/auth"
AWS_CONFIG = Config(
    connect_timeout=3,
    read_timeout=4,
    retries={"total_max_attempts": 2},
)
SSM = boto3.client(
    "ssm",
    region_name=REGION,
    config=AWS_CONFIG,
)
DDB = boto3.resource(
    "dynamodb",
    region_name=REGION,
    config=AWS_CONFIG,
)
# Low-level client for transactions that use typed values ({"S": ...}, {"N": ...}).
# Do NOT use DDB.meta.client for these: the resource's client auto-serializes
# values, so typed dicts would be stored as Maps and fail key-schema validation.
DDB_CLIENT = boto3.client(
    "dynamodb",
    region_name=REGION,
    config=AWS_CONFIG,
)
TOKEN_TABLE = DDB.Table(
    os.environ["TOKEN_REVOCATIONS_TABLE"]
)
REFRESH_TABLE = DDB.Table(
    os.environ["REFRESH_TOKENS_TABLE"]
)
JWT_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
DB_USERNAME_PARAMETER = os.environ["DB_USERNAME_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ["DB_PORT"])
DB_NAME = os.environ["DB_NAME"]
_secret = None
_credentials = None
_settings = None
# ---------------------------------------------------------
# COMMON RESPONSE
# ---------------------------------------------------------
def response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload),
    }
# ---------------------------------------------------------
# SSM CONFIGURATION
# ---------------------------------------------------------
def parameter_value(name, decrypt=False):
    result = SSM.get_parameter(
        Name=name,
        WithDecryption=decrypt,
    )
    return result["Parameter"]["Value"]
def settings():
    global _settings
    if _settings is None:
        _settings = {
            "access_token_seconds": int(
                parameter_value(
                    f"{CONFIG_PREFIX}/access-token-seconds"
                )
            ),
            "refresh_token_seconds": int(
                parameter_value(
                    f"{CONFIG_PREFIX}/refresh-token-seconds"
                )
            ),
            "bcrypt_rounds": int(
                parameter_value(
                    f"{CONFIG_PREFIX}/bcrypt-rounds"
                )
            ),
            "password_min_length": int(
                parameter_value(
                    f"{CONFIG_PREFIX}/password-min-length"
                )
            ),
        }
        if not 4 <= _settings["bcrypt_rounds"] <= 16:
            raise RuntimeError(
                "bcrypt-rounds must be between 4 and 16"
            )
        if _settings["password_min_length"] <= 0:
            raise RuntimeError(
                "password-min-length must be positive"
            )
        if (
            _settings["access_token_seconds"] <= 0
            or _settings["refresh_token_seconds"] <= 0
        ):
            raise RuntimeError(
                "Token lifetimes must be positive"
            )
    return _settings
def jwt_secret():
    global _secret
    if _secret is None:
        _secret = parameter_value(
            JWT_PARAMETER,
            decrypt=True,
        ).encode("utf-8")
        if len(_secret) < 32:
            raise RuntimeError(
                "JWT signing secret must be at least 32 bytes"
            )
    return _secret
def db_credentials():
    global _credentials
    if _credentials is None:
        username = parameter_value(
            DB_USERNAME_PARAMETER,
            decrypt=True,
        )
        password = parameter_value(
            DB_PASSWORD_PARAMETER,
            decrypt=True,
        )
        if not username or not password:
            raise RuntimeError(
                "Database credentials are empty"
            )
        _credentials = {
            "username": username,
            "password": password,
        }
    return _credentials
# ---------------------------------------------------------
# MYSQL CONNECTION
# ---------------------------------------------------------
def db_connection():
    creds = db_credentials()
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=creds["username"],
        password=creds["password"],
        database=DB_NAME,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        ssl={"check_hostname": True},
    )
# ---------------------------------------------------------
# JWT HELPERS
# ---------------------------------------------------------
def b64url(data):
    return (
        base64.urlsafe_b64encode(data)
        .rstrip(b"=")
        .decode("ascii")
    )
def b64url_decode(value):
    return base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )
def create_access_token(user):
    now = int(time.time())
    # Exactly five claims, as specified in the project design.
    claims = {
        "sub": str(user["user_id"]),
        "role": user["role"],
        "iat": now,
        "exp": now + settings()["access_token_seconds"],
        "jti": str(uuid.uuid4()),
    }
    header = b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    payload = b64url(
        json.dumps(
            claims,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    message = f"{header}.{payload}"
    signature = b64url(
        hmac.new(
            jwt_secret(),
            message.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"{message}.{signature}", claims
def decode_access_token(token):
    try:
        if not isinstance(token, str):
            return None
        head, body, signature = token.split(".")
        header = json.loads(b64url_decode(head))
        claims = json.loads(b64url_decode(body))
        if (
            not isinstance(header, dict)
            or header.get("alg") != "HS256"
            or header.get("typ") != "JWT"
        ):
            return None
        if not isinstance(claims, dict):
            return None
        if set(claims) != {
            "sub", "role", "iat", "exp", "jti"
        }:
            return None
        expected = b64url(
            hmac.new(
                jwt_secret(),
                f"{head}.{body}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(expected, signature):
            return None
        if claims["role"] not in ("doctor", "patient"):
            return None
        if not isinstance(claims["sub"], str):
            return None
        if not claims["sub"]:
            return None
        if not isinstance(claims["jti"], str):
            return None
        if not claims["jti"]:
            return None
        if (
            type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
        ):
            return None
        now = int(time.time())
        if claims["iat"] > now or claims["exp"] <= now:
            return None
        return claims
    except Exception:
        return None
# ---------------------------------------------------------
# PASSWORD HASHING - BCRYPT
# ---------------------------------------------------------
def password_is_valid(password):
    return (
        isinstance(password, str)
        and len(password) >= settings()["password_min_length"]
        and re.search(r"[A-Za-z]", password) is not None
        and re.search(r"[0-9]", password) is not None
        and len(password.encode("utf-8")) <= 72
    )
def hash_password(password):
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(
            rounds=settings()["bcrypt_rounds"]
        ),
    ).decode("utf-8")
def verify_password(password, stored_hash):
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash.encode("utf-8"),
        )
    except (ValueError, TypeError, AttributeError):
        return False
# ---------------------------------------------------------
# REFRESH TOKEN HELPERS
# ---------------------------------------------------------
def token_hash(token):
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
def expiry_timestamp(value):
    """
    Convert an expiry value into an integer timestamp.
    Supports normal DynamoDB numeric values and legacy
    AttributeValue-shaped maps such as {"N": "1790000000"}.
    """
    if isinstance(value, dict):
        value = value.get(
            "N",
            value.get("S", 0),
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
def create_refresh_token(user_id, role):
    raw_token = secrets.token_urlsafe(48)
    now = int(time.time())
    expires = now + settings()["refresh_token_seconds"]
    REFRESH_TABLE.put_item(
        Item={
            "tokenHash": token_hash(raw_token),
            "userId": str(user_id),
            "createdAt": now,
            "expiresAt": expires,
        },
        ConditionExpression="attribute_not_exists(tokenHash)",
    )
    return raw_token, expires

def rotate_refresh_token(old_token, user, role):
    """Atomically consume the old refresh token and create its replacement."""
    now = int(time.time())
    old_hash = token_hash(old_token)
    user_id = str(user["user_id"])

    old_item = REFRESH_TABLE.get_item(
        Key={"tokenHash": old_hash},
        ConsistentRead=True,
    ).get("Item")
    if not old_item:
        raise ValueError("Refresh token does not exist")
    if str(old_item.get("userId")) != user_id:
        raise ValueError("Refresh token user mismatch")
    # The final refresh-tokens table stores tokenHash, userId, createdAt,
    # and expiresAt only. The authoritative role comes from the users row.
    if user.get("role") != role:
        raise ValueError("Refresh token role mismatch")
    if expiry_timestamp(old_item.get("expiresAt")) <= now:
        raise ValueError("Refresh token has expired")

    session_user = dict(user)
    session_user["role"] = role
    access_token, _ = create_access_token(session_user)
    new_token = secrets.token_urlsafe(48)
    new_hash = token_hash(new_token)
    expires = now + settings()["refresh_token_seconds"]

    DDB_CLIENT.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": REFRESH_TABLE.name,
                    "Item": {
                        "tokenHash": {"S": new_hash},
                        "userId": {"S": user_id},
                        "createdAt": {"N": str(now)},
                        "expiresAt": {"N": str(expires)},
                    },
                    "ConditionExpression": "attribute_not_exists(tokenHash)",
                }
            },
            {
                "Delete": {
                    "TableName": REFRESH_TABLE.name,
                    "Key": {"tokenHash": {"S": old_hash}},
                    "ConditionExpression": (
                        "userId = :user_id AND attribute_exists(expiresAt)"
                    ),
                    "ExpressionAttributeValues": {
                        ":user_id": {"S": user_id},
                    },
                }
            },
        ]
    )
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_in": settings()["access_token_seconds"],
        "refresh_token": new_token,
        "refresh_expires_in": settings()["refresh_token_seconds"],
    }

def register(data, role):
    """Create exactly one patient OR doctor account for an email address."""
    name = data.get("full_name")
    email = data.get("email")
    password = data.get("password")

    if role not in ("doctor", "patient"):
        return response(400, {"error": "Invalid registration role"})
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        return response(400, {"error": "Valid full_name is required"})
    if (
        not isinstance(email, str)
        or not email.strip()
        or len(email.strip()) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip())
    ):
        return response(400, {"error": "Valid email is required"})
    if not password_is_valid(password):
        return response(
            400,
            {
                "error": (
                    "Password must meet the configured minimum length "
                    "and contain at least one letter and one digit"
                )
            },
        )

    name = name.strip()
    email = email.strip().lower()
    try:
        with db_connection() as conn:
            conn.begin()
            try:
                with conn.cursor() as cur:
                    # UNIQUE(email) in MySQL is the final concurrency guard.
                    # This lookup provides a clear response for normal duplicates.
                    cur.execute(
                        "SELECT user_id FROM users WHERE email = %s LIMIT 1",
                        (email,),
                    )
                    if cur.fetchone():
                        conn.rollback()
                        return response(
                            409,
                            {"error": "This email is already registered. Each email can have only one account."},
                        )

                    user_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO users
                            (user_id, full_name, email, password_hash, role, is_active)
                        VALUES (%s, %s, %s, %s, %s, 1)
                        """,
                        (
                            user_id,
                            name,
                            email,
                            hash_password(password),
                            role,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return response(
            201,
            {
                "message": "Registration successful",
                "user": {
                    "user_id": user_id,
                    "full_name": name,
                    "email": email,
                    "role": role,
                    "roles": [role],
                },
            },
        )
    except pymysql.err.IntegrityError as exc:
        # Handles simultaneous registrations that race past the lookup.
        print(f"Registration uniqueness conflict: {exc}")
        return response(
            409,
            {"error": "This email is already registered. Each email can have only one account."},
        )
    except Exception as exc:
        print(f"Registration failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})

def login(data, role):
    email = data.get("email")
    password = data.get("password")

    if role not in ("doctor", "patient"):
        return response(400, {"error": "Invalid login role"})
    if not isinstance(email, str) or not email.strip():
        return response(400, {"error": "email is required"})
    if not isinstance(password, str):
        return response(400, {"error": "password is required"})

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, full_name, email, password_hash, role, is_active
                    FROM users
                    WHERE email = %s AND role = %s
                    LIMIT 1
                    """,
                    (email.strip().lower(), role),
                )
                user = cur.fetchone()

        if (
            not user
            or not user["is_active"]
            or user["role"] != role
            or not verify_password(password, user["password_hash"])
        ):
            return response(401, {"error": "Invalid email or password"})

        session_user = dict(user)
        session_user["role"] = role
        access_token, _ = create_access_token(session_user)
        refresh_token, _ = create_refresh_token(user["user_id"], role)

        return response(
            200,
            {
                "token_type": "Bearer",
                "access_token": access_token,
                "expires_in": settings()["access_token_seconds"],
                "refresh_token": refresh_token,
                "refresh_expires_in": settings()["refresh_token_seconds"],
                "user": {
                    "user_id": str(user["user_id"]),
                    "full_name": user["full_name"],
                    "email": user["email"],
                    "role": role,
                    "roles": [role],
                },
            },
        )
    except Exception as exc:
        print(f"Login failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})

def refresh(data):
    raw_token = data.get("refresh_token") or data.get("refreshToken")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return response(401, {"error": "Invalid or expired refresh token"})

    raw_token = raw_token.strip()
    try:
        item = REFRESH_TABLE.get_item(
            Key={"tokenHash": token_hash(raw_token)},
            ConsistentRead=True,
        ).get("Item")
        if not item or expiry_timestamp(item.get("expiresAt")) <= int(time.time()):
            return response(401, {"error": "Invalid or expired refresh token"})

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, full_name, email, role, is_active
                    FROM users
                    WHERE user_id = %s
                    LIMIT 1
                    """,
                    (item["userId"],),
                )
                user = cur.fetchone()

        if (
            not user
            or not user["is_active"]
            or str(user["user_id"]) != str(item["userId"])
        ):
            return response(401, {"error": "Invalid refresh token"})

        # The final users schema has exactly one ENUM role per account.
        # DynamoDB refresh-token records intentionally do not duplicate it.
        selected_role = user["role"]
        if selected_role not in ("doctor", "patient"):
            return response(401, {"error": "Refresh token role is no longer valid"})

        return response(
            200,
            rotate_refresh_token(raw_token, user, selected_role),
        )
    except ValueError:
        return response(
            401,
            {"error": "Refresh token has expired, already been used, or is invalid"},
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code")
        reasons = exc.response.get("CancellationReasons")
        print("Refresh DynamoDB error code:", code)
        print("Refresh DynamoDB error message:", error.get("Message"))
        if reasons:
            print("Refresh DynamoDB cancellation reasons:", reasons)

        if code == "ConditionalCheckFailedException":
            return response(
                401,
                {"error": "Refresh token is invalid or has already been used"},
            )
        if code == "TransactionCanceledException":
            cancellation_reasons = exc.response.get("CancellationReasons") or []
            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in cancellation_reasons
            ):
                return response(
                    401,
                    {"error": "Refresh token is invalid or has already been used"},
                )
            print("Refresh transaction cancellation reasons:", cancellation_reasons)
            traceback.print_exc()
            return response(500, {"error": "Unable to rotate refresh token"})

        print(f"Refresh failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})
    except Exception as exc:
        print(f"Refresh failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})

def logout(event, data):
    """
    Log out by consuming a refresh token.
    The refresh token may be sent in the JSON body ("refresh_token") or as
    the Bearer value in the Authorization header. If the Bearer value is a
    valid access token instead, it is optional: when present, its jti is
    also revoked and its user must match the refresh token's owner.
    """
    headers = {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }
    authorization = headers.get("authorization", "")
    bearer = None
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        parts = authorization.split(None, 1)
        bearer = parts[1].strip() if len(parts) == 2 else None
    # A valid access token (optional) lets us revoke its jti as well.
    claims = decode_access_token(bearer) if bearer else None
    raw_refresh_token = data.get("refresh_token") or data.get("refreshToken")
    # Refresh token sent in the Authorization header instead of the body.
    if not raw_refresh_token and bearer and not claims:
        raw_refresh_token = bearer
    if not isinstance(raw_refresh_token, str) or not raw_refresh_token.strip():
        return response(400, {"error": "refresh_token is required for logout"})
    try:
        old_hash = token_hash(raw_refresh_token.strip())
        refresh_item = REFRESH_TABLE.get_item(
            Key={"tokenHash": old_hash},
            ConsistentRead=True,
        ).get("Item")
        if (
            not refresh_item
            or expiry_timestamp(refresh_item.get("expiresAt")) <= int(time.time())
        ):
            return response(
                400,
                {"error": "Refresh token is invalid or expired"},
            )
        owner_id = str(refresh_item.get("userId"))
        if claims and claims["sub"] != owner_id:
            return response(
                400,
                {"error": "Refresh token belongs to another user"},
            )
        transact_items = [
            {
                "Delete": {
                    "TableName": REFRESH_TABLE.name,
                    "Key": {"tokenHash": {"S": old_hash}},
                    # Expiry was validated from the strongly consistent read
                    # above; the condition guards against concurrent reuse.
                    "ConditionExpression": (
                        "userId = :user_id AND attribute_exists(expiresAt)"
                    ),
                    "ExpressionAttributeValues": {
                        ":user_id": {"S": owner_id},
                    },
                }
            }
        ]
        if claims:
            transact_items.insert(
                0,
                {
                    "Put": {
                        "TableName": TOKEN_TABLE.name,
                        "Item": {
                            "jti": {"S": claims["jti"]},
                            "expiresAt": {"N": str(claims["exp"])},
                        },
                    }
                },
            )
        DDB_CLIENT.transact_write_items(TransactItems=transact_items)
        return response(200, {"message": "Logged out successfully"})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return response(400, {"error": "Refresh token is invalid, expired, or already used"})
        if code == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons") or []
            print("Logout transaction cancellation reasons:", reasons)
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                return response(
                    400,
                    {"error": "Refresh token is invalid, expired, or already used"},
                )
            traceback.print_exc()
            return response(500, {"error": "Unable to complete logout"})
        print("Logout DynamoDB error:", code, exc.response.get("Error", {}).get("Message"))
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})
    except Exception as exc:
        print(f"Logout failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return response(500, {"error": "Internal server error"})
def parse_event(event):
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}
    method = (
        http_context.get("method")
        or event.get("httpMethod")
        or ""
    ).upper()
    path = (
        event.get("rawPath")
        or event.get("path")
        or "/"
    ).split("?", 1)[0].rstrip("/").lower() or "/"
    body = event.get("body")
    if isinstance(body, str):
        try:
            if event.get("isBase64Encoded"):
                body = base64.b64decode(
                    body,
                    validate=True,
                ).decode("utf-8")
            body = json.loads(body)
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            body = None
    return (
        method,
        path,
        body if isinstance(body, dict) else None,
    )
# ---------------------------------------------------------
# LAMBDA ENTRY POINT
# ---------------------------------------------------------
def lambda_handler(event, context):
    method, path, data = parse_event(event)
    if method != "POST":
        return response(
            405,
            {"error": "POST required"},
        )
    if data is None:
        return response(
            400,
            {"error": "A valid JSON object is required"},
        )
    routes = {
        "/auth/doctors/register": lambda: register(
            data, "doctor"
        ),
        "/auth/doctors/login": lambda: login(
            data, "doctor"
        ),
        "/auth/patients/register": lambda: register(
            data, "patient"
        ),
        "/auth/patients/login": lambda: login(
            data, "patient"
        ),
        "/auth/refresh": lambda: refresh(data),
        "/auth/logout": lambda: logout(
            event, data
        ),
    }
    handler = routes.get(path)
    if handler is None:
        return response(
            404,
            {"error": "Route not found"},
        )
    return handler()
