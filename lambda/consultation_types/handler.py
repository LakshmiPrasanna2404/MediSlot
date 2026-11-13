"""Consultation Types Lambda (doctor role).

    GET    /doctors/me/consultation-types
    POST   /doctors/me/consultation-types
    PUT    /doctors/me/consultation-types/{consultationTypeId}
    DELETE /doctors/me/consultation-types/{consultationTypeId}

The four types of the frozen rule set live in the consultation_types
catalogue (name, duration, allowed seats):

    1  Quick Consult        15 min  capacity 1
    2  Standard Consult     30 min  capacity 1
    3  Detailed Consult     60 min  capacity 1
    4  Health Camp Session  60 min  capacity 2 to 10

What a doctor sets for a type is one row in doctor_consultation_types, keyed
by (doctor_id, consultation_type_id): fee, seats, description, active flag.
{consultationTypeId} is the catalogue id, so the same id means the same type
for every doctor.

A doctor offers each type at most once. Capacity (Health Camp only) can
change only while no active weekly pattern uses the type, because existing
slots were materialised with the old capacity. A type used by an active
pattern cannot be removed; removal is a soft delete (is_active = 0) because
appointments keep referring to it. Adding a removed type again reactivates
the same row.
"""

from medislot_common import db, logs, profiles
from medislot_common.http import APIError, api_handler, identity, parse_body, path_param
from medislot_common.validation import integer, money, reject_unknown, text

COLUMNS = """dct.consultation_type_id, ct.name, dct.description, dct.fee, ct.duration_minutes,
             dct.capacity, dct.is_active, dct.created_at, dct.updated_at"""
FROM = """FROM doctor_consultation_types dct
          JOIN consultation_types ct ON ct.consultation_type_id = dct.consultation_type_id"""
FIELDS = {"consultation_type_id", "name", "duration_minutes", "fee", "capacity", "description"}


def _doctor_id(cur, event):
    return profiles.require_doctor(cur, identity(event, "doctor").user_id)["doctor_id"]


def _type_id(event):
    return integer(path_param(event, "consultationTypeId"), "consultationTypeId", minimum=1)


