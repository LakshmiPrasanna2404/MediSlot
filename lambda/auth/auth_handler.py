"""MediSlot authentication Lambda.

Routes:
    POST /register
    POST /login
    POST /refresh
    POST /logout

Database credentials are read from SSM Parameter Store SecureString parameters.
Refresh tokens are stored as SHA-256 hashes and rotated as single-use sessions in DynamoDB.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import ClientError


REGION = os.environ.get("AWS_REGION", "us-east-1")

# Short network timeouts and one attempt prevent an unreachable VPC endpoint
# from consuming the whole Lambda execution window.
AWS_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=4,
    retries={"total_max_attempts": 1},
)

SSM = boto3.client("ssm", region_name=REGION, config=AWS_CLIENT_CONFIG)
DDB = boto3.resource("dynamodb", region_name=REGION, config=AWS_CLIENT_CONFIG)

TOKEN_TABLE = DDB.Table(os.environ["TOKEN_REVOCATIONS_TABLE"])
REFRESH_TABLE = DDB.Table(os.environ["REFRESH_TOKENS_TABLE"])

JWT_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
DB_USERNAME_PARAMETER = os.environ["DB_USERNAME_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ["DB_NAME"]

_secret_cache = None
_db_cache = None


def response(code, data):
    """Build a JSON API response."""
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(data),
    }


def jwt_secret():
    """Read and cache the JWT signing secret."""
    global _secret_cache

    if _secret_cache is None:
        print("DIAG: JWT SSM retrieval starting")
        try:
            result = SSM.get_parameter(
                Name=JWT_PARAMETER,
                WithDecryption=True,
            )
            print("DIAG: JWT SSM retrieval succeeded")
        except Exception as exc:
            print(f"DIAG: JWT SSM retrieval failed ({type(exc).__name__})")
            raise
        _secret_cache = result["Parameter"]["Value"].encode("utf-8")

        if len(_secret_cache) < 32:
            raise RuntimeError("JWT signing secret must be at least 32 bytes")

    return _secret_cache


def db_credentials():
    """Read and cache database credentials from SSM Parameter Store."""
    global _db_cache

    if _db_cache is None:
        print("DIAG: Database credential SSM retrieval starting")
        try:
            username_result = SSM.get_parameter(
                Name=DB_USERNAME_PARAMETER,
                WithDecryption=True,
            )
            password_result = SSM.get_parameter(
                Name=DB_PASSWORD_PARAMETER,
                WithDecryption=True,
            )
            print("DIAG: Database credential SSM retrieval succeeded")
        except Exception as exc:
            print(f"DIAG: Database credential SSM retrieval failed ({type(exc).__name__})")
            raise

        username = username_result["Parameter"].get("Value")
        password = password_result["Parameter"].get("Value")

        if not isinstance(username, str) or not username:
            raise RuntimeError("Database username parameter is empty")
        if not isinstance(password, str) or not password:
            raise RuntimeError("Database password parameter is empty")

        _db_cache = {
            "username": username,
            "password": password,
        }

    return _db_cache


def connection():
    """Open a connection to the existing private RDS database."""
    credentials = db_credentials()
    print("DIAG: MySQL connection starting")

    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=credentials["username"],
            password=credentials["password"],
            database=DB_NAME,
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            ssl={"check_hostname": True},
        )
        print("DIAG: MySQL connection succeeded")
        return conn
    except Exception as exc:
        print(f"DIAG: MySQL connection failed ({type(exc).__name__})")
        raise


def b64(data):
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value):
    """Decode a base64url value."""
    return base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )


def create_token(user, typ="access", lifetime=900):
    """Create an HS256 JWT."""
    now = int(time.time())

    header = b64(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode("utf-8")
    )

    claims = {
        "sub": str(user["user_id"]),
        "email": user["email"],
        "role": user["role"],
        "typ": typ,
        "iat": now,
        "exp": now + lifetime,
        "jti": str(uuid.uuid4()),
    }

    payload = b64(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    )

    message = f"{header}.{payload}"

    signature = b64(
        hmac.new(
            jwt_secret(),
            message.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )

    return f"{message}.{signature}", claims


def decode_token(token, expected_type):
    """Validate a token's signature, type, and expiry."""
    try:
        if not isinstance(token, str):
            return None

        header_part, payload_part, signature = token.split(".")

        header = json.loads(unb64(header_part))
        claims = json.loads(unb64(payload_part))

        if not isinstance(header, dict) or not isinstance(claims, dict):
            return None

        if header.get("alg") != "HS256":
            return None

        expected_signature = b64(
            hmac.new(
                jwt_secret(),
                f"{header_part}.{payload_part}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )

        if not hmac.compare_digest(expected_signature, signature):
            return None

        if claims.get("typ") != expected_type:
            return None

        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None

        if exp <= int(time.time()):
            return None

        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            return None

        if not isinstance(claims.get("jti"), str) or not claims["jti"]:
            return None

        if not isinstance(claims.get("email"), str):
            return None

        if claims.get("role") not in ("patient", "doctor", "admin"):
            return None

        return claims

    except Exception:
        return None


def hash_password(password):
    """Hash a password using PBKDF2-HMAC-SHA256."""
    salt = os.urandom(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        310000,
    )

    return f"pbkdf2_sha256$310000${b64(salt)}${b64(digest)}"


def verify_password(password, encoded):
    """Verify a PBKDF2 password hash."""
    try:
        algorithm, rounds, salt, digest = encoded.split("$")

        if algorithm != "pbkdf2_sha256":
            return False

        iterations = int(rounds)
        if not 100000 <= iterations <= 1000000:
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            unb64(salt),
            iterations,
        )

        return hmac.compare_digest(actual, unb64(digest))

    except Exception:
        return False


def refresh_token_hash(token):
    """Return a non-reversible SHA-256 digest for a refresh token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def store_refresh_token(token, claims):
    """Store refresh-token metadata; never store the raw token."""
    now = int(time.time())
    REFRESH_TABLE.put_item(
        Item={
            "tokenHash": refresh_token_hash(token),
            "userId": str(claims["sub"]),
            "jti": claims["jti"],
            "createdAt": now,
            "expiresAt": int(claims["exp"]),
        },
        ConditionExpression="attribute_not_exists(tokenHash)",
    )


def consume_refresh_token(token, claims):
    """Atomically delete a valid stored refresh token once."""
    token_hash = refresh_token_hash(token)
    now = int(time.time())
    item = REFRESH_TABLE.get_item(
        Key={"tokenHash": token_hash},
        ConsistentRead=True,
    ).get("Item")

    if not item:
        return False
    if item.get("userId") != str(claims["sub"]):
        return False
    if item.get("jti") != claims["jti"]:
        return False
    if int(item.get("expiresAt", 0)) <= now:
        return False

    try:
        REFRESH_TABLE.delete_item(
            Key={"tokenHash": token_hash},
            ConditionExpression=(
                "jti = :jti AND userId = :uid AND expiresAt > :now"
            ),
            ExpressionAttributeValues={
                ":jti": claims["jti"],
                ":uid": str(claims["sub"]),
                ":now": now,
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def revoke_refresh_token(token):
    """Remove a refresh-token session if the supplied token is valid."""
    claims = decode_token(token, "refresh") if isinstance(token, str) else None
    if not claims:
        return False
    return consume_refresh_token(token, claims)


def issue_tokens(user):
    """Issue an access token and persist a hash of the refresh token."""
    access_token, _ = create_token(
        user,
        typ="access",
        lifetime=900,
    )

    refresh_token, refresh_claims = create_token(
        user,
        typ="refresh",
        lifetime=604800,
    )
    store_refresh_token(refresh_token, refresh_claims)

    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_in": 900,
        "refresh_token": refresh_token,
        "refresh_expires_in": 604800,
    }


def parse(event):
    """Extract HTTP method, path, and JSON request body."""
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}

    method = (
        http_context.get("method")
        or event.get("httpMethod")
        or event.get("method")
        or ""
    ).upper()

    path = (
        event.get("rawPath")
        or event.get("path")
        or "/"
    ).split("?", 1)[0].rstrip("/") or "/"

    body = event.get("body") or {}

    if isinstance(body, str):
        try:
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body, validate=True).decode("utf-8")

            body = json.loads(body)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            body = None

    if not isinstance(body, dict):
        body = None

    return method, path, body


def handler_register(data):
    """Register a new patient account."""
    name = str(data.get("full_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")

    if (
        not name
        or len(name) > 120
        or "@" not in email
        or len(email) > 254
        or not isinstance(password, str)
        or not 12 <= len(password) <= 256
    ):
        return response(
            400,
            {
                "error": (
                    "full_name, valid email, and password "
                    "(12â€“256 characters) are required"
                )
            },
        )

    user_id = str(uuid.uuid4())
    password_hash = hash_password(password)

    try:
        print("DIAG: Registration database operation starting")
        with connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users
                        (user_id, full_name, email, password_hash, role, is_active)
                    VALUES (%s, %s, %s, %s, 'patient', 1)
                    """,
                    (user_id, name, email, password_hash),
                )

        print("DIAG: Registration database operation succeeded")
        return response(
            201,
            {
                "message": "Registration successful",
                "user": {
                    "user_id": user_id,
                    "full_name": name,
                    "email": email,
                    "role": "patient",
                },
            },
        )

    except pymysql.err.IntegrityError:
        return response(
            409,
            {"error": "An account with this email already exists"},
        )

    except Exception as exc:
        print(f"DIAG: Registration failed ({type(exc).__name__}): {exc}")
        return response(500, {"error": "Internal server error"})


