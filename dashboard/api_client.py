"""Calls to the MediSlot HTTP API on behalf of the signed-in user.

The access and refresh tokens live in the server-side session. When the API
answers 401 (the 15-minute access token expired, or it is no longer on the
active-tokens list), the client rotates the refresh token once and retries;
if that fails the user is signed out.

Error bodies: the Lambdas answer {"error": ...}; API Gateway itself answers
{"message": ...} (for example 403 from the authorizer, 404 for an unknown
route, 429 when throttled). Both are shown to the user.
"""

import base64
import json
import time

import requests
from flask import g, session

from . import settings, weblog

TIMEOUT_SECONDS = 20


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


class SessionExpired(Exception):
    """The refresh token no longer works; the user must log in again."""


def jwt_role(token):
    """Role claim from the session's JWT, for UI routing only (the API verifies the token)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("role")
    except (AttributeError, IndexError, ValueError):
        return None


def _send(method, path, *, json_body=None, params=None, token=None):
    headers = {"X-Correlation-Id": g.get("correlation_id", "web"), "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        response = requests.request(
            method, settings.api_base_url() + path, json=json_body, params=params, headers=headers, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        weblog.error("api call failed", apiMethod=method, apiPath=path, error=type(exc).__name__,
                     durationMs=round((time.perf_counter() - started) * 1000, 1))
        raise
    fields = {"apiMethod": method, "apiPath": path, "apiStatus": response.status_code,
              "durationMs": round((time.perf_counter() - started) * 1000, 1),
              # HTTP APIs return the request id as apigw-requestid (REST APIs used x-amzn-RequestId).
              "apiRequestId": response.headers.get("apigw-requestid") or response.headers.get("x-amzn-RequestId")}
    if response.status_code >= 400:
        body = _payload(response)
        fields["apiError"] = body.get("error") or body.get("message")
        weblog.warning("api call", **fields)
    else:
        weblog.info("api call", **fields)
    return response


def _payload(response):
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {"error": response.text[:200] or f"HTTP {response.status_code}"}


GATEWAY_MESSAGES = {
    403: "This account type cannot use this page",
    404: "That page or record was not found",
    429: "Too many requests, please try again in a moment",
}


def _error_text(status, body):
    if body.get("error"):
        return body["error"]
    if body.get("message"):
        return GATEWAY_MESSAGES.get(status, body["message"])
    return f"HTTP {status}"


def _raise_for(response):
    if response.status_code >= 400:
        body = _payload(response)
        raise ApiError(response.status_code, _error_text(response.status_code, body), body.get("details"))


def public_call(method, path, json_body=None):
    """Register, login and refresh: no Authorization header."""
    response = _send(method, path, json_body=json_body)
    _raise_for(response)
    return _payload(response)


def store_tokens(tokens):
    session["access_token"] = tokens["access_token"]
    session["refresh_token"] = tokens["refresh_token"]
    session["role"] = jwt_role(tokens["access_token"])


def refresh_tokens():
    refresh = session.get("refresh_token")
    if not refresh:
        return False
    response = _send("POST", "/auth/refresh", json_body={"refresh_token": refresh})
    if response.status_code != 200:
        weblog.info("session refresh failed", apiStatus=response.status_code)
        return False
    store_tokens(_payload(response))
    weblog.info("session tokens refreshed")
    return True


def call(method, path, json_body=None, params=None):
    """An authenticated call. Rotates the tokens once on 401."""
    token = session.get("access_token")
    if not token:
        raise SessionExpired()
    response = _send(method, path, json_body=json_body, params=params, token=token)
    if response.status_code == 401:
        if not refresh_tokens():
            weblog.info("session expired, signing out")
            session.clear()
            raise SessionExpired()
        response = _send(method, path, json_body=json_body, params=params, token=session["access_token"])
        if response.status_code == 401:
            weblog.info("session expired, signing out")
            session.clear()
            raise SessionExpired()
    _raise_for(response)
    return _payload(response)


def logout():
    """Revoke the refresh token and the access token, then forget them."""
    token = session.get("access_token")
    refresh = session.get("refresh_token")
    try:
        if token:
            _send("POST", "/auth/logout", json_body={"refresh_token": refresh} if refresh else {}, token=token)
    except requests.RequestException:
        pass  # the session is cleared below either way
    session.clear()
