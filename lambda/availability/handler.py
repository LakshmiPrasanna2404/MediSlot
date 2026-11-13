"""Availability + Blackouts Lambda (doctor role).

    GET    /doctors/me/availability
    PUT    /doctors/me/availability          replace the whole weekly pattern set
    GET    /doctors/me/blackouts
    POST   /doctors/me/blackouts             {"date": "YYYY-MM-DD", "reason": "..."}
    DELETE /doctors/me/blackouts/{blackoutId}

Patterns are recurring weekly windows in the doctor's local time:
day_of_week (ISO, Monday=1 ... Sunday=7), start_time, end_time, consultation
type. Two patterns on the same day may not overlap, because a doctor cannot
be in two slots at once.

After every change the doctor's slots are synced straight away (same code as
the daily materialiser), so new patterns become bookable and removed patterns
or new blackout dates stop being bookable. A slot somebody already holds a
seat in is never deleted; it is reported back so the doctor can cancel it.
"""

import uuid
from datetime import timedelta

import pymysql

from medislot_common import config, db, logs, profiles, slots
from medislot_common.http import APIError, api_handler, identity, parse_body, path_param
from medislot_common.validation import clock, integer, iso_date, reject_unknown, text

MAX_PATTERNS = 50
DAY_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}


def _doctor(cur, event):
    return profiles.require_doctor(cur, identity(event, "doctor").user_id)


def _sync(doctor_id):
    with db.connection().cursor() as cur:
        summary = slots.sync_from_database(
            cur, config.table("SLOTS_TABLE"), doctor_id, slots.now_utc(),
            config.config_int("slots/booking-window-days"),
        )
    logs.info("slots synced", **summary)
    return summary


def _minutes(value):
    return value.hour * 60 + value.minute


def validate_patterns(raw, types):
    """Return validated patterns or raise 400. `types` maps id -> type row."""
    if not isinstance(raw, list):
        raise APIError(400, "patterns must be a list")
    if len(raw) > MAX_PATTERNS:
        raise APIError(400, f"At most {MAX_PATTERNS} weekly patterns are allowed")
    result = []
    for index, item in enumerate(raw):
        label = f"patterns[{index}]"
        if not isinstance(item, dict):
            raise APIError(400, f"{label} must be an object")
        reject_unknown(item, {"consultation_type_id", "day_of_week", "start_time", "end_time"})
        type_id = integer(item.get("consultation_type_id"), f"{label}.consultation_type_id", minimum=1, required=True)
        if type_id not in types:
            raise APIError(400, f"{label}.consultation_type_id is not one of your active consultation types")
        day = integer(item.get("day_of_week"), f"{label}.day_of_week", 1, 7, required=True)
        start = clock(item.get("start_time"), f"{label}.start_time")
        end = clock(item.get("end_time"), f"{label}.end_time")
        if end <= start:
            raise APIError(400, f"{label}: end_time must be after start_time")
        duration = int(types[type_id]["duration_minutes"])
        if _minutes(end) - _minutes(start) < duration:
            raise APIError(400, f"{label}: the window is shorter than one {duration}-minute slot")
        result.append({"consultation_type_id": type_id, "day_of_week": day, "start_time": start, "end_time": end})

    by_day = {}
    for pattern in result:
        by_day.setdefault(pattern["day_of_week"], []).append(pattern)
    for day, day_patterns in by_day.items():
        day_patterns.sort(key=lambda p: p["start_time"])
        for earlier, later in zip(day_patterns, day_patterns[1:], strict=False):
            if later["start_time"] < earlier["end_time"]:
                raise APIError(
                    400,
                    f"Patterns overlap on {DAY_NAMES[day]}: "
                    f"{earlier['start_time']:%H:%M}-{earlier['end_time']:%H:%M} and "
                    f"{later['start_time']:%H:%M}-{later['end_time']:%H:%M}",
                )
    return result


def get_availability(event):
    with db.connection().cursor() as cur:
        doctor = _doctor(cur, event)
        cur.execute(
            """SELECT ap.availability_id, ap.consultation_type_id, ct.name AS consultation_type_name,
                      ct.duration_minutes, ap.day_of_week, ap.start_time, ap.end_time
               FROM availability_patterns ap
               JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id
               WHERE ap.doctor_id = %s AND ap.is_active = 1
               ORDER BY ap.day_of_week, ap.start_time""",
            (doctor["doctor_id"],),
        )
        return 200, {"timezone": doctor["timezone"], "patterns": cur.fetchall()}


