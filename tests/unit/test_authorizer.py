"""Lambda authorizer: 401 for bad tokens, 403 (Deny) for the wrong role, context for good ones."""

import base64
import json
import time

import pytest
from conftest import JWT_SECRET, FakeContext, load_handler

from medislot_common import tokens

ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/{method}{path}"


@pytest.fixture()
def authorizer(aws):
    return load_handler("authorizer", "authorizer_handler.py")


def make_token(role="patient", secret=JWT_SECRET, issuer="medislot-test", lifetime=900, now=None):
    return tokens.issue("user-1", role, secret.encode(), issuer, lifetime, now=now)


def invoke(authorizer, token, method="GET", path="/patients/me"):
    event = {"type": "TOKEN", "authorizationToken": f"Bearer {token}", "methodArn": ARN.format(method=method, path=path)}
    return authorizer.lambda_handler(event, FakeContext())


def test_valid_token_is_allowed_with_context(authorizer):
    token, claims = make_token("patient")
    result = invoke(authorizer, token)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"] == {"userId": "user-1", "role": "patient", "jti": claims["jti"], "exp": str(claims["exp"])}


@pytest.mark.parametrize(
    "role,method,path,effect",
    [
        ("doctor", "GET", "/doctors/me", "Allow"),
        ("patient", "GET", "/doctors/me", "Deny"),
        ("patient", "PUT", "/doctors/me/availability", "Deny"),
        ("doctor", "GET", "/patients/me", "Deny"),
        ("patient", "GET", "/doctors", "Allow"),
        ("patient", "GET", "/doctors/6f1c1e9e-1111-4c7a-9c4e-2d3a7b9c0d11/slots", "Allow"),
        ("doctor", "GET", "/doctors/6f1c1e9e-1111-4c7a-9c4e-2d3a7b9c0d11", "Deny"),
        ("doctor", "POST", "/auth/logout", "Allow"),
        ("patient", "POST", "/auth/logout", "Allow"),
        ("doctor", "GET", "/unknown", "Deny"),
    ],
)
def test_role_rules(authorizer, role, method, path, effect):
    token, _ = make_token(role)
    result = invoke(authorizer, token, method, path)
    assert result["policyDocument"]["Statement"][0]["Effect"] == effect


def _assert_unauthorized(authorizer, token):
    with pytest.raises(Exception, match="^Unauthorized$"):
        invoke(authorizer, token)


def test_expired_token_is_401(authorizer):
    token, _ = make_token(now=time.time() - 2000)
    _assert_unauthorized(authorizer, token)


def test_wrongly_signed_token_is_401(authorizer):
    token, _ = make_token(secret="a-completely-different-secret-of-enough-length!!")
    _assert_unauthorized(authorizer, token)


def test_tampered_payload_is_401(authorizer):
    token, claims = make_token("patient")
    head, _body, signature = token.split(".")
    claims["role"] = "doctor"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    _assert_unauthorized(authorizer, f"{head}.{forged}.{signature}")


def test_alg_none_is_401(authorizer):
    token, _ = make_token()
    _head, body, _sig = token.split(".")
    none_head = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT","iss":"medislot-test"}').rstrip(b"=").decode()
    _assert_unauthorized(authorizer, f"{none_head}.{body}.")


def test_wrong_issuer_is_401(authorizer):
    token, _ = make_token(issuer="someone-else")
    _assert_unauthorized(authorizer, token)


def test_revoked_token_is_401(authorizer, aws):
    token, claims = make_token()
    aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-token-revocations").put_item(
        Item={"jti": claims["jti"], "userId": "user-1", "createdAt": int(time.time()), "expiresAt": claims["exp"]}
    )
    _assert_unauthorized(authorizer, token)


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer not.a.jwt"])
def test_malformed_header_is_401(authorizer, header):
    event = {"type": "TOKEN", "authorizationToken": header, "methodArn": ARN.format(method="GET", path="/patients/me")}
    with pytest.raises(Exception, match="^Unauthorized$"):
        authorizer.lambda_handler(event, FakeContext())