def _catalogue_type(cur, body):
    """The catalogue row named by consultation_type_id or name."""
    if body.get("consultation_type_id") is not None:
        type_id = integer(body["consultation_type_id"], "consultation_type_id", minimum=1)
        cur.execute("SELECT * FROM consultation_types WHERE consultation_type_id = %s", (type_id,))
    else:
        name = text(body.get("name"), "name", 100, required=True)
        cur.execute("SELECT * FROM consultation_types WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        if "name" in body and body["name"] != row["name"]:
            raise APIError(400, "name does not match consultation_type_id")
        return row
    cur.execute("SELECT name FROM consultation_types ORDER BY consultation_type_id")
    raise APIError(400, "name must be one of the consultation types", {"allowed": [r["name"] for r in cur.fetchall()]})


def _check_capacity(type_row, capacity):
    low, high = type_row["min_capacity"], type_row["max_capacity"]
    if not low <= capacity <= high:
        if low == high:
            raise APIError(400, f"{type_row['name']} capacity must be {low}")
        raise APIError(400, f"{type_row['name']} capacity must be between {low} and {high}")


def _active_pattern_count(cur, doctor_id, type_id):
    cur.execute(
        """SELECT COUNT(*) AS n FROM availability_patterns
           WHERE doctor_id = %s AND consultation_type_id = %s AND is_active = 1""",
        (doctor_id, type_id),
    )
    return cur.fetchone()["n"]


def _offering(cur, doctor_id, type_id, lock=False):
    cur.execute(
        f"SELECT {COLUMNS}, ct.min_capacity, ct.max_capacity {FROM} "
        "WHERE dct.doctor_id = %s AND dct.consultation_type_id = %s" + (" FOR UPDATE" if lock else ""),
        (doctor_id, type_id),
    )
    return cur.fetchone()


def _public(row):
    return {key: row[key] for key in (
        "consultation_type_id", "name", "description", "fee", "duration_minutes",
        "capacity", "is_active", "created_at", "updated_at",
    )}


def list_types(event):
    with db.connection().cursor() as cur:
        doctor_id = _doctor_id(cur, event)
        cur.execute(
            f"SELECT {COLUMNS} {FROM} WHERE dct.doctor_id = %s ORDER BY dct.is_active DESC, dct.consultation_type_id",
            (doctor_id,),
        )
        rows = cur.fetchall()
        logs.info("consultation types listed", doctorId=doctor_id, count=len(rows),
                  active=sum(1 for row in rows if row["is_active"]))
        return 200, {"consultation_types": rows}


def create_type(event):
    body = parse_body(event)
    reject_unknown(body, FIELDS)
    with db.transaction() as cur:
        type_row = _catalogue_type(cur, body)
        duration = integer(body.get("duration_minutes"), "duration_minutes") or type_row["duration_minutes"]
        if duration != type_row["duration_minutes"]:
            raise APIError(400, f"{type_row['name']} lasts {type_row['duration_minutes']} minutes")
        capacity = integer(body.get("capacity"), "capacity")
        if capacity is None:
            capacity = type_row["min_capacity"]
        _check_capacity(type_row, capacity)
        fee = money(body.get("fee"), "fee")
        description = text(body.get("description"), "description", 255)

        doctor_id = _doctor_id(cur, event)
        type_id = type_row["consultation_type_id"]
        existing = _offering(cur, doctor_id, type_id, lock=True)
        if existing and existing["is_active"]:
            logs.warning("consultation type already offered", doctorId=doctor_id, consultationTypeId=type_id)
            raise APIError(409, f"You already offer {type_row['name']}; change its fee instead")
        if existing:
            cur.execute(
                """UPDATE doctor_consultation_types SET fee = %s, capacity = %s, description = %s, is_active = 1
                   WHERE doctor_id = %s AND consultation_type_id = %s""",
                (fee, capacity, description, doctor_id, type_id),
            )
        else:
            cur.execute(
                """INSERT INTO doctor_consultation_types (doctor_id, consultation_type_id, fee, capacity, description)
                   VALUES (%s, %s, %s, %s, %s)""",
                (doctor_id, type_id, fee, capacity, description),
            )
        logs.info("consultation type reactivated" if existing else "consultation type added", doctorId=doctor_id,
                  consultationTypeId=type_id, typeName=type_row["name"], fee=fee, capacity=capacity)
        return 201, {"consultation_type": _public(_offering(cur, doctor_id, type_id))}


def update_type(event):
    type_id = _type_id(event)
    body = parse_body(event)
    reject_unknown(body, FIELDS - {"consultation_type_id"})
    if not body:
        raise APIError(400, "Provide at least one field to change")
    with db.transaction() as cur:
        doctor_id = _doctor_id(cur, event)
        current = _offering(cur, doctor_id, type_id, lock=True)
        if not current or not current["is_active"]:
            raise APIError(404, "Consultation type not found")
        if "name" in body and body["name"] != current["name"]:
            raise APIError(400, "name cannot change; add the other consultation type instead")
        if "duration_minutes" in body and integer(body["duration_minutes"], "duration_minutes") != current["duration_minutes"]:
            raise APIError(400, "duration_minutes is fixed for each consultation type")
        changes = {}
        if "fee" in body:
            changes["fee"] = money(body["fee"], "fee")
        if "description" in body:
            changes["description"] = text(body["description"], "description", 255)
        if "capacity" in body:
            capacity = integer(body["capacity"], "capacity", required=True)
            _check_capacity(current, capacity)
            if capacity != current["capacity"]:
                if _active_pattern_count(cur, doctor_id, type_id):
                    raise APIError(409, "Capacity cannot change while weekly availability uses this type")
                changes["capacity"] = capacity
        if changes:
            assignments = ", ".join(f"{column} = %s" for column in changes)
            cur.execute(
                f"UPDATE doctor_consultation_types SET {assignments} WHERE doctor_id = %s AND consultation_type_id = %s",
                [*changes.values(), doctor_id, type_id],
            )
        logs.info("consultation type updated", doctorId=doctor_id, consultationTypeId=type_id, changed=changes)
        return 200, {"consultation_type": _public(_offering(cur, doctor_id, type_id))}


def delete_type(event):
    type_id = _type_id(event)
    with db.transaction() as cur:
        doctor_id = _doctor_id(cur, event)
        current = _offering(cur, doctor_id, type_id, lock=True)
        if not current or not current["is_active"]:
            raise APIError(404, "Consultation type not found")
        if _active_pattern_count(cur, doctor_id, type_id):
            raise APIError(409, "Remove this type from your weekly availability first")
        cur.execute(
            "UPDATE doctor_consultation_types SET is_active = 0 WHERE doctor_id = %s AND consultation_type_id = %s",
            (doctor_id, type_id),
        )
    logs.info("consultation type removed", doctorId=doctor_id, consultationTypeId=type_id)
    return 204, None


lambda_handler = api_handler(
    {
        "GET /doctors/me/consultation-types": list_types,
        "POST /doctors/me/consultation-types": create_type,
        "PUT /doctors/me/consultation-types/{consultationTypeId}": update_type,
        "DELETE /doctors/me/consultation-types/{consultationTypeId}": delete_type,
    }
)
