"""API Gateway (HTTP API, Lambda proxy, payload format 2.0) request and response helpers.

The acting user is read ONLY from the Lambda authorizer context
(requestContext.authorizer.lambda). Request bodies, query strings and headers
are never used to decide who the caller is.

Events in the REST API format (payload 1.0) are still understood, so requests
keep working while a deployment switches the API from REST to HTTP.
"""

import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal

import pymysql

from . import logs, schemas

CORRELATION_HEADER = "X-Correlation-Id"
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class APIError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Identity:
    user_id: str
    role: str
    jti: str
    exp: int


def json_default(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # MySQL DATETIME/TIMESTAMP values are UTC (session time_zone is +00:00).
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")
        return value.isoformat()
    if isinstance(value, (date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        # MySQL TIME columns arrive as timedelta: always render HH:MM.
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}"
    if isinstance(value, Decimal):
        # Money and counters: integers stay integers, the rest keep 2 places.
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def response(status, payload=None, headers=None):
    result_headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }
    if logs.correlation_id():
        result_headers[CORRELATION_HEADER] = logs.correlation_id()
    if headers:
        result_headers.update(headers)
    if status == 204:
        result_headers.pop("Content-Type", None)
        return {"statusCode": 204, "headers": result_headers, "body": ""}
    return {
        "statusCode": status,
        "headers": result_headers,
        "body": json.dumps(payload if payload is not None else {}, default=json_default),
    }


def header(event, name):
    headers = event.get("headers") or {}
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None


def parse_body(event):
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "Request body must be valid JSON") from None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise APIError(400, "Request body must be valid JSON") from None
    if not isinstance(value, dict):
        raise APIError(400, "Request body must be a JSON object")
    return value


def query(event):
    return dict(event.get("queryStringParameters") or {})


def path_param(event, name):
    value = (event.get("pathParameters") or {}).get(name)
    if value in (None, ""):
        raise APIError(400, f"{name} is required")
    return value


def authorizer_context(event):
    """The context map the Lambda authorizer returned for this request.

    HTTP API: requestContext.authorizer.lambda. REST API: requestContext.authorizer.
    Public routes have none.
    """
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    if "lambda" in authorizer:
        return authorizer.get("lambda") or {}
    return authorizer


def identity(event, required_role=None):
    """Return the caller as resolved by the Lambda authorizer."""
    context = authorizer_context(event)
    user_id = context.get("userId")
    role = context.get("role")
    if not user_id or role not in ("doctor", "patient"):
        raise APIError(401, "Missing authenticated identity")
    if required_role and role != required_role:
        raise APIError(403, f"{required_role.title()} access required")
    try:
        exp = int(context.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    return Identity(user_id=str(user_id), role=role, jti=str(context.get("jti") or ""), exp=exp)


def route_key(event):
    """'GET /doctors/me/consultation-types/{consultationTypeId}' style key.

    HTTP API events carry it as routeKey; REST API events are rebuilt from the
    method and the resource.
    """
    if event.get("routeKey"):
        return event["routeKey"]
    return f"{(event.get('httpMethod') or '').upper()} {event.get('resource') or event.get('path') or ''}"


def check_body(event, key):
    """400 'Invalid request body' when a route's body does not match its schema."""
    schema = schemas.ROUTE_BODIES.get(key)
    if schema is None:
        return
    found = schemas.problems(parse_body(event), schema)
    if found:
        logs.warning("request body rejected", problems=found[:10])
        raise APIError(400, "Invalid request body", found)


def _correlation_from(event, context):
    candidate = header(event, CORRELATION_HEADER)
    if isinstance(candidate, str) and _CORRELATION_PATTERN.match(candidate):
        return candidate
    request_id = (event.get("requestContext") or {}).get("requestId")
    if request_id:
        return request_id
    return getattr(context, "aws_request_id", None) or str(uuid.uuid4())


_MYSQL_CLIENT_ERRORS = {
    1062: (409, "This record already exists"),
    1048: (400, "A required value is missing"),
    1264: (400, "A value is out of range"),
    1406: (400, "A value is too long"),
    1452: (400, "A value refers to a record that does not exist"),
    3819: (400, "The values break a database rule"),
}


def _short(value, limit=100):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _request_fields(event):
    """What the request asked for, for the 'request received' line. Never the body."""
    request = event.get("requestContext") or {}
    source = request.get("http") or request.get("identity") or {}
    return {
        "pathParameters": {k: _short(v) for k, v in (event.get("pathParameters") or {}).items()} or None,
        "queryParameters": {k: _short(v) for k, v in (event.get("queryStringParameters") or {}).items()} or None,
        "hasBody": bool(event.get("body")),
        "sourceIp": source.get("sourceIp"),
        "userAgent": _short(source.get("userAgent") or header(event, "User-Agent") or "", 120) or None,
    }


def api_handler(routes):
    """Build a Lambda handler that dispatches on 'METHOD /resource'.

    Each route function receives (event) and returns (status, payload).
    A route with a body schema (schemas.ROUTE_BODIES) only runs when the body
    matches it. Every request writes a 'request received' line (route, caller, path and
    query parameters, source IP) and a 'request completed' line (status,
    duration, and the error message for a 4xx). Lines in between, written by
    the route itself, carry the same route, userId and role.
    """
    def handler(event, context):
        logs.set_correlation_id(
            _correlation_from(event, context),
            getattr(context, "aws_request_id", None),
            (event.get("requestContext") or {}).get("requestId"),
        )
        started = time.perf_counter()
        key = route_key(event)
        caller = authorizer_context(event)
        logs.set_context(route=key, userId=caller.get("userId"), role=caller.get("role"))
        logs.info("request received", **_request_fields(event))
        error_message = None
        try:
            route = routes.get(key)
            if route is None:
                raise APIError(404, "Route not found")
            check_body(event, key)
            status, payload = route(event)
            result = response(status, payload)
        except APIError as exc:
            body = {"error": exc.message}
            if exc.details is not None:
                body["details"] = exc.details
            error_message = exc.message
            result = response(exc.status, body)
        except pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            if code in _MYSQL_CLIENT_ERRORS:
                status, message = _MYSQL_CLIENT_ERRORS[code]
                logs.warning("Database rejected the request", mysqlCode=code)
                result = response(status, {"error": message})
            else:
                logs.exception("Database error", mysqlCode=code)
                if code in (2003, 2006, 2013):
                    result = response(503, {"error": "Database temporarily unavailable"})
                else:
                    result = response(500, {"error": "Internal server error"})
        except Exception:  # noqa: BLE001 - last-resort guard, logged with stack trace
            logs.exception("Unhandled error", route=key)
            result = response(500, {"error": "Internal server error"})
        status = result["statusCode"]
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        logs.log(
            level,
            "request completed",
            status=status,
            outcome="ok" if status < 400 else "rejected" if status < 500 else "failed",
            error=error_message,
            durationMs=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    return handler
