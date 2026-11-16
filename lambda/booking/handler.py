"""Booking Lambda (patient role, architecture box 5).

    POST /appointments                  {"slot_id"} with an Idempotency-Key header
    GET  /appointments?status=upcoming|past|all
    GET  /appointments/{appointmentId}

Booking, in order:
1. Idempotency: the patient + Idempotency-Key is claimed in the idempotency
   table. A replay returns the first answer; a key reused for another body
   gets 422 (medislot_common.idempotency).
2. The slot is read (strongly consistent): it must exist, be OPEN and start in
   the future, and its consultation type must still be offered.
3. In one MySQL transaction, with the patient's row locked so two bookings by
   the same patient run one after the other:
   * at most CONFIG_BOOKING_MAX_UPCOMING_PER_DOCTOR (3) upcoming appointments
     with this doctor, counted from appointments;
   * the seat is claimed in DynamoDB (one conditional transaction on the slot's
     version and seat count plus the seat item; conflicts retried with backoff);
   * the appointment row is inserted (CONFIRMED, the fee at booking time).
   If anything after the seat claim fails, the seat is given back.
4. After commit: AppointmentBooked is published (a failed publish never undoes
   the booking).
"""

import uuid
from datetime import UTC, datetime

import pymysql

from medislot_common import appointments, config, db, events, idempotency, logs, profiles, refunds, slots
from medislot_common.http import APIError, api_handler, header, identity, parse_body, path_param, query
from medislot_common.validation import uuid_value

LIST_FILTERS = ("upcoming", "past", "all")


