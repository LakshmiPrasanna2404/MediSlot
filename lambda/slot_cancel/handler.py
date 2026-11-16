"""Slot Cancel Lambda (doctor role, architecture box 5) and the steps of its workflow.

    POST /doctors/me/slots/{slotId}/cancel   {"reason": "..."} (optional)

The API call closes the slot and hands the bookings to the Step Functions
state machine medislot-<env>-slot-cancel, which calls this same function back
for each step (the event then carries "workflowStep" instead of a route):

    1. API          slot item OPEN -> CANCELLED (doctor-slots, conditional), so
                    nobody can book it any more; start the workflow; 202.
    2. list         the slot's CONFIRMED appointments (RDS).
    3. Map          for each one, at most 5 at a time, cancelAppointment:
                    CANCELLED_BY_DOCTOR, 100% refund in refund_ledger, seat
                    given back, AppointmentCancelled published.
    4. list again   catches a booking that was committing while the slot
                    closed; the loop ends when the list is empty.
    5. finish       SlotCancelled published.

Every step can run twice without doing anything twice: the status changes are
conditional, the ledger has one row per appointment, and the seat delete is
conditional.
"""

import json
import os
import re
from datetime import UTC, datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from medislot_common import appointments, config, db, events, logs, profiles, refunds, slots
from medislot_common.http import APIError, api_handler, identity, parse_body, path_param

STATE_MACHINE = "SLOT_CANCEL_STATE_MACHINE_ARN"
FINISHED_BADLY = ("FAILED", "TIMED_OUT", "ABORTED")


# ---------------------------------------------------------------------------
# API: POST /doctors/me/slots/{slotId}/cancel
# ---------------------------------------------------------------------------


def _execution_name(slot_id):
    doctor_id, _, start = slot_id.partition("#")
    return f"{doctor_id}-{re.sub(r'[^0-9]', '', start)}"


