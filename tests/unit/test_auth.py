"""Auth Lambda: registration, password policy, login, refresh rotation, logout."""

import time

import pytest
from conftest import JWT_SECRET, api_event, call

from medislot_common import db, tokens


def test_register_doctor_creates_user_and_doctor_rows(register):
    user = register("doctor", specialisation="Cardiology", hospital_name="City Care", hospital_city="Pune",
                    timezone="Asia/Kolkata", years_experience=7)
    row = db.fetch_one(
        """SELECT u.role, u.password_hash, d.licence_number, d.timezone, s.name AS specialisation, h.city
           FROM users u JOIN doctors d ON d.user_id = u.user_id
           JOIN specialisations s ON s.specialisation_id = d.specialisation_id
           JOIN hospitals h ON h.hospital_id = d.hospital_id
           WHERE u.user_id = %s""",
        (user["user_id"],),
    )
    assert row["role"] == "doctor"
    assert row["specialisation"] == "Cardiology"
    assert row["city"] == "Pune"
    # bcrypt with work factor 12, never the plain password
    assert row["password_hash"].startswith("$2b$12$")
    assert user["password"] not in row["password_hash"]


def test_register_patient_creates_patient_row(register):
    user = register("patient", city="Mumbai", gender="female")
    row = db.fetch_one("SELECT city, gender FROM patients WHERE user_id = %s", (user["user_id"],))
    assert row == {"city": "Mumbai", "gender": "female"}


@pytest.mark.parametrize("password", ["short1", "onlyletterslong", "1234567890123", "a" * 9 + "1"])
def test_password_policy(auth_fn, password):
    body = {"full_name": "X", "email": "p@example.com", "password": password}
    status, payload = call(auth_fn, api_event("POST", "/auth/patients/register", body=body))
    if len(password) >= 10 and any(c.isalpha() for c in password) and any(c.isdigit() for c in password):
        assert status == 201
    else:
        assert status == 400
        assert "Password" in payload["error"]


def test_email_is_unique_across_roles(register, auth_fn):
    register("patient", email="shared@example.com")
    body = {"full_name": "Dr Y", "email": "Shared@Example.com", "password": "CorrectHorse42", "licence_number": "L-9"}
    status, _ = call(auth_fn, api_event("POST", "/auth/doctors/register", body=body))
    assert status == 409


def test_unknown_specialisation_is_rejected(auth_fn):
    body = {"full_name": "Dr Z", "email": "z@example.com", "password": "CorrectHorse42", "licence_number": "L-1",
            "specialisation": "Astrology"}
    status, payload = call(auth_fn, api_event("POST", "/auth/doctors/register", body=body))
    assert status == 400
    assert "Cardiology" in payload["details"]["allowed"]


def test_login_returns_five_claim_token(register, auth_fn):
    user = register("patient")
    status, payload = call(auth_fn, api_event("POST", "/auth/patients/login",
                                               body={"email": user["email"], "password": user["password"]}))
    assert status == 200
    claims = tokens.verify(payload["access_token"], JWT_SECRET.encode(), "medislot-test")
    assert set(claims) == {"sub", "role", "iat", "exp", "jti"}
    assert claims["exp"] - claims["iat"] == 900
    assert claims["sub"] == user["user_id"] and claims["role"] == "patient"


def test_login_wrong_password_and_wrong_portal_get_the_same_401(register, auth_fn):
    user = register("patient")
    wrong_password = call(auth_fn, api_event("POST", "/auth/patients/login",
                                             body={"email": user["email"], "password": "Nope123456"}))
    wrong_portal = call(auth_fn, api_event("POST", "/auth/doctors/login",
                                           body={"email": user["email"], "password": user["password"]}))
    unknown = call(auth_fn, api_event("POST", "/auth/patients/login",
                                      body={"email": "nobody@example.com", "password": "Whatever123"}))
    assert wrong_password == wrong_portal == unknown == (401, {"error": "Invalid email or password"})


def _login(auth_fn, user, role):
    return call(auth_fn, api_event("POST", f"/auth/{role}s/login",
                                   body={"email": user["email"], "password": user["password"]}))[1]


def test_refresh_rotates_and_old_token_stops_working(register, auth_fn):
    user = register("doctor")
    session = _login(auth_fn, user, "doctor")
    status, rotated = call(auth_fn, api_event("POST", "/auth/refresh", body={"refresh_token": session["refresh_token"]}))
    assert status == 200
    assert rotated["refresh_token"] != session["refresh_token"]
    status, _ = call(auth_fn, api_event("POST", "/auth/refresh", body={"refresh_token": session["refresh_token"]}))
    assert status == 401
    status, _ = call(auth_fn, api_event("POST", "/auth/refresh", body={"refresh_token": rotated["refresh_token"]}))
    assert status == 200


def test_logout_revokes_refresh_token_and_denylists_jti(register, auth_fn, aws):
    user = register("patient")
    session = _login(auth_fn, user, "patient")
    claims = tokens.verify(session["access_token"], JWT_SECRET.encode(), "medislot-test")
    event = api_event("POST", "/auth/logout", body={"refresh_token": session["refresh_token"]},
                      user={"user_id": user["user_id"], "role": "patient"}, jti=claims["jti"])
    event["requestContext"]["authorizer"]["exp"] = str(claims["exp"])
    assert call(auth_fn, event)[0] == 200

    table = aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-token-revocations")
    item = table.get_item(Key={"jti": claims["jti"]})["Item"]
    assert item["userId"] == user["user_id"]
    assert int(item["expiresAt"]) == claims["exp"]  # TTL = remaining token lifetime
    assert call(auth_fn, api_event("POST", "/auth/refresh", body={"refresh_token": session["refresh_token"]}))[0] == 401


def test_logout_requires_authorizer_identity(auth_fn):
    status, _ = call(auth_fn, api_event("POST", "/auth/logout", body={}))
    assert status == 401


def test_deactivated_user_cannot_refresh(register, auth_fn):
    user = register("patient")
    session = _login(auth_fn, user, "patient")
    db.execute("UPDATE users SET is_active = 0 WHERE user_id = %s", (user["user_id"],))
    assert call(auth_fn, api_event("POST", "/auth/refresh", body={"refresh_token": session["refresh_token"]}))[0] == 401


def test_token_lifetime_and_issuer_come_from_ssm(register, auth_fn, aws):
    aws.client("ssm", region_name="us-east-1").put_parameter(
        Name="/medislot/test/auth/access-token-seconds", Value="60", Type="String", Overwrite=True)
    from medislot_common import config

    config.reset_caches()
    user = register("patient")
    session = _login(auth_fn, user, "patient")
    claims = tokens.verify(session["access_token"], JWT_SECRET.encode(), "medislot-test", now=time.time())
    assert claims["exp"] - claims["iat"] == 60