def _book(caller, patient, slot_id, idempotency_key):
    try:
        key = slots.slot_key(slot_id)
    except ValueError:
        raise APIError(400, "slot_id is not a valid slot") from None
    now = datetime.now(UTC)
    item = config.table(appointments.SLOTS).get_item(Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise APIError(404, "Slot not found")
    if item.get("status") != "OPEN":
        raise APIError(409, "This slot is no longer open")
    start = slots.parse_iso_utc(item["startTime"])
    end = slots.parse_iso_utc(item["endTime"])
    if start <= now:
        raise APIError(409, "This slot has already started")
    doctor_id = item["doctorId"]
    type_id = int(item["consultationTypeId"])
    appointment_id = str(uuid.uuid4())
    limit = config.config_int("booking/max-upcoming-per-doctor")

    claimed = False
    try:
        with db.transaction() as cur:
            # One booking at a time per patient: the limit below is then exact.
            cur.execute("SELECT patient_id FROM patients WHERE patient_id = %s FOR UPDATE", (patient["patient_id"],))
            cur.execute(
                """SELECT COUNT(*) AS n FROM appointments
                   WHERE patient_id = %s AND doctor_id = %s AND status = 'CONFIRMED' AND slot_start_utc > %s""",
                (patient["patient_id"], doctor_id, appointments.naive(now)),
            )
            if int(cur.fetchone()["n"]) >= limit:
                raise APIError(409, f"You can have at most {limit} upcoming appointments with the same doctor")
            cur.execute(
                """SELECT fee, is_active FROM doctor_consultation_types
                   WHERE doctor_id = %s AND consultation_type_id = %s""",
                (doctor_id, type_id),
            )
            offer = cur.fetchone()
            if not offer or not offer["is_active"]:
                raise APIError(409, "The doctor no longer offers this consultation type")

            appointments.claim_seat(slot_id, patient["patient_id"], appointment_id)
            claimed = True
            cur.execute(
                """INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id,
                                             slot_start_utc, slot_end_utc, consultation_fee, status, idempotency_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'CONFIRMED', %s)""",
                (appointment_id, patient["patient_id"], doctor_id, slot_id, type_id, appointments.naive(start),
                 appointments.naive(end), offer["fee"], idempotency_key),
            )
            row = appointments.load(cur, appointment_id)
    except pymysql.err.IntegrityError as exc:
        if claimed:
            _give_back(slot_id, patient["patient_id"])
        if "uq_patient_active_slot" in str(exc):
            raise APIError(409, "You already have an appointment in this slot") from None
        if "uq_patient_idempotency" in str(exc):
            # The same key booked before and its idempotency record has expired:
            # the appointments table still knows it, so return that booking.
            with db.connection().cursor() as cur:
                cur.execute(appointments.VIEW_SQL + " WHERE a.patient_id = %s AND a.idempotency_key = %s",
                            (patient["patient_id"], idempotency_key))
                earlier = cur.fetchone()
            if earlier:
                return 201, {"appointment": appointments.view(earlier)}
        raise
    except BaseException:
        if claimed:
            _give_back(slot_id, patient["patient_id"])
        raise

    logs.metric("AppointmentsBooked")
    logs.info("appointment booked", appointmentId=appointment_id, slotId=slot_id, doctorId=doctor_id,
              consultationTypeId=type_id, fee=row["consultation_fee"])
    events.publish((events.BOOKED, appointments.event_detail(row)))
    return 201, {"appointment": appointments.view(row)}


def _give_back(slot_id, patient_id):
    try:
        appointments.release_seat(slot_id, patient_id)
    except Exception:  # noqa: BLE001 - logged; the seat count is then one too high until fixed by hand
        logs.metric("SeatReleaseFailures")
        logs.exception("seat could not be given back after a failed booking", slotId=slot_id)


def book(event):
    caller = identity(event, "patient")
    body = parse_body(event)
    slot_id = body["slot_id"].strip()
    idempotency_key = header(event, "Idempotency-Key")
    with db.connection().cursor() as cur:
        patient = profiles.require_patient(cur, caller.user_id)

    claim, replay = idempotency.begin(patient["patient_id"], idempotency_key, {"slot_id": slot_id})
    if replay:
        return replay
    try:
        status, payload = _book(caller, patient, slot_id, idempotency_key)
    except APIError as exc:
        if exc.status >= 500 or (isinstance(exc.details, dict) and exc.details.get("retryable")):
            idempotency.forget(claim)
        else:
            body = {"error": exc.message}
            if exc.details is not None:
                body["details"] = exc.details
            idempotency.finish(claim, exc.status, body)
        raise
    except BaseException:
        idempotency.forget(claim)
        raise
    idempotency.finish(claim, status, payload)
    return status, payload


def list_appointments(event):
    caller = identity(event, "patient")
    params = query(event)
    which = params.get("status", "all")
    if which not in LIST_FILTERS:
        raise APIError(400, "status must be one of " + ", ".join(LIST_FILTERS))
    now = datetime.now(UTC)
    with db.connection().cursor() as cur:
        patient = profiles.require_patient(cur, caller.user_id)
        sql = appointments.VIEW_SQL + " WHERE a.patient_id = %s"
        values = [patient["patient_id"]]
        upcoming = "a.status = 'CONFIRMED' AND a.slot_start_utc > %s"
        if which == "upcoming":
            sql += f" AND {upcoming} ORDER BY a.slot_start_utc"
            values.append(appointments.naive(now))
        elif which == "past":
            sql += f" AND NOT ({upcoming}) ORDER BY a.slot_start_utc DESC"
            values.append(appointments.naive(now))
        else:
            sql += " ORDER BY a.slot_start_utc DESC"
        cur.execute(sql + " LIMIT 200", values)
        rows = cur.fetchall()
        cur.execute(
            """SELECT r.* FROM refund_ledger r WHERE r.patient_id = %s""",
            (patient["patient_id"],),
        )
        ledger = {row["appointment_id"]: row for row in cur.fetchall()}
    result = []
    for row in rows:
        entry = appointments.view(row)
        entry["refund"] = refunds.view(ledger.get(row["appointment_id"]))
        start = appointments.aware(row["slot_start_utc"])
        entry["can_cancel"] = row["status"] == "CONFIRMED" and start > now
        if entry["can_cancel"]:
            # What a cancellation now would refund, so the patient sees it before choosing.
            entry["refund_if_cancelled_now"] = refunds.for_patient_cancel(row["consultation_fee"], start, now)
        result.append(entry)
    logs.info("appointments listed", filter=which, count=len(result))
    return 200, {"appointments": result}


def get_appointment(event):
    caller = identity(event, "patient")
    appointment_id = uuid_value(path_param(event, "appointmentId"), "appointmentId")
    with db.connection().cursor() as cur:
        patient = profiles.require_patient(cur, caller.user_id)
        row = appointments.load(cur, appointment_id)
        if not row or row["patient_id"] != patient["patient_id"]:
            raise APIError(404, "Appointment not found")  # another patient's id looks the same as a missing one
        cur.execute("SELECT * FROM refund_ledger WHERE appointment_id = %s", (appointment_id,))
        refund = cur.fetchone()
    entry = appointments.view(row)
    entry["refund"] = refunds.view(refund)
    return 200, {"appointment": entry}


lambda_handler = api_handler(
    {
        "POST /appointments": book,
        "GET /appointments": list_appointments,
        "GET /appointments/{appointmentId}": get_appointment,
    }
)
