"""Consultation Types Lambda (doctor role).

    GET    /doctors/me/consultation-types
    POST   /doctors/me/consultation-types
    PUT    /doctors/me/consultation-types/{consultationTypeId}
    DELETE /doctors/me/consultation-types/{consultationTypeId}

Frozen rule set:
    Quick Consult        15 min  capacity 1
    Standard Consult     30 min  capacity 1
    Detailed Consult     60 min  capacity 1
    Health Camp Session  60 min  capacity 2 to 10

Name and duration are fixed once created. Capacity (Health Camp only) can
change only while no active weekly pattern uses the type, because existing
slots were materialised with the old capacity. A type used by an active
pattern cannot be deleted; delete is a soft delete (is_active = 0) because
appointments keep referring to it.
"""

from medislot_common import db, profiles
from medislot_common.http import APIError, api_handler, identity, parse_body, path_param
from medislot_common.validation import integer, money, reject_unknown, text

TYPES = {
    "Quick Consult": {"duration": 15, "capacity": (1, 1)},
    "Standard Consult": {"duration": 30, "capacity": (1, 1)},
    "Detailed Consult": {"duration": 60, "capacity": (1, 1)},
    "Health Camp Session": {"duration": 60, "capacity": (2, 10)},
}

COLUMNS = """consultation_type_id, name, description, fee, duration_minutes, capacity,
             is_active, created_at, updated_at"""


def _doctor_id(cur, event):
    return profiles.require_doctor(cur, identity(event, "doctor").user_id)["doctor_id"]


def _type_id(event):
    return integer(path_param(event, "consultationTypeId"), "consultationTypeId", minimum=1)


def _active_pattern_count(cur, doctor_id, type_id):
    cur.execute(
        """SELECT COUNT(*) AS n FROM availability_patterns
           WHERE doctor_id = %s AND consultation_type_id = %s AND is_active = 1""",
        (doctor_id, type_id),
    )
    return cur.fetchone()["n"]


def _check_capacity(name, capacity):
    low, high = TYPES[name]["capacity"]
    if not low <= capacity <= high:
        if low == high:
            raise APIError(400, f"{name} capacity must be {low}")
        raise APIError(400, f"{name} capacity must be between {low} and {high}")


def list_types(event):
    with db.connection().cursor() as cur:
        doctor_id = _doctor_id(cur, event)
        cur.execute(
            f"SELECT {COLUMNS} FROM consultation_types WHERE doctor_id = %s ORDER BY is_active DESC, consultation_type_id",
            (doctor_id,),
        )
        return 200, {"consultation_types": cur.fetchall()}


def create_type(event):
    body = parse_body(event)
    reject_unknown(body, {"name", "duration_minutes", "fee", "capacity", "description"})
    name = text(body.get("name"), "name", 100, required=True)
    if name not in TYPES:
        raise APIError(400, "name must be one of the consultation types", {"allowed": list(TYPES)})
    duration = integer(body.get("duration_minutes"), "duration_minutes") or TYPES[name]["duration"]
    if duration != TYPES[name]["duration"]:
        raise APIError(400, f"{name} lasts {TYPES[name]['duration']} minutes")
    capacity = integer(body.get("capacity"), "capacity")
    if capacity is None:
        capacity = TYPES[name]["capacity"][0]
    _check_capacity(name, capacity)
    fee = money(body.get("fee"), "fee")
    description = text(body.get("description"), "description", 255)

    with db.transaction() as cur:
        doctor_id = _doctor_id(cur, event)
        cur.execute(
            """INSERT INTO consultation_types (doctor_id, name, description, fee, duration_minutes, capacity)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (doctor_id, name, description, fee, duration, capacity),
        )
        type_id = cur.lastrowid
        cur.execute(f"SELECT {COLUMNS} FROM consultation_types WHERE consultation_type_id = %s", (type_id,))
        return 201, {"consultation_type": cur.fetchone()}


def update_type(event):
    type_id = _type_id(event)
    body = parse_body(event)
    reject_unknown(body, {"name", "duration_minutes", "fee", "capacity", "description"})
    if not body:
        raise APIError(400, "Provide at least one field to change")
    with db.transaction() as cur:
        doctor_id = _doctor_id(cur, event)
        cur.execute(
            f"SELECT {COLUMNS} FROM consultation_types WHERE consultation_type_id = %s AND doctor_id = %s FOR UPDATE",
            (type_id, doctor_id),
        )
        current = cur.fetchone()
        if not current or not current["is_active"]:
            raise APIError(404, "Consultation type not found")
        if "name" in body and body["name"] != current["name"]:
            raise APIError(400, "name cannot change; create a new consultation type instead")
        if "duration_minutes" in body and integer(body["duration_minutes"], "duration_minutes") != current["duration_minutes"]:
            raise APIError(400, "duration_minutes cannot change; create a new consultation type instead")
        changes = {}
        if "fee" in body:
            changes["fee"] = money(body["fee"], "fee")
        if "description" in body:
            changes["description"] = text(body["description"], "description", 255)
        if "capacity" in body:
            capacity = integer(body["capacity"], "capacity", required=True)
            _check_capacity(current["name"], capacity)
            if capacity != current["capacity"]:
                if _active_pattern_count(cur, doctor_id, type_id):
                    raise APIError(409, "Capacity cannot change while weekly availability uses this type")
                changes["capacity"] = capacity
        if changes:
            assignments = ", ".join(f"{column} = %s" for column in changes)
            cur.execute(
                f"UPDATE consultation_types SET {assignments} WHERE consultation_type_id = %s",
                [*changes.values(), type_id],
            )
        cur.execute(f"SELECT {COLUMNS} FROM consultation_types WHERE consultation_type_id = %s", (type_id,))
        return 200, {"consultation_type": cur.fetchone()}


def delete_type(event):
    type_id = _type_id(event)
    with db.transaction() as cur:
        doctor_id = _doctor_id(cur, event)
        cur.execute(
            "SELECT is_active FROM consultation_types WHERE consultation_type_id = %s AND doctor_id = %s FOR UPDATE",
            (type_id, doctor_id),
        )
        row = cur.fetchone()
        if not row or not row["is_active"]:
            raise APIError(404, "Consultation type not found")
        if _active_pattern_count(cur, doctor_id, type_id):
            raise APIError(409, "Remove this type from your weekly availability first")
        cur.execute("UPDATE consultation_types SET is_active = 0 WHERE consultation_type_id = %s", (type_id,))
    return 204, None


lambda_handler = api_handler(
    {
        "GET /doctors/me/consultation-types": list_types,
        "POST /doctors/me/consultation-types": create_type,
        "PUT /doctors/me/consultation-types/{consultationTypeId}": update_type,
        "DELETE /doctors/me/consultation-types/{consultationTypeId}": delete_type,
    }
)
