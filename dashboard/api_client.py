"""Calls to the MediSlot REST API on behalf of the signed-in user.

The access and refresh tokens live in the server-side session. When the API
answers 401 (the 15-minute access token expired), the client rotates the
refresh token once and retries; if that fails the user is signed out.
"""

import base64
import json

import requests
from flask import g, session

from . import settings

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
    return requests.request(
        method, settings.api_base_url() + path, json=json_body, params=params, headers=headers, timeout=TIMEOUT_SECONDS
    )


def _payload(response):
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {"error": response.text[:200] or f"HTTP {response.status_code}"}


def _raise_for(response):
    if response.status_code >= 400:
        body = _payload(response)
        raise ApiError(response.status_code, body.get("error") or f"HTTP {response.status_code}", body.get("details"))


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
        return False
    store_tokens(_payload(response))
    return True


def call(method, path, json_body=None, params=None):
    """An authenticated call. Rotates the tokens once on 401."""
    token = session.get("access_token")
    if not token:
        raise SessionExpired()
    response = _send(method, path, json_body=json_body, params=params, token=token)
    if response.status_code == 401:
        if not refresh_tokens():
            session.clear()
            raise SessionExpired()
        response = _send(method, path, json_body=json_body, params=params, token=session["access_token"])
        if response.status_code == 401:
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
