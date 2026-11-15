"""No-show job (architecture box 7).

Each booking creates one EventBridge Scheduler schedule at the slot end plus
CONFIG_NO_SHOW_GRACE_MINUTES (30), which invokes this function once with

    {"appointmentId": "..."}

If the doctor has not recorded an outcome by then (the appointment is still
CONFIRMED), it becomes NO_SHOW with a 0% refund_ledger row, and
AppointmentNoShow is published. Anything else (completed, cancelled, already
marked) is left as it is, so a late or repeated run changes nothing.
"""

from datetime import UTC, datetime, timedelta

from medislot_common import appointments, config, db, events, logs, refunds


def lambda_handler(event, context):
    appointment_id = str(event.get("appointmentId") or "")
    logs.set_correlation_id(f"no-show-{appointment_id}", getattr(context, "aws_request_id", None))
    logs.set_context(appointmentId=appointment_id)
    grace = timedelta(minutes=config.config_int("no-show/grace-minutes"))
    with db.transaction() as cur:
        row = appointments.load(cur, appointment_id, lock=True)
        if not row:
            logs.warning("no-show check for an unknown appointment")
            return {"marked": False, "reason": "not found"}
        if row["status"] != "CONFIRMED":
            logs.info("no-show check: outcome already recorded", status=row["status"])
            return {"marked": False, "reason": row["status"]}
        if appointments.aware(row["slot_end_utc"]) + grace - timedelta(minutes=1) > datetime.now(UTC):
            logs.warning("no-show check ran early; left for the doctor")
            return {"marked": False, "reason": "early"}
        cur.execute(
            """UPDATE appointments SET status = 'NO_SHOW', version = version + 1
               WHERE appointment_id = %s AND status = 'CONFIRMED'""",
            (appointment_id,),
        )
        refunds.record(cur, appointment_id, row["patient_id"], refunds.for_no_show(row["consultation_fee"]))
        row = appointments.load(cur, appointment_id)
    logs.metric("OutcomesRecorded", Outcome="NO_SHOW")
    logs.info("appointment outcome recorded", outcome="NO_SHOW", markedBy="no-show job")
    events.publish((events.NO_SHOW, appointments.event_detail(row, markedBy="system")))
    return {"marked": True}
