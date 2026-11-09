"""Input validation helpers. Every failure raises APIError(400)."""

import re
import uuid
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .http import APIError

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def reject_unknown(body, allowed):
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise APIError(400, "Unsupported fields", {"fields": unknown})


def text(value, field, max_length, required=False):
    if value is None:
        if required:
            raise APIError(400, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise APIError(400, f"{field} must be a string")
    value = value.strip()
    if not value:
        if required:
            raise APIError(400, f"{field} is required")
        return None
    if len(value) > max_length:
        raise APIError(400, f"{field} must be at most {max_length} characters")
    return value


def email(value):
    value = text(value, "email", 254, required=True).lower()
    if not EMAIL_PATTERN.match(value):
        raise APIError(400, "email must be a valid email address")
    return value


def integer(value, field, minimum=None, maximum=None, required=False):
    if value is None or value == "":
        if required:
            raise APIError(400, f"{field} is required")
        return None
    if isinstance(value, bool):
        raise APIError(400, f"{field} must be an integer")
    if isinstance(value, str):
        if not re.fullmatch(r"-?\d+", value.strip()):
            raise APIError(400, f"{field} must be an integer")
        value = int(value.strip())
    if not isinstance(value, int):
        raise APIError(400, f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise APIError(400, f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise APIError(400, f"{field} must be at most {maximum}")
    return value


def money(value, field):
    if value is None or isinstance(value, bool):
        raise APIError(400, f"{field} is required")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise APIError(400, f"{field} must be a number") from None
    if not amount.is_finite():
        raise APIError(400, f"{field} must be a number")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount < 0 or amount > Decimal("99999999.99"):
        raise APIError(400, f"{field} must be between 0 and 99999999.99")
    return amount


def iso_date(value, field, required=True):
    if value in (None, ""):
        if required:
            raise APIError(400, f"{field} is required (YYYY-MM-DD)")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise APIError(400, f"{field} must use YYYY-MM-DD") from None


def clock(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value.strip()):
        raise APIError(400, f"{field} must use HH:MM")
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError:
        raise APIError(400, f"{field} must be a valid time") from None
    if parsed.second or parsed.microsecond:
        raise APIError(400, f"{field} must be a whole minute")
    return parsed


def timezone_name(value, field="timezone"):
    value = text(value, field, 64, required=True)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise APIError(400, f"{field} must be an IANA time zone such as Asia/Kolkata") from None
    return value


def uuid_value(value, field):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise APIError(400, f"{field} must be a UUID") from None
