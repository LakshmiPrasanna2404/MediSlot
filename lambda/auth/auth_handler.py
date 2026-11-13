"""MediSlot Auth Lambda (architecture box 4a).

Routes (REST API, Lambda proxy):
    POST /auth/doctors/register    public
    POST /auth/patients/register   public
    POST /auth/doctors/login       public
    POST /auth/patients/login      public
    POST /auth/refresh             public (needs a valid refresh token)
    POST /auth/logout              protected by the Lambda authorizer

* Passwords: bcrypt, work factor from the auth stack (12 or higher, enforced here too).
  Policy: at least 10 characters (auth stack parameter), one letter and one digit.
* Access token: HS256 JWT, 15 minutes (SSM), exactly sub/role/iat/exp/jti.
* Refresh token: opaque random string, 7 days (SSM), stored only as a
  SHA-256 hash in DynamoDB (refresh-tokens, TTL on expiresAt). Rotated on
  every refresh, so each refresh token works once.
* Logout: deletes the refresh token and writes the access token's jti to
  token-revocations with a TTL equal to the token's remaining lifetime.
* Registration creates the users row and the matching doctors/patients row in
  one transaction, so the profile exists as soon as the account does.
"""

import os
import re
import secrets
import time
import uuid

import bcrypt
import pymysql
from botocore.exceptions import ClientError

from medislot_common import config, db, logs, profiles, tokens
from medislot_common.http import APIError, api_handler, identity, parse_body
from medislot_common.validation import email as valid_email
from medislot_common.validation import reject_unknown, text

MIN_BCRYPT_ROUNDS = 12
GENERIC_LOGIN_ERROR = "Invalid email or password"
# Used to spend the same bcrypt time when the email does not exist, so the
# response time does not reveal which emails are registered.
# A cost-12 hash of random bytes nobody knows; checking against it takes as
# long as checking a real password.
_DUMMY_HASH = "$2b$12$zGA4WprPvgYv3hrOwpKZtuGJCoRJupIVdb7kUIk.sz46QJWXOEoDi"

COMMON_FIELDS = {"first_name", "last_name", "email", "password"}
DOCTOR_FIELDS = COMMON_FIELDS | {
    "licence_number", "specialisation", "hospital_name", "hospital_address", "hospital_city",
    "years_experience", "qualifications", "languages", "timezone",
}
PATIENT_FIELDS = COMMON_FIELDS | set(profiles.PATIENT_FIELDS)


def _auth_param(name, decrypt=False):
    return config.get_parameter(f"/medislot/{config.environment()}/auth/{name}", decrypt=decrypt)


def settings():
    values = {
        "access_seconds": int(_auth_param("access-token-seconds")),
        "refresh_seconds": int(_auth_param("refresh-token-seconds")),
        "bcrypt_rounds": config.config_int("auth/bcrypt-rounds"),
        "password_min_length": config.config_int("auth/password-min-length"),
        "issuer": _auth_param("jwt-issuer"),
    }
    if values["bcrypt_rounds"] < MIN_BCRYPT_ROUNDS:
        raise RuntimeError("bcrypt-rounds must be 12 or higher")
    if values["password_min_length"] < 10:
        raise RuntimeError("password-min-length must be 10 or higher")
    return values


def signing_secret():
    secret = config.get_parameter(os.environ["JWT_SECRET_PARAMETER"], decrypt=True).encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("JWT signing secret must be at least 32 bytes")
    return secret


def refresh_table():
    return config.table("REFRESH_TOKENS_TABLE")


def revocations_table():
    return config.table("TOKEN_REVOCATIONS_TABLE")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def check_password_policy(password):
    minimum = settings()["password_min_length"]
    if (
        not isinstance(password, str)
        or len(password) < minimum
        or not re.search(r"[A-Za-z]", password)
        or not re.search(r"[0-9]", password)
    ):
        raise APIError(400, f"Password must be at least {minimum} characters and contain a letter and a digit")
    if len(password.encode("utf-8")) > 72:
        raise APIError(400, "Password must be at most 72 bytes")


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=settings()["bcrypt_rounds"])).decode("utf-8")