def replace_availability(event):
    body = parse_body(event)
    reject_unknown(body, {"patterns"})
    with db.transaction() as cur:
        doctor = _doctor(cur, event)
        cur.execute(
            """SELECT dct.consultation_type_id, ct.duration_minutes
               FROM doctor_consultation_types dct
               JOIN consultation_types ct ON ct.consultation_type_id = dct.consultation_type_id
               WHERE dct.doctor_id = %s AND dct.is_active = 1""",
            (doctor["doctor_id"],),
        )
        types = {row["consultation_type_id"]: row for row in cur.fetchall()}
        patterns = validate_patterns(body.get("patterns"), types)
        cur.execute(
            "UPDATE availability_patterns SET is_active = 0 WHERE doctor_id = %s AND is_active = 1",
            (doctor["doctor_id"],),
        )
        if patterns:
            cur.executemany(
                """INSERT INTO availability_patterns
                       (availability_id, doctor_id, consultation_type_id, day_of_week, start_time, end_time)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [
                    (str(uuid.uuid4()), doctor["doctor_id"], p["consultation_type_id"], p["day_of_week"],
                     p["start_time"], p["end_time"])
                    for p in patterns
                ],
            )
    logs.info("weekly availability saved", doctorId=doctor["doctor_id"], patterns=len(patterns),
              days=sorted({p["day_of_week"] for p in patterns}),
              consultationTypeIds=sorted({p["consultation_type_id"] for p in patterns}))
    return 200, {"patterns_saved": len(patterns), "slot_sync": _sync(doctor["doctor_id"])}


def list_blackouts(event):
    with db.connection().cursor() as cur:
        doctor = _doctor(cur, event)
        cur.execute(
            "SELECT blackout_id, `date`, reason, created_at FROM blackout_dates WHERE doctor_id = %s ORDER BY `date`",
            (doctor["doctor_id"],),
        )
        return 200, {"blackouts": cur.fetchall()}


def create_blackout(event):
    body = parse_body(event)
    reject_unknown(body, {"date", "reason"})
    day = iso_date(body.get("date"), "date")
    reason = text(body.get("reason"), "reason", 255)
    with db.transaction() as cur:
        doctor = _doctor(cur, event)
        today = slots.local_today(doctor["timezone"], slots.now_utc())
        if day < today:
            raise APIError(400, "date must be today or later in your time zone")
        if day > today + timedelta(days=366):
            raise APIError(400, "date must be within the next year")
        try:
            cur.execute(
                "INSERT INTO blackout_dates (doctor_id, `date`, reason) VALUES (%s, %s, %s)",
                (doctor["doctor_id"], day, reason),
            )
        except pymysql.err.IntegrityError:
            raise APIError(409, "That date is already a blackout date") from None
        blackout_id = cur.lastrowid
    logs.info("blackout date added", doctorId=doctor["doctor_id"], blackoutId=blackout_id, date=day)
    return 201, {
        "blackout": {"blackout_id": blackout_id, "date": day, "reason": reason},
        "slot_sync": _sync(doctor["doctor_id"]),
    }


def delete_blackout(event):
    blackout_id = integer(path_param(event, "blackoutId"), "blackoutId", minimum=1)
    with db.transaction() as cur:
        doctor = _doctor(cur, event)
        cur.execute(
            "DELETE FROM blackout_dates WHERE blackout_id = %s AND doctor_id = %s",
            (blackout_id, doctor["doctor_id"]),
        )
        if cur.rowcount == 0:
            raise APIError(404, "Blackout date not found")
    logs.info("blackout date removed", doctorId=doctor["doctor_id"], blackoutId=blackout_id)
    return 200, {"message": "Blackout date removed", "slot_sync": _sync(doctor["doctor_id"])}


lambda_handler = api_handler(
    {
        "GET /doctors/me/availability": get_availability,
        "PUT /doctors/me/availability": replace_availability,
        "GET /doctors/me/blackouts": list_blackouts,
        "POST /doctors/me/blackouts": create_blackout,
        "DELETE /doctors/me/blackouts/{blackoutId}": delete_blackout,
    }
)
