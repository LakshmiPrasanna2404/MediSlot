"""Milestone 2 checks against the deployed API: tokens, roles, logout, password policy."""

import base64
import json
import os
import time

import pytest

pytestmark = pytest.mark.integration


def test_every_protected_route_needs_a_token(api):
    for method, path in [("GET", "/doctors/me"), ("GET", "/patients/me"), ("GET", "/doctors"), ("POST", "/auth/logout")]:
        assert api.call(method, path).status_code == 401, path


def test_wrong_role_gets_403(api, patient, doctor):
    assert api.call("GET", "/doctors/me", token=patient["access_token"]).status_code == 403
    assert api.call("PUT", "/doctors/me/availability", token=patient["access_token"], json={"patterns": []}).status_code == 403
    assert api.call("GET", "/patients/me", token=doctor["access_token"]).status_code == 403


def test_tampered_token_gets_401(api, patient):
    head, body, signature = patient["access_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["role"] = "doctor"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert api.call("GET", "/doctors/me", token=f"{head}.{forged}.{signature}").status_code == 401


def test_wrongly_signed_token_gets_401(api, patient):
    head, body, _signature = patient["access_token"].split(".")
    assert api.call("GET", "/patients/me", token=f"{head}.{body}.AAAA").status_code == 401


@pytest.mark.skipif(os.environ.get("MEDISLOT_READ_JWT_SECRET") != "true", reason="needs the deploy role (reads the signing key)")
def test_expired_token_gets_401(api, patient):
    import sys
    from pathlib import Path

    import boto3

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda" / "shared"))
    from medislot_common import tokens

    env = os.environ.get("MEDISLOT_ENV", "dev")
    ssm = boto3.client("ssm")
    secret = ssm.get_parameter(Name=f"/medislot/{env}/auth/jwt-secret", WithDecryption=True)["Parameter"]["Value"]
    issuer = ssm.get_parameter(Name=f"/medislot/{env}/auth/jwt-issuer")["Parameter"]["Value"]
    claims = json.loads(base64.urlsafe_b64decode(patient["access_token"].split(".")[1] + "=="))
    expired, _ = tokens.issue(claims["sub"], "patient", secret.encode(), issuer, 60, now=time.time() - 3600)
    assert api.call("GET", "/patients/me", token=expired).status_code == 401


def test_refresh_rotates_and_old_refresh_token_is_dead(api, new_patient):
    session = new_patient()
    first = api.call("POST", "/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert first.status_code == 200
    assert api.call("POST", "/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
    assert api.call("GET", "/patients/me", token=first.json()["access_token"]).status_code == 200


def test_token_used_after_logout_gets_401(api, new_patient):
    session = new_patient()
    assert api.call("GET", "/patients/me", token=session["access_token"]).status_code == 200
    out = api.call("POST", "/auth/logout", token=session["access_token"], json={"refresh_token": session["refresh_token"]})
    assert out.status_code == 200
    assert api.call("GET", "/patients/me", token=session["access_token"]).status_code == 401
    assert api.call("POST", "/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401


def test_password_policy_and_duplicate_email(api, patient):
    weak = api.call("POST", "/auth/patients/register", json={"full_name": "X", "email": "weak@example.com", "password": "short1"})
    assert weak.status_code == 400
    duplicate = api.call("POST", "/auth/doctors/register", json={
        "full_name": "Dup", "email": patient["email"], "password": "Another-Pass-77", "licence_number": "DUP-1"})
    assert duplicate.status_code == 409


def test_request_body_validation(api):
    response = api.call("POST", "/auth/patients/login", json={"email": "a@b.c"})
    assert response.status_code == 400