def password_matches(password, stored_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def issue_session(user_id, role):
    cfg = settings()
    access_token, _claims = tokens.issue(user_id, role, signing_secret(), cfg["issuer"], cfg["access_seconds"])
    raw_refresh = secrets.token_urlsafe(48)
    now = int(time.time())
    refresh_table().put_item(
        Item={
            "tokenHash": tokens.refresh_token_hash(raw_refresh),
            "userId": str(user_id),
            "createdAt": now,
            "expiresAt": now + cfg["refresh_seconds"],
        },
        ConditionExpression="attribute_not_exists(tokenHash)",
    )
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_in": cfg["access_seconds"],
        "refresh_token": raw_refresh,
        "refresh_expires_in": cfg["refresh_seconds"],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register(event, role):
    body = parse_body(event)
    reject_unknown(body, DOCTOR_FIELDS if role == "doctor" else PATIENT_FIELDS)
    names = profiles.name_values(body, required=True)
    address = valid_email(body.get("email"))
    password = body.get("password")
    check_password_policy(password)
    licence = text(body.get("licence_number"), "licence_number", 50, required=True) if role == "doctor" else None

    if db.fetch_one("SELECT user_id FROM users WHERE email = %s", (address,)):
        raise APIError(409, "This email is already registered. Each email can have only one account.")

    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    profile_id = str(uuid.uuid4())
    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO users (user_id, first_name, last_name, email, password_hash, role)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, names["first_name"], names["last_name"], address, password_hash, role),
            )
            if role == "doctor":
                values = {"licence_number": licence, **profiles.doctor_values(cur, body, creating=True)}
                table, key = "doctors", "doctor_id"
            else:
                values = profiles.patient_values(body)
                table, key = "patients", "patient_id"
            columns = [key, "user_id", *values]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                [profile_id, user_id, *values.values()],
            )
    except pymysql.err.IntegrityError as exc:
        message = str(exc)
        if "uq_doctors_licence_number" in message:
            raise APIError(409, "This licence number is already registered") from None
        if "uq_users_email" in message:
            raise APIError(409, "This email is already registered. Each email can have only one account.") from None
        raise
    logs.metric("Registrations", Role=role)
    logs.info("account registered", userId=user_id, role=role)
    return 201, {
        "message": "Registration successful",
        "user": {"user_id": user_id, **names, "email": address, "role": role},
        f"{role}_id": profile_id,
    }