def handler_login(data):
    """Authenticate a user with email and password."""
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")

    if not email or not isinstance(password, str):
        return response(
            400,
            {"error": "email and password are required"},
        )

    try:
        with connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, full_name, email, password_hash, role, is_active
                    FROM users
                    WHERE email = %s
                    LIMIT 1
                    """,
                    (email,),
                )
                user = cursor.fetchone()

        if (
            not user
            or not user["is_active"]
            or not verify_password(password, user["password_hash"])
        ):
            return response(
                401,
                {"error": "Invalid email or password"},
            )

        result = issue_tokens(user)
        result["user"] = {
            "user_id": str(user["user_id"]),
            "full_name": user["full_name"],
            "email": user["email"],
            "role": user["role"],
        }

        return response(200, result)

    except Exception:
        print("Login failed; details omitted")
        return response(500, {"error": "Internal server error"})


def revoke_token_once(jti, expires_at):
    """Atomically record a token as revoked/consumed exactly once."""
    try:
        TOKEN_TABLE.put_item(
            Item={
                "jti": jti,
                "expiresAt": int(expires_at),
            },
            ConditionExpression="attribute_not_exists(jti)",
        )
        return True

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")

        if error_code == "ConditionalCheckFailedException":
            return False

        raise


def handler_refresh(data):
    """Consume a refresh token once and issue a replacement pair."""
    token = data.get("refresh_token")
    claims = decode_token(token, "refresh") if isinstance(token, str) else None

    if not claims:
        return response(
            401,
            {"error": "Invalid or expired refresh token"},
        )

    try:
        with connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, full_name, email, role, is_active
                    FROM users
                    WHERE user_id = %s
                    LIMIT 1
                    """,
                    (claims["sub"],),
                )
                user = cursor.fetchone()

        if not user or not user["is_active"]:
            return response(
                401,
                {"error": "Invalid refresh token"},
            )

        # The raw token must match a stored hash and can be consumed once.
        consumed = consume_refresh_token(token, claims)
        if not consumed:
            return response(
                401,
                {"error": "Refresh token is not active or was already used"},
            )

        return response(200, issue_tokens(user))

    except Exception as exc:
        print(f"DIAG: Refresh failed ({type(exc).__name__})")
        return response(500, {"error": "Internal server error"})


