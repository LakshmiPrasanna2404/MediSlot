"""Schedule Lambda (doctor role).

    GET /doctors/me/schedule?date=YYYY-MM-DD&view=day|week
        Day or week view in the doctor's local time: every slot with its
        capacity, booked and held seats, and the patients booked into it.
    GET /doctors/me/appointments?from=YYYY-MM-DD&to=YYYY-MM-DD&status=CONFIRMED
        The doctor's appointments in a local date range (default: next 30 days).

Slots come from DynamoDB (doctor-slots table, key doctorId + startTime), the
patients booked into them from RDS (appointments, the system of record).
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from medislot_common import config, db, logs, profiles, slots
from medislot_common.http import APIError, api_handler, identity, query
from medislot_common.validation import iso_date

STATUSES = ("CONFIRMED", "COMPLETED", "CANCELLED_BY_PATIENT", "CANCELLED_BY_DOCTOR", "NO_SHOW")
SHOWN_ON_SCHEDULE = ("CONFIRMED", "COMPLETED", "NO_SHOW")
MAX_RANGE_DAYS = 92


def _naive_utc(value):
    return value.replace(tzinfo=None)


def schedule(event):
    caller = identity(event, "doctor")
    params = query(event)
    view_name = params.get("view", "day")
    if view_name not in ("day", "week"):
        raise APIError(400, "view must be day or week")
    days = 1 if view_name == "day" else 7

    with db.connection().cursor() as cur:
        doctor = profiles.require_doctor(cur, caller.user_id)
        tz_name = doctor["timezone"]
        first_day = iso_date(params.get("date"), "date", required=False) or slots.local_today(tz_name, slots.now_utc())
        start_utc, end_utc = slots.doctor_local_range(tz_name, first_day, days)

        cur.execute(
            """SELECT dct.consultation_type_id, ct.name, dct.fee, ct.duration_minutes
               FROM doctor_consultation_types dct
               JOIN consultation_types ct ON ct.consultation_type_id = dct.consultation_type_id
               WHERE dct.doctor_id = %s""",
            (doctor["doctor_id"],),
        )
        types = {row["consultation_type_id"]: row for row in cur.fetchall()}
        cur.execute(
            f"""SELECT a.appointment_id, a.slot_id, a.status, a.patient_id, u.first_name AS patient_first_name,
                       u.last_name AS patient_last_name, p.phone AS patient_phone
                FROM appointments a
                JOIN patients p ON p.patient_id = a.patient_id
                JOIN users u ON u.user_id = p.user_id
                WHERE a.doctor_id = %s AND a.slot_start_utc >= %s AND a.slot_start_utc < %s
                  AND a.status IN ({', '.join(['%s'] * len(SHOWN_ON_SCHEDULE))})
                ORDER BY a.slot_start_utc, u.first_name, u.last_name""",
            (doctor["doctor_id"], _naive_utc(start_utc), _naive_utc(end_utc), *SHOWN_ON_SCHEDULE),
        )
        patients_by_slot = {}
        for row in cur.fetchall():
            patients_by_slot.setdefault(row.pop("slot_id"), []).append(row)

    items = slots.query_doctor_slots(
        config.table("SLOTS_TABLE"), doctor["doctor_id"], slots.iso_utc(start_utc), slots.iso_utc(end_utc)
    )
    by_day = {first_day + timedelta(days=offset): [] for offset in range(days)}
    for item in sorted(items, key=lambda i: i["startTime"]):
        entry = slots.slot_view(item)
        type_row = types.get(entry["consultation_type_id"], {})
        entry["consultation_type_name"] = type_row.get("name")
        entry["patients"] = patients_by_slot.get(item["slotId"], [])
        local_day = slots.parse_iso_utc(item["startTime"]).astimezone(ZoneInfo(tz_name)).date()
        by_day.setdefault(local_day, []).append(entry)

    logs.info("schedule read", doctorId=doctor["doctor_id"], view=view_name, date=first_day, slots=len(items),
              bookedPatients=sum(len(rows) for rows in patients_by_slot.values()))
    return 200, {
        "doctor_id": doctor["doctor_id"],
        "timezone": tz_name,
        "view": view_name,
        "from": first_day,
        "to": first_day + timedelta(days=days - 1),
        "days": [{"date": day, "slots": by_day[day]} for day in sorted(by_day)],
    }


def appointments(event):
    caller = identity(event, "doctor")
    params = query(event)
    status = params.get("status")
    if status is not None and status not in STATUSES:
        raise APIError(400, "status must be one of " + ", ".join(STATUSES))
    with db.connection().cursor() as cur:
        doctor = profiles.require_doctor(cur, caller.user_id)
        tz_name = doctor["timezone"]
        first = iso_date(params.get("from"), "from", required=False) or slots.local_today(tz_name, slots.now_utc())
        last = iso_date(params.get("to"), "to", required=False) or first + timedelta(days=29)
        if last < first:
            raise APIError(400, "to must not be before from")
        if (last - first).days >= MAX_RANGE_DAYS:
            raise APIError(400, f"The range can cover at most {MAX_RANGE_DAYS} days")
        start_utc, end_utc = slots.doctor_local_range(tz_name, first, (last - first).days + 1)
        sql = """SELECT a.appointment_id, a.slot_id, a.status, a.slot_start_utc, a.slot_end_utc,
                        a.consultation_fee, a.reschedule_count, ct.name AS consultation_type_name,
                        a.patient_id, u.first_name AS patient_first_name, u.last_name AS patient_last_name
                 FROM appointments a
                 JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
                 JOIN patients p ON p.patient_id = a.patient_id
                 JOIN users u ON u.user_id = p.user_id
                 WHERE a.doctor_id = %s AND a.slot_start_utc >= %s AND a.slot_start_utc < %s"""
        values = [doctor["doctor_id"], _naive_utc(start_utc), _naive_utc(end_utc)]
        if status:
            sql += " AND a.status = %s"
            values.append(status)
        cur.execute(sql + " ORDER BY a.slot_start_utc LIMIT 500", values)
        rows = cur.fetchall()
    logs.info("appointments listed", doctorId=doctor["doctor_id"], dateFrom=first, dateTo=last, status=status,
              count=len(rows))
    return 200, {"timezone": tz_name, "from": first, "to": last, "appointments": rows}


lambda_handler = api_handler(
    {
        "GET /doctors/me/schedule": schedule,
        "GET /doctors/me/appointments": appointments,
    }
)