def login(event, role):
    body = parse_body(event)
    address = body.get("email")
    password = body.get("password")
    if not isinstance(address, str) or not address.strip() or not isinstance(password, str):
        raise APIError(400, "email and password are required")
    user = db.fetch_one(
        "SELECT user_id, first_name, last_name, email, password_hash, role, is_active"
        " FROM users WHERE email = %s AND role = %s",
        (address.strip().lower(), role),
    )
    valid = password_matches(password, user["password_hash"] if user else _DUMMY_HASH)
    if not user or not valid or not user["is_active"]:
        # The caller always gets the same message; the log keeps the real reason.
        reason = "unknown_email" if not user else "wrong_password" if not valid else "account_deactivated"
        logs.metric("LoginFailures", Role=role)
        logs.warning("login rejected", loginRole=role, reason=reason, userId=user["user_id"] if user else None)
        raise APIError(401, GENERIC_LOGIN_ERROR)
    session = issue_session(user["user_id"], role)
    logs.metric("LoginSuccesses", Role=role)
    logs.info("login succeeded", userId=user["user_id"], loginRole=role)
    session["user"] = {
        "user_id": user["user_id"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "email": user["email"],
        "role": role,
    }
    return 200, session


def refresh(event):
    body = parse_body(event)
    raw = body.get("refresh_token")
    if not isinstance(raw, str) or not raw.strip():
        logs.warning("refresh rejected", reason="missing_token")
        raise APIError(401, "Invalid or expired refresh token")
    old_hash = tokens.refresh_token_hash(raw.strip())
    now = int(time.time())
    item = refresh_table().get_item(Key={"tokenHash": old_hash}, ConsistentRead=True).get("Item")
    if not item or int(item.get("expiresAt", 0)) <= now:
        logs.warning("refresh rejected", reason="unknown_or_expired_token")
        raise APIError(401, "Invalid or expired refresh token")
    user = db.fetch_one("SELECT user_id, role, is_active FROM users WHERE user_id = %s", (item["userId"],))
    if not user or not user["is_active"]:
        logs.warning("refresh rejected", reason="account_deactivated", userId=item["userId"])
        raise APIError(401, "Invalid or expired refresh token")

    cfg = settings()
    access_token, _claims = tokens.issue(user["user_id"], user["role"], signing_secret(), cfg["issuer"], cfg["access_seconds"])
    new_raw = secrets.token_urlsafe(48)
    table = refresh_table()
    try:
        # Rotation: the old token is deleted and the new one created in one
        # transaction. The delete is conditional, so a token works once.
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": table.name,
                        "Key": {"tokenHash": old_hash},
                        "ConditionExpression": "userId = :uid AND expiresAt > :now",
                        "ExpressionAttributeValues": {":uid": user["user_id"], ":now": now},
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            "tokenHash": tokens.refresh_token_hash(new_raw),
                            "userId": user["user_id"],
                            "createdAt": now,
                            "expiresAt": now + cfg["refresh_seconds"],
                        },
                        "ConditionExpression": "attribute_not_exists(tokenHash)",
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            logs.warning("refresh rejected", reason="token_already_used", userId=user["user_id"])
            raise APIError(401, "Refresh token is invalid or has already been used") from None
        raise
    logs.info("tokens refreshed", userId=user["user_id"], userRole=user["role"])
    return 200, {
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_in": cfg["access_seconds"],
        "refresh_token": new_raw,
        "refresh_expires_in": cfg["refresh_seconds"],
    }


def logout(event):
    caller = identity(event)  # set by the Lambda authorizer
    body = parse_body(event)
    raw = body.get("refresh_token")
    refresh_revoked = False
    if raw is not None:
        if not isinstance(raw, str) or not raw.strip():
            raise APIError(400, "refresh_token must be a string")
        token_hash = tokens.refresh_token_hash(raw.strip())
        item = refresh_table().get_item(Key={"tokenHash": token_hash}, ConsistentRead=True).get("Item")
        if item and item.get("userId") != caller.user_id:
            raise APIError(400, "Refresh token belongs to another user")
        refresh_revoked = bool(item)
        if item:
            try:
                refresh_table().delete_item(
                    Key={"tokenHash": token_hash},
                    ConditionExpression="userId = :uid",
                    ExpressionAttributeValues={":uid": caller.user_id},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
    if not caller.jti or caller.exp <= 0:
        raise APIError(401, "Missing token details")
    # The denylist entry lives exactly as long as the access token would have.
    revocations_table().put_item(
        Item={
            "jti": caller.jti,
            "userId": caller.user_id,
            "createdAt": int(time.time()),
            "expiresAt": caller.exp,
        }
    )
    logs.info("logged out", userId=caller.user_id, accessTokenRevoked=caller.jti, refreshTokenRevoked=refresh_revoked)
    return 200, {"message": "Logged out"}


lambda_handler = api_handler(
    {
        "POST /auth/doctors/register": lambda event: register(event, "doctor"),
        "POST /auth/patients/register": lambda event: register(event, "patient"),
        "POST /auth/doctors/login": lambda event: login(event, "doctor"),
        "POST /auth/patients/login": lambda event: login(event, "patient"),
        "POST /auth/refresh": refresh,
        "POST /auth/logout": logout,
    }
)
