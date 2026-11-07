"""Jinja filters and shared constants for the templates."""

from datetime import datetime

DAYS = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}

# Mirrors database/migrations/V004__seed_specialisations.sql.
SPECIALISATIONS = [
    "General Medicine", "Cardiology", "Dermatology", "Paediatrics", "Orthopaedics", "Gynaecology", "ENT",
    "Ophthalmology", "Neurology", "Psychiatry", "Dentistry", "Pulmonology", "Gastroenterology", "Endocrinology",
    "Nephrology", "Urology", "Oncology",
]

TIMEZONES = [
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Europe/London", "Europe/Berlin",
    "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney", "UTC",
]

CONSULTATION_TYPES = {
    "Quick Consult": 15,
    "Standard Consult": 30,
    "Detailed Consult": 60,
    "Health Camp Session": 60,
}


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_time(value):
    parsed = _parse(value)
    return parsed.strftime("%H:%M") if parsed else ""


def local_datetime(value):
    parsed = _parse(value)
    return parsed.strftime("%a %d %b %Y, %H:%M") if parsed else ""


def long_date(value):
    parsed = _parse(value)
    return parsed.strftime("%A %d %B %Y") if parsed else str(value or "")


def money(value):
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def register(app):
    app.jinja_env.filters.update(local_time=local_time, local_datetime=local_datetime, long_date=long_date, money=money)
    app.jinja_env.globals.update(
        DAYS=DAYS, SPECIALISATIONS=SPECIALISATIONS, TIMEZONES=TIMEZONES, CONSULTATION_TYPES=CONSULTATION_TYPES
    )
