"""Cancel Lambda (patient role, architecture box 5).

    DELETE /appointments/{appointmentId}

1. In one MySQL transaction, with the appointment row locked: it must be the
   caller's, CONFIRMED, and not yet started. The refund tier comes from the
   time left before the start (medislot_common.refunds); the status becomes
   CANCELLED_BY_PATIENT and one refund_ledger row is written.
2. After commit: the seat is given back in DynamoDB (seat deleted, bookedCount
   - 1, in one transaction), the reminder and no-show schedules are deleted,
   and AppointmentCancelled and SlotSeatFreed are published.

A second cancel of the same appointment gets 409: the status change is
conditional and the ledger has one row per appointment, so nothing is
refunded twice.
"""

from datetime import UTC, datetime

from medislot_common import appointments, db, events, logs, profiles, refunds
from medislot_common.http import APIError, api_handler, identity, path_param
from medislot_common.validation import uuid_value

LABELS = {
    "COMPLETED": "completed",
    "CANCELLED_BY_PATIENT": "already cancelled",
    "CANCELLED_BY_DOCTOR": "already cancelled by the doctor",
    "NO_SHOW": "marked as a no-show",
}


def cancel(event):
    caller = identity(event, "patient")
    appointment_id = uuid_value(path_param(event, "appointmentId"), "appointmentId")
    now = datetime.now(UTC)
    with db.transaction() as cur:
        patient = profiles.require_patient(cur, caller.user_id)
        row = appointments.load(cur, appointment_id, lock=True)
        if not row or row["patient_id"] != patient["patient_id"]:
            raise APIError(404, "Appointment not found")
        if row["status"] != "CONFIRMED":
            raise APIError(409, f"This appointment is {LABELS.get(row['status'], row['status'].lower())}")
        start = appointments.aware(row["slot_start_utc"])
        if start <= now:
            raise APIError(409, "This appointment has already started and can no longer be cancelled")
        refund = refunds.for_patient_cancel(row["consultation_fee"], start, now)
        cur.execute(
            """UPDATE appointments SET status = 'CANCELLED_BY_PATIENT', version = version + 1
               WHERE appointment_id = %s AND status = 'CONFIRMED'""",
            (appointment_id,),
        )
        if cur.rowcount != 1:
            raise APIError(409, "This appointment was changed at the same time; reload and try again")
        ledger = refunds.record(cur, appointment_id, row["patient_id"], refund)
        row = appointments.load(cur, appointment_id)

    hours_before = round((start - now).total_seconds() / 3600, 2)
    logs.metric("AppointmentsCancelled", CancelledBy="patient")
    logs.info("appointment cancelled", appointmentId=appointment_id, slotId=row["slot_id"], hoursBefore=hours_before,
              refundPercent=refund["refund_percent"], refundAmount=refund["refund_amount"], tier=refund["tier"])

    released = appointments.release_seat(row["slot_id"], row["patient_id"])
    appointments.delete_jobs(appointment_id)
    events.publish(
        (events.CANCELLED, appointments.event_detail(
            row, cancelledBy="patient", refundPercent=refund["refund_percent"], refundAmount=refund["refund_amount"])),
        (events.SEAT_FREED, {"slotId": row["slot_id"], "doctorId": row["doctor_id"], "seatsFreed": 1 if released else 0}),
    )
    return 200, {"appointment": appointments.view(row), "refund": refunds.view(ledger)}


lambda_handler = api_handler({"DELETE /appointments/{appointmentId}": cancel})
