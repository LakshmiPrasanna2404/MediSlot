"""API Gateway (REST, Lambda proxy) request and response helpers.

The acting user is read ONLY from the Lambda authorizer context
(requestContext.authorizer). Request bodies, query strings and headers are
never used to decide who the caller is.
"""

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal

import pymysql

from . import logs

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


def identity(event, required_role=None):
    """Return the caller as resolved by the Lambda authorizer.

    API Gateway puts the authorizer's context map at requestContext.authorizer.
    """
    context = (event.get("requestContext") or {}).get("authorizer") or {}
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
    """'GET /doctors/me/consultation-types/{consultationTypeId}' style key."""
    return f"{(event.get('httpMethod') or '').upper()} {event.get('resource') or event.get('path') or ''}"


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


def api_handler(routes):
    """Build a Lambda handler that dispatches on 'METHOD /resource'.

    Each route function receives (event) and returns (status, payload).
    """
    def handler(event, context):
        logs.set_correlation_id(
            _correlation_from(event, context),
            getattr(context, "aws_request_id", None),
            (event.get("requestContext") or {}).get("requestId"),
        )
        started = time.perf_counter()
        key = route_key(event)
        try:
            route = routes.get(key)
            if route is None:
                raise APIError(404, "Route not found")
            status, payload = route(event)
            result = response(status, payload)
        except APIError as exc:
            body = {"error": exc.message}
            if exc.details is not None:
                body["details"] = exc.details
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
        logs.info(
            "request completed",
            route=key,
            status=result["statusCode"],
            durationMs=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    return handler