def _start_workflow(slot_id, doctor_id, reason):
    """Start the workflow once per slot. A finished-badly run is started again under a new name."""
    client = config.client("stepfunctions")
    arn = os.environ[STATE_MACHINE]
    name = _execution_name(slot_id)
    payload = json.dumps({"slotId": slot_id, "doctorId": doctor_id, "reason": reason,
                          "correlationId": logs.correlation_id(), "list": {"round": 0}})
    try:
        return client.start_execution(stateMachineArn=arn, name=name, input=payload)["executionArn"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
    existing = arn.replace(":stateMachine:", ":execution:") + f":{name}"
    status = client.describe_execution(executionArn=existing)["status"]
    if status not in FINISHED_BADLY:
        logs.info("slot cancel workflow already started", execution=name, status=status)
        return existing
    retry_name = f"{name}-{int(datetime.now(UTC).timestamp())}"
    logs.warning("slot cancel workflow restarted", previous=name, previousStatus=status, execution=retry_name)
    return client.start_execution(stateMachineArn=arn, name=retry_name, input=payload)["executionArn"]


def cancel_slot(event):
    caller = identity(event, "doctor")
    slot_id = unquote(path_param(event, "slotId")).strip()
    reason = (parse_body(event).get("reason") or "").strip() or None
    try:
        key = slots.slot_key(slot_id)
    except ValueError:
        raise APIError(400, "slotId is not a valid slot") from None
    with db.connection().cursor() as cur:
        doctor = profiles.require_doctor(cur, caller.user_id)
    if key["doctorId"] != doctor["doctor_id"]:
        raise APIError(404, "Slot not found")  # another doctor's slot looks the same as a missing one

    table = config.table(appointments.SLOTS)
    item = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise APIError(404, "Slot not found")
    if slots.parse_iso_utc(item["startTime"]) <= datetime.now(UTC):
        raise APIError(409, "This slot has already started and can no longer be cancelled")
    if item.get("status") == "OPEN":
        try:
            item = table.update_item(
                Key=key,
                UpdateExpression="SET #status = :cancelled, version = version + :one, cancelledAt = :now"
                                 + (", cancelReason = :reason" if reason else ""),
                ConditionExpression="#status = :open",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":cancelled": "CANCELLED", ":open": "OPEN", ":one": 1,
                                           ":now": slots.iso_utc(datetime.now(UTC)),
                                           **({":reason": reason} if reason else {})},
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            item = table.get_item(Key=key, ConsistentRead=True).get("Item") or item
    if item.get("status") != "CANCELLED":
        raise APIError(409, f"This slot is {str(item.get('status', 'unknown')).lower()} and cannot be cancelled")

    with db.connection().cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE slot_id = %s AND status = 'CONFIRMED'", (slot_id,))
        affected = int(cur.fetchone()["n"])
    try:
        execution = _start_workflow(slot_id, doctor["doctor_id"], reason)
    except Exception:  # noqa: BLE001 - the slot is closed already; the doctor can press cancel again
        logs.metric("SlotCancelStartFailures")
        logs.exception("slot cancel workflow could not start", slotId=slot_id)
        raise APIError(503, "The slot is closed, but cancelling its bookings could not start yet; please try again") from None

    logs.metric("SlotsCancelled")
    logs.info("slot cancelled", slotId=slot_id, appointmentsToCancel=affected, execution=execution.rsplit(":", 1)[-1])
    view = slots.slot_view(item)
    view["status"] = "CANCELLED"
    return 202, {"slot": view, "appointments_to_cancel": affected}


api = api_handler({"POST /doctors/me/slots/{slotId}/cancel": cancel_slot})


# ---------------------------------------------------------------------------
# Workflow steps (called by Step Functions)
# ---------------------------------------------------------------------------


def list_appointments(event):
    with db.connection().cursor() as cur:
        cur.execute(
            "SELECT appointment_id FROM appointments WHERE slot_id = %s AND status = 'CONFIRMED' ORDER BY created_at",
            (event["slotId"],),
        )
        ids = [row["appointment_id"] for row in cur.fetchall()]
    round_number = int(event.get("round") or 0) + 1
    logs.info("slot cancel: appointments to cancel", slotId=event["slotId"], count=len(ids), round=round_number)
    return {"appointmentIds": ids, "count": len(ids), "round": round_number}


def cancel_appointment(event):
    appointment_id = event["appointmentId"]
    with db.transaction() as cur:
        row = appointments.load(cur, appointment_id, lock=True)
        if not row:
            logs.warning("slot cancel: appointment not found", appointmentId=appointment_id)
            return {"appointmentId": appointment_id, "result": "missing"}
        if row["status"] not in ("CONFIRMED", "CANCELLED_BY_DOCTOR"):
            # The patient cancelled (or it was marked) in the meantime: their outcome stands.
            logs.info("slot cancel: appointment left as it is", appointmentId=appointment_id, status=row["status"])
            return {"appointmentId": appointment_id, "result": "skipped", "status": row["status"]}
        if row["status"] == "CONFIRMED":
            cur.execute(
                """UPDATE appointments SET status = 'CANCELLED_BY_DOCTOR', version = version + 1
                   WHERE appointment_id = %s AND status = 'CONFIRMED'""",
                (appointment_id,),
            )
        ledger = refunds.record(cur, appointment_id, row["patient_id"], refunds.for_doctor_cancel(row["consultation_fee"]))
        row = appointments.load(cur, appointment_id)

    # A retried step repeats these; each is safe to repeat.
    appointments.release_seat(row["slot_id"], row["patient_id"])
    events.publish((events.CANCELLED, appointments.event_detail(
        row, cancelledBy="doctor", reason=event.get("reason"),
        refundPercent=ledger["refund_percent"], refundAmount=ledger["refund_amount"])))
    logs.metric("AppointmentsCancelled", CancelledBy="doctor")
    logs.info("slot cancel: appointment cancelled", appointmentId=appointment_id, refundAmount=ledger["refund_amount"])
    return {"appointmentId": appointment_id, "result": "cancelled"}


def finish(event):
    slot_id = event["slotId"]
    item = config.table(appointments.SLOTS).get_item(Key=slots.slot_key(slot_id), ConsistentRead=True).get("Item") or {}
    with db.connection().cursor() as cur:
        cur.execute(
            """SELECT d.user_id, d.timezone, u.first_name, u.last_name FROM doctors d
               JOIN users u ON u.user_id = d.user_id WHERE d.doctor_id = %s""",
            (event["doctorId"],),
        )
        doctor = cur.fetchone() or {}
        cur.execute(
            "SELECT COUNT(*) AS n FROM appointments WHERE slot_id = %s AND status = 'CANCELLED_BY_DOCTOR'",
            (slot_id,),
        )
        cancelled = int(cur.fetchone()["n"])
    start = slots.parse_iso_utc(item["startTime"]) if item.get("startTime") else None
    local_start = None
    if start and doctor.get("timezone"):
        local_start = start.astimezone(ZoneInfo(doctor["timezone"])).strftime("%a %d %b %Y, %H:%M")
    events.publish((events.SLOT_CANCELLED, {
        "slotId": slot_id,
        "doctorId": event["doctorId"],
        "doctorUserId": doctor.get("user_id"),
        "doctorName": " ".join(part for part in (doctor.get("first_name"), doctor.get("last_name")) if part),
        "startUtc": item.get("startTime"),
        "localStart": local_start,
        "timezone": doctor.get("timezone"),
        "slotStatus": item.get("status"),
        "reason": event.get("reason"),
        "appointmentsCancelled": cancelled,
    }))
    logs.info("slot cancel finished", slotId=slot_id, appointmentsCancelled=cancelled, rounds=event.get("rounds"))
    return {"slotId": slot_id, "appointmentsCancelled": cancelled}


STEPS = {"listAppointments": list_appointments, "cancelAppointment": cancel_appointment, "finish": finish}


def workflow(event, context):
    logs.set_correlation_id(event.get("correlationId") or getattr(context, "aws_request_id", None),
                            getattr(context, "aws_request_id", None))
    step = event["workflowStep"]
    logs.set_context(workflowStep=step, slotId=event.get("slotId"))
    try:
        return STEPS[step](event)
    except Exception:
        logs.exception("slot cancel step failed")  # Step Functions retries the step
        raise


def lambda_handler(event, context):
    if isinstance(event, dict) and "workflowStep" in event:
        return workflow(event, context)
    return api(event, context)
