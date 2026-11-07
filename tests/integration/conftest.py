"""Integration tests against a deployed environment.

The API address comes from MEDISLOT_API_URL, or from the medislot-<env>-api
stack outputs (MEDISLOT_ENV, default dev) using the caller's AWS credentials.
Every test account uses a unique email, so runs never collide.
"""

import os
import secrets
import uuid

import pytest
import requests

ENV = os.environ.get("MEDISLOT_ENV", "dev")
RUN = uuid.uuid4().hex[:8]
# A fresh password per run: test accounts live in the shared dev database.
PASSWORD = secrets.token_urlsafe(18) + "a7"  # letter and digit, as the policy requires


def _api_url():
    if os.environ.get("MEDISLOT_API_URL"):
        return os.environ["MEDISLOT_API_URL"].rstrip("/")
    import boto3

    outputs = boto3.client("cloudformation").describe_stacks(StackName=f"medislot-{ENV}-api")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "ApiUrl").rstrip("/")


@pytest.fixture(scope="session")
def api():
    try:
        base = _api_url()
    except Exception as exc:  # noqa: BLE001 - no deployment to test against
        pytest.skip(f"no deployed API: {exc}")

    class Api:
        url = base

        def call(self, method, path, token=None, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["X-Correlation-Id"] = f"itest-{RUN}-{uuid.uuid4().hex[:6]}"
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return requests.request(method, base + path, headers=headers, timeout=30, **kwargs)

    return Api()


def _register_and_login(api, role, **extra):
    email = f"itest-{RUN}-{role}-{uuid.uuid4().hex[:6]}@example.com"
    body = {"full_name": extra.pop("full_name", f"Itest {role.title()} {RUN}"), "email": email, "password": PASSWORD}
    if role == "doctor":
        body["licence_number"] = f"IT-{RUN}-{uuid.uuid4().hex[:6]}"
    body.update(extra)
    response = api.call("POST", f"/auth/{role}s/register", json=body)
    assert response.status_code == 201, response.text
    login = api.call("POST", f"/auth/{role}s/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    session = login.json()
    session["email"] = email
    session["profile_id"] = response.json()[f"{role}_id"]
    return session


@pytest.fixture(scope="session")
def doctor(api):
    return _register_and_login(
        api, "doctor", full_name=f"Itestdoc {RUN} Rao", specialisation="Cardiology",
        hospital_name=f"Itest Hospital {RUN}", hospital_city="Itestcity", timezone="Asia/Kolkata",
    )


@pytest.fixture(scope="session")
def patient(api):
    session = _register_and_login(api, "patient", city="Pune")
    yield session
    api.call("DELETE", "/patients/me", token=session["access_token"])


@pytest.fixture()
def new_patient(api):
    return lambda **extra: _register_and_login(api, "patient", **extra)
