"""Reminder job (architecture box 7).

Each booking creates one EventBridge Scheduler schedule per reminder offset
(CONFIG_REMINDERS_OFFSETS_HOURS, "24,2"); each invokes this function once with

    {"appointmentId": "...", "hoursBefore": 24}

and deletes itself afterwards. The reminder goes to the patient only if the
appointment is still CONFIRMED and has not started; a cancellation deletes the
schedules, and this check covers a schedule that fired anyway. Sent once per
appointment and offset: dispatch key <appointmentId>#reminder-<h>h#<userId>.
"""

from datetime import UTC, datetime

from medislot_common import appointments, db, logs, notify


def lambda_handler(event, context):
    appointment_id = str(event.get("appointmentId") or "")
    hours = int(event.get("hoursBefore") or 0)
    logs.set_correlation_id(f"reminder-{appointment_id}-{hours}h", getattr(context, "aws_request_id", None))
    logs.set_context(appointmentId=appointment_id, hoursBefore=hours)
    with db.connection().cursor() as cur:
        row = appointments.load(cur, appointment_id)
    if not row:
        logs.warning("reminder for an unknown appointment")
        return {"sent": False, "reason": "not found"}
    if row["status"] != "CONFIRMED":
        logs.info("reminder not needed", status=row["status"])
        return {"sent": False, "reason": row["status"]}
    if appointments.aware(row["slot_start_utc"]) <= datetime.now(UTC):
        logs.info("reminder too late; the appointment has started")
        return {"sent": False, "reason": "started"}

    detail = appointments.event_detail(row)
    where = ", ".join(part for part in (row.get("hospital_name"), row.get("hospital_city")) if part)
    text = (
        f"Reminder: your {detail['consultationType'].lower()} with Dr. {detail['doctorName']} starts in about "
        f"{hours} hour{'s' if hours != 1 else ''}, at {detail['localStart']} ({detail['timezone']})."
        + (f"\nWhere: {where}." if where else "")
        + "\n\nMediSlot"
    )
    sent = notify.send(f"{appointment_id}#reminder-{hours}h#{detail['patientUserId']}", detail["patientUserId"],
                       f"MediSlot: appointment in {hours} hour{'s' if hours != 1 else ''}", text,
                       "AppointmentReminder", appointment_id)
    logs.metric("RemindersSent" if sent else "RemindersAlreadySent", HoursBefore=str(hours))
    return {"sent": sent}
