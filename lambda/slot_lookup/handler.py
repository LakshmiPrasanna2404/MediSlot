"""Slot Lookup Lambda (patient role).

    GET /doctors/{doctorId}/slots?from=YYYY-MM-DD&to=YYYY-MM-DD&consultationTypeId=

Returns the doctor's open future slots between two local dates (the doctor's
time zone, both inclusive; default today .. today + 6), with free seats per
slot: capacity - bookedCount - heldCount. Full slots are included with
free_seats = 0 so the UI can show them as full.
"""

from datetime import timedelta

from medislot_common import config, db, slots
from medislot_common.http import APIError, api_handler, identity, path_param, query
from medislot_common.validation import integer, iso_date, uuid_value


def lookup(event):
    identity(event, "patient")
    doctor_id = uuid_value(path_param(event, "doctorId"), "doctorId")
    params = query(event)
    unknown = sorted(set(params) - {"from", "to", "consultationTypeId"})
    if unknown:
        raise APIError(400, "Unsupported query parameters", {"parameters": unknown})
    type_filter = integer(params.get("consultationTypeId"), "consultationTypeId", minimum=1)

    with db.connection().cursor() as cur:
        cur.execute(
            """SELECT d.timezone FROM doctors d JOIN users u ON u.user_id = d.user_id
               WHERE d.doctor_id = %s AND u.is_active = 1""",
            (doctor_id,),
        )
        doctor = cur.fetchone()
        if not doctor:
            raise APIError(404, "Doctor not found")
        cur.execute(
            """SELECT consultation_type_id, name, fee, duration_minutes
               FROM consultation_types WHERE doctor_id = %s AND is_active = 1""",
            (doctor_id,),
        )
        types = {row["consultation_type_id"]: row for row in cur.fetchall()}
    if type_filter is not None and type_filter not in types:
        raise APIError(404, "Consultation type not found for this doctor")

    tz_name = doctor["timezone"]
    now = slots.now_utc()
    today = slots.local_today(tz_name, now)
    first = iso_date(params.get("from"), "from", required=False) or today
    last = iso_date(params.get("to"), "to", required=False) or first + timedelta(days=6)
    if last < first:
        raise APIError(400, "to must not be before from")
    max_days = config.config_int("slots/max-lookup-days")
    if (last - first).days + 1 > max_days:
        raise APIError(400, f"The range can cover at most {max_days} days")

    start_utc, end_utc = slots.doctor_local_range(tz_name, first, (last - first).days + 1)
    start_iso = max(slots.iso_utc(start_utc), slots.iso_utc(now))
    items = slots.query_doctor_slots(config.table("SLOTS_TABLE"), doctor_id, start_iso, slots.iso_utc(end_utc))

    result = []
    for item in sorted(items, key=lambda i: i["startTime"]):
        type_id = int(item["consultationTypeId"])
        if item.get("status") != "OPEN" or item["startTime"] <= slots.iso_utc(now) or type_id not in types:
            continue
        if type_filter is not None and type_id != type_filter:
            continue
        entry = slots.slot_view(item)
        entry["consultation_type_name"] = types[type_id]["name"]
        entry["fee"] = types[type_id]["fee"]
        result.append(entry)
    return 200, {"doctor_id": doctor_id, "timezone": tz_name, "from": first, "to": last, "slots": result}


lambda_handler = api_handler({"GET /doctors/{doctorId}/slots": lookup})
