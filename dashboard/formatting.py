"""Jinja filters and shared constants for the templates."""

from datetime import datetime

from markupsafe import Markup

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


def person_name(row, prefix=""):
    """'First Last' from an API record: {{ doctor|person_name }}, {{ p|person_name('patient_') }}."""
    row = row or {}
    parts = (row.get(f"{prefix}first_name"), row.get(f"{prefix}last_name"))
    return " ".join(str(part) for part in parts if part)


def initials(name):
    """Two letters for an avatar: "Dr. Ananya Rao" -> "AR"."""
    words = [w for w in str(name or "").replace(".", " ").split() if w.lower() not in ("dr", "mr", "mrs", "ms")]
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


# Line icons drawn on a 24x24 grid, stroked with the current text colour.
ICONS = {
    "calendar": Markup('<rect x="3" y="4.5" width="18" height="17" rx="2"/><path d="M16 2.5v4M8 2.5v4M3 10h18"/>'),
    "calendar-x": Markup(
        '<rect x="3" y="4.5" width="18" height="17" rx="2"/>'
        '<path d="M16 2.5v4M8 2.5v4M3 10h18M9.5 13.5l5 5M14.5 13.5l-5 5"/>'
    ),
    "clock": Markup('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>'),
    "clipboard": Markup(
        '<rect x="8" y="2.5" width="8" height="4" rx="1"/>'
        '<path d="M16 4.5h2a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h2M9 12h6M9 16h4"/>'
    ),
    "user": Markup('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
    "users": Markup('<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M16 4a4 4 0 0 1 0 8M22 21a7 7 0 0 0-4.5-6.5"/>'),
    "search": Markup('<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>'),
    "log-out": Markup('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>'),
    "arrow-left": Markup('<path d="M19 12H5M12 19l-7-7 7-7"/>'),
    "chevron-right": Markup('<path d="m9 18 6-6-6-6"/>'),
    "map-pin": Markup('<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>'),
    "briefcase": Markup(
        '<rect x="2.5" y="7" width="19" height="14" rx="2"/>'
        '<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>'
    ),
    "cap": Markup('<path d="M22 9.5 12 4.5 2 9.5l10 5 10-5Z"/><path d="M6 11.5v5c3 2.7 9 2.7 12 0v-5M22 9.5v6"/>'),
    "message": Markup('<path d="M21 12a8.5 8.5 0 0 1-12.3 7.6L3 21l1.5-5.4A8.5 8.5 0 1 1 21 12Z"/>'),
    "mail": Markup('<rect x="2.5" y="4.5" width="19" height="15" rx="2"/><path d="m21.5 7-9.5 6-9.5-6"/>'),
    "id-card": Markup(
        '<rect x="2.5" y="5" width="19" height="14" rx="2"/>'
        '<circle cx="8.5" cy="12" r="2.2"/><path d="M14 10h4M14 14h4"/>'
    ),
    "hospital": Markup('<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M12 7v6M9 10h6M10 21v-4h4v4"/>'),
    "stethoscope": Markup(
        '<path d="M6 3H5a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1"/>'
        '<path d="M9 15a6 6 0 0 0 12 0v-3"/><circle cx="21" cy="10" r="2"/>'
    ),
    "plus": Markup('<path d="M12 5v14M5 12h14"/>'),
    "check": Markup('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
    "trash": Markup(
        '<path d="M3.5 6h17M9 6V4h6v2M18.5 6l-.9 13.2a2 2 0 0 1-2 1.8H8.4a2 2 0 0 1-2-1.8L5.5 6"/>'
        '<path d="M10 10.5v6M14 10.5v6"/>'
    ),
    "upload": Markup('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>'),
    "check-circle": Markup('<circle cx="12" cy="12" r="9"/><path d="m8 12.3 2.8 2.8L16.2 9.5"/>'),
    "alert-circle": Markup('<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5M12 16.2v.3"/>'),
    "alert-triangle": Markup(
        '<path d="M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>'
        '<path d="M12 9v4.5M12 17v.3"/>'
    ),
    "info": Markup('<circle cx="12" cy="12" r="9"/><path d="M12 16.5v-5M12 7.8v.3"/>'),
}

_SVG = Markup(
    '<svg class="icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>'
)


def icon(name, size=18, cls=""):
    """Inline SVG icon. Decorative: the text next to it carries the meaning."""
    return _SVG.format(cls=cls, size=int(size), body=ICONS[name])


def register(app):
    app.jinja_env.filters.update(local_time=local_time, local_datetime=local_datetime, long_date=long_date, money=money,
                                 initials=initials, person_name=person_name)
    app.jinja_env.globals.update(
        DAYS=DAYS, SPECIALISATIONS=SPECIALISATIONS, TIMEZONES=TIMEZONES, CONSULTATION_TYPES=CONSULTATION_TYPES, icon=icon
    )
