"""Request body schemas, checked before a route runs.

HTTP APIs in API Gateway do not validate request bodies, so the JSON schemas
the REST API used to check are kept here and applied by http.api_handler to
every route that takes a body. A body that does not match gets 400 "Invalid
request body" with the list of problems, and the route never runs.

Only the parts of JSON Schema these schemas use are supported: type,
required, properties, additionalProperties: false, minLength, maxLength,
pattern, enum, minimum, maximum, items and maxItems. An empty schema ({})
accepts any value; the route's own validation checks those fields.
"""

import re

ANY = {}
STRING = {"type": "string"}
NAME = {"type": "string", "minLength": 1, "maxLength": 60}
CLOCK_TIME = {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"}
DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}


def _object(properties, required=()):
    schema = {"type": "object", "additionalProperties": False, "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


_DOCTOR_PROFILE = {
    "licence_number": STRING,
    "specialisation": STRING,
    "hospital_name": STRING,
    "hospital_address": STRING,
    "hospital_city": STRING,
    "qualifications": STRING,
    "languages": STRING,
    "timezone": STRING,
    "years_experience": ANY,
}
_PATIENT_PROFILE = {
    "date_of_birth": STRING,
    "gender": STRING,
    "phone": STRING,
    "address": STRING,
    "city": STRING,
    "emergency_contact_name": STRING,
    "emergency_contact_phone": STRING,
}
_ACCOUNT = {"first_name": NAME, "last_name": NAME, "email": STRING, "password": STRING}

LOGIN = _object({"email": STRING, "password": STRING}, ["email", "password"])
REFRESH = _object({"refresh_token": STRING}, ["refresh_token"])
LOGOUT = _object({"refresh_token": STRING})
DOCTOR_REGISTRATION = _object(
    {**_ACCOUNT, **_DOCTOR_PROFILE}, ["first_name", "last_name", "email", "password", "licence_number"]
)
PATIENT_REGISTRATION = _object({**_ACCOUNT, **_PATIENT_PROFILE}, ["first_name", "last_name", "email", "password"])
DOCTOR_PROFILE_UPDATE = _object({"first_name": NAME, "last_name": NAME, **_DOCTOR_PROFILE})
PATIENT_PROFILE_UPDATE = _object({"first_name": NAME, "last_name": NAME, **_PATIENT_PROFILE})
PHOTO_UPLOAD = _object(
    {"content_type": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp"]}}, ["content_type"]
)
CONSULTATION_TYPE_CREATE = _object(
    {
        "consultation_type_id": {"type": "integer", "minimum": 1},
        "name": STRING,
        "description": STRING,
        "fee": ANY,
        "duration_minutes": ANY,
        "capacity": ANY,
    },
    ["fee"],
)
CONSULTATION_TYPE_UPDATE = _object(
    {"name": STRING, "description": STRING, "fee": ANY, "duration_minutes": ANY, "capacity": ANY}
)
AVAILABILITY_REPLACE = _object(
    {
        "patterns": {
            "type": "array",
            "maxItems": 50,
            "items": _object(
                {"consultation_type_id": ANY, "day_of_week": ANY, "start_time": CLOCK_TIME, "end_time": CLOCK_TIME},
                ["consultation_type_id", "day_of_week", "start_time", "end_time"],
            ),
        }
    },
    ["patterns"],
)
BLACKOUT_CREATE = _object({"date": DATE, "reason": STRING}, ["date"])

# Route key ('METHOD /path' as API Gateway names the route) -> body schema.
ROUTE_BODIES = {
    "POST /auth/doctors/register": DOCTOR_REGISTRATION,
    "POST /auth/patients/register": PATIENT_REGISTRATION,
    "POST /auth/doctors/login": LOGIN,
    "POST /auth/patients/login": LOGIN,
    "POST /auth/refresh": REFRESH,
    "POST /auth/logout": LOGOUT,
    "PUT /doctors/me": DOCTOR_PROFILE_UPDATE,
    "POST /doctors/me/photo": PHOTO_UPLOAD,
    "POST /doctors/me/consultation-types": CONSULTATION_TYPE_CREATE,
    "PUT /doctors/me/consultation-types/{consultationTypeId}": CONSULTATION_TYPE_UPDATE,
    "PUT /doctors/me/availability": AVAILABILITY_REPLACE,
    "POST /doctors/me/blackouts": BLACKOUT_CREATE,
    "PUT /patients/me": PATIENT_PROFILE_UPDATE,
}

_TYPE_NAMES = {
    "object": "an object",
    "array": "a list",
    "string": "a string",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "true or false",
}


def _is_type(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if expected == "integer":
        return isinstance(value, int)
    if expected == "number":
        return isinstance(value, (int, float))
    return True


def _matches(pattern, value):
    # JSON Schema patterns follow JavaScript, where a final $ does not accept a
    # trailing newline; Python's $ would.
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value) is not None


def _check(value, schema, where, problems):
    label = where or "body"
    expected = schema.get("type")
    if expected and not _is_type(value, expected):
        problems.append(f"{label} must be {_TYPE_NAMES.get(expected, expected)}")
        return
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{label} must be one of: {', '.join(str(v) for v in schema['enum'])}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{label} must be at least {schema['minLength']} character(s)")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{label} must be at most {schema['maxLength']} characters")
        if "pattern" in schema and not _matches(schema["pattern"], value):
            problems.append(f"{label} has the wrong format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{label} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{label} must be at most {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{label} must have at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _check(item, schema["items"], f"{label}[{index}]", problems)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                problems.append(f"{_join(where, name)} is required")
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(properties)):
                problems.append(f"{_join(where, name)} is not allowed")
        for name, sub_schema in properties.items():
            if name in value:
                _check(value[name], sub_schema, _join(where, name), problems)


def _join(where, name):
    return f"{where}.{name}" if where else name


def problems(value, schema):
    """Every way the value breaks the schema, as short messages ([] if it fits)."""
    found = []
    _check(value, schema, "", found)
    return found
