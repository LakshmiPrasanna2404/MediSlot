"""Outcome Lambda (doctor role, architecture box 5).

    POST /doctors/me/appointments/{appointmentId}/outcome   {"outcome": "COMPLETED" | "NO_SHOW"}

The doctor marks how an appointment went, once it has started. Only the
doctor's own CONFIRMED appointments can be marked, and only once (the status
change is conditional). NO_SHOW writes a refund_ledger row with 0% refund.
Publishes AppointmentCompleted or AppointmentNoShow.
"""

from datetime import UTC, datetime

from medislot_common import appointments, db, events, logs, profiles, refunds
from medislot_common.http import APIError, api_handler, identity, parse_body, path_param
from medislot_common.validation import uuid_value

EVENT_FOR = {"COMPLETED": events.COMPLETED, "NO_SHOW": events.NO_SHOW}


def mark(event):
    caller = identity(event, "doctor")
    appointment_id = uuid_value(path_param(event, "appointmentId"), "appointmentId")
    outcome = parse_body(event)["outcome"]
    now = datetime.now(UTC)
    with db.transaction() as cur:
        doctor = profiles.require_doctor(cur, caller.user_id)
        row = appointments.load(cur, appointment_id, lock=True)
        if not row or row["doctor_id"] != doctor["doctor_id"]:
            raise APIError(404, "Appointment not found")
        if row["status"] != "CONFIRMED":
            raise APIError(409, f"This appointment is already {row['status'].replace('_', ' ').lower()}")
        if appointments.aware(row["slot_start_utc"]) > now:
            raise APIError(409, "An outcome can be recorded once the appointment has started")
        cur.execute(
            "UPDATE appointments SET status = %s, version = version + 1 WHERE appointment_id = %s AND status = 'CONFIRMED'",
            (outcome, appointment_id),
        )
        if cur.rowcount != 1:
            raise APIError(409, "This appointment was changed at the same time; reload and try again")
        ledger = None
        if outcome == "NO_SHOW":
            ledger = refunds.record(cur, appointment_id, row["patient_id"], refunds.for_no_show(row["consultation_fee"]))
        row = appointments.load(cur, appointment_id)

    logs.metric("OutcomesRecorded", Outcome=outcome)
    logs.info("appointment outcome recorded", appointmentId=appointment_id, outcome=outcome, markedBy="doctor")
    events.publish((EVENT_FOR[outcome], appointments.event_detail(row, markedBy="doctor")))
    return 200, {"appointment": appointments.view(row), "refund": refunds.view(ledger)}


lambda_handler = api_handler({"POST /doctors/me/appointments/{appointmentId}/outcome": mark})