def handler_logout(event, data=None):
    """Revoke the access token and, if supplied, the refresh session."""
    headers = {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }

    auth = headers.get("authorization", "")

    if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
        return response(
            401,
            {"error": "Valid bearer access token required"},
        )

    parts = auth.split(None, 1)
    if len(parts) != 2:
        return response(
            401,
            {"error": "Valid bearer access token required"},
        )

    claims = decode_token(parts[1], "access")

    if not claims:
        return response(
            401,
            {"error": "Valid bearer access token required"},
        )

    try:
        # The client may include refresh_token in the logout JSON body.
        refresh_token = (data or {}).get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            revoke_refresh_token(refresh_token)

        revoked = revoke_token_once(
            claims["jti"],
            claims["exp"],
        )

        if not revoked:
            return response(
                200,
                {"message": "Logged out successfully"},
            )

        return response(
            200,
            {"message": "Logged out successfully"},
        )

    except Exception:
        print("Logout failed; details omitted")
        return response(500, {"error": "Internal server error"})


def lambda_handler(event, context):
    """Lambda entry point."""
    print("DIAG: Auth Lambda invocation started")
    method, path, data = parse(event)
    print(f"DIAG: Request parsed method={method or 'missing'} path={path}")

    if method != "POST":
        return response(405, {"error": "POST required"})

    if data is None:
        return response(400, {"error": "A valid JSON object is required"})

    if path.endswith("/register"):
        return handler_register(data)

    if path.endswith("/login"):
        return handler_login(data)

    if path.endswith("/refresh"):
        return handler_refresh(data)

    if path.endswith("/logout"):
        return handler_logout(event, data)

    return response(404, {"error": "Route not found"})