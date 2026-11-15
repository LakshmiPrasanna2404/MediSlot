"""Notification Lambda (architecture box 8).

Invoked by the EventBridge rule on the medislot-<env>-events bus for the
domain events below. Works out who should hear about the event and sends each
of them one email through the SNS topic (medislot_common.notify), exactly once:
the dispatch key is <appointmentId or slotId>#<eventType>#<recipient userId>,
so an event delivered twice, or published twice by a retried step, sends one
message.

    AppointmentBooked                 patient and doctor
    AppointmentCancelled (patient)    patient and doctor
    AppointmentCancelled (doctor)     patient
    AppointmentCompleted              patient
    AppointmentNoShow                 patient
    SlotCancelled                     doctor

Needs no database: every event carries the ids and the names and times the
message text uses, so this function runs outside the VPC.

A failure raises, so EventBridge retries the event (rule retry policy) and the
Lambda's own async retries apply; a recipient already sent to is skipped.
"""

from medislot_common import logs, notify

SIGN_OFF = "\n\nMediSlot"


def _money(value):
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _when(detail):
    zone = detail.get("timezone")
    return f"{detail.get('localStart')} ({zone})" if zone else str(detail.get("localStart") or detail.get("startUtc"))


def _booked(detail):
    when, kind = _when(detail), detail.get("consultationType") or "Consultation"
    return [
        (detail.get("patientUserId"), "Appointment confirmed",
         f"Your {kind.lower()} with Dr. {detail.get('doctorName')} is booked for {when}.\n"
         f"Fee: {_money(detail.get('fee'))}. You will get reminders before it starts."),
        (detail.get("doctorUserId"), "New appointment booked",
         f"{detail.get('patientName')} booked a {kind.lower()} with you for {when}."),
    ]


def _cancelled(detail):
    when = _when(detail)
    refund = (f"Refund: {_money(detail.get('refundAmount'))} ({detail.get('refundPercent')}% of "
              f"{_money(detail.get('fee'))}).")
    if detail.get("cancelledBy") == "doctor":
        reason = f"\nReason: {detail['reason']}" if detail.get("reason") else ""
        return [
            (detail.get("patientUserId"), "Appointment cancelled by the doctor",
             f"Dr. {detail.get('doctorName')} cancelled your appointment on {when}.{reason}\n{refund}"),
        ]
    return [
        (detail.get("patientUserId"), "Appointment cancelled",
         f"Your appointment with Dr. {detail.get('doctorName')} on {when} is cancelled.\n{refund}"),
        (detail.get("doctorUserId"), "Appointment cancelled by the patient",
         f"{detail.get('patientName')} cancelled the appointment on {when}. The seat is open again."),
    ]


def _completed(detail):
    return [
        (detail.get("patientUserId"), "Appointment completed",
         f"Your appointment with Dr. {detail.get('doctorName')} on {_when(detail)} is marked as completed."),
    ]


def _no_show(detail):
    return [
        (detail.get("patientUserId"), "Missed appointment",
         f"You were marked as not attending your appointment with Dr. {detail.get('doctorName')} on "
         f"{_when(detail)}. Missed appointments are not refunded."),
    ]


def _slot_cancelled(detail):
    count = int(detail.get("appointmentsCancelled") or 0)
    patients = "No patient had booked it." if count == 0 else (
        f"{count} booked patient{'s' if count != 1 else ''} {'were' if count != 1 else 'was'} "
        "notified and refunded in full.")
    return [
        (detail.get("doctorUserId"), "Slot cancelled",
         f"Your slot on {_when(detail)} is cancelled. {patients}"),
    ]


MESSAGES = {
    "AppointmentBooked": _booked,
    "AppointmentCancelled": _cancelled,
    "AppointmentCompleted": _completed,
    "AppointmentNoShow": _no_show,
    "SlotCancelled": _slot_cancelled,
}


def lambda_handler(event, context):
    envelope = event.get("detail") or {}  # EventBridge detail = the MediSlot envelope
    event_type = envelope.get("eventType") or event.get("detail-type")
    detail = envelope.get("data") or {}
    logs.set_correlation_id(envelope.get("correlationId") or getattr(context, "aws_request_id", None),
                            getattr(context, "aws_request_id", None))
    logs.set_context(eventType=event_type, eventId=envelope.get("eventId"))
    build = MESSAGES.get(event_type)
    if build is None:
        logs.info("event needs no notification")
        return {"sent": 0, "skipped": 0}
    entity = detail.get("appointmentId") or detail.get("slotId")
    sent = skipped = 0
    for recipient, subject, text in build(detail):
        if not recipient:
            logs.warning("notification without a recipient", entityId=entity)
            continue
        if notify.send(f"{entity}#{event_type}#{recipient}", recipient, f"MediSlot: {subject}", text + SIGN_OFF,
                       event_type, entity):
            sent += 1
        else:
            skipped += 1
    logs.info("event handled", entityId=entity, sent=sent, alreadySent=skipped)
    return {"sent": sent, "skipped": skipped}
