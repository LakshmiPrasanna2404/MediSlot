"""Appointments: seats in DynamoDB, rows in RDS.

The contended resource is the slot item in doctor-slots (capacity, bookedCount,
heldCount, version, status). A seat is free when status is OPEN and
bookedCount + heldCount < capacity.

claim_seat (booking) is one DynamoDB transaction:
    Update the slot   only if status = OPEN and version = the version just read
                      (so the free-seat count read a moment ago is still true);
                      bookedCount + 1, version + 1
    Put the seat      seats item (slotId, patientId), only if it does not exist
A version conflict (someone else booked in between) is retried after reading
the slot again, with exponential backoff and jitter, up to
CONFIG_BOOKING_RETRY_MAX_ATTEMPTS times. No lock and no queue: concurrent
bookings can never push bookedCount past capacity.

release_seat (cancel) is the reverse transaction: delete the seat (only if it
exists) and give the place back (bookedCount - 1, version + 1). Because the seat
delete is conditional, running it twice gives the place back once.
"""

import secrets
import time
from datetime import UTC
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

from . import config, logs, slots
from .http import APIError

SLOTS = "SLOTS_TABLE"
SEATS = "SEATS_TABLE"
_random = secrets.SystemRandom()

VIEW_SQL = """
SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id, a.consultation_type_id, a.slot_start_utc,
       a.slot_end_utc, a.consultation_fee, a.status, a.reschedule_count, a.created_at, a.updated_at,
       ct.name AS consultation_type_name, ct.duration_minutes, d.timezone,
       du.user_id AS doctor_user_id, du.first_name AS doctor_first_name, du.last_name AS doctor_last_name,
       h.name AS hospital_name, h.city AS hospital_city, s.name AS specialisation,
       pu.user_id AS patient_user_id, pu.first_name AS patient_first_name, pu.last_name AS patient_last_name
FROM appointments a
JOIN consultation_types ct ON ct.consultation_type_id = a.consultation_type_id
JOIN doctors d ON d.doctor_id = a.doctor_id
JOIN users du ON du.user_id = d.user_id
LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
LEFT JOIN specialisations s ON s.specialisation_id = d.specialisation_id
JOIN patients p ON p.patient_id = a.patient_id
JOIN users pu ON pu.user_id = p.user_id
"""


# ---------------------------------------------------------------------------
# Rows and views
# ---------------------------------------------------------------------------


def aware(value):
    """MySQL DATETIME values are UTC (session time zone +00:00) but arrive naive."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def naive(value):
    return value.astimezone(UTC).replace(tzinfo=None)


def load(cursor, appointment_id, lock=False):
    cursor.execute(VIEW_SQL + " WHERE a.appointment_id = %s" + (" FOR UPDATE OF a" if lock else ""), (appointment_id,))
    return cursor.fetchone()


def _name(row, prefix):
    return " ".join(part for part in (row.get(f"{prefix}first_name"), row.get(f"{prefix}last_name")) if part)


def view(row):
    zone = ZoneInfo(row["timezone"])
    start, end = aware(row["slot_start_utc"]), aware(row["slot_end_utc"])
    return {
        "appointment_id": row["appointment_id"],
        "slot_id": row["slot_id"],
        "status": row["status"],
        "consultation_type_id": row["consultation_type_id"],
        "consultation_type_name": row["consultation_type_name"],
        "duration_minutes": row.get("duration_minutes"),
        "consultation_fee": row["consultation_fee"],
        "start_utc": slots.iso_utc(start),
        "end_utc": slots.iso_utc(end),
        "local_start": start.astimezone(zone).isoformat(),
        "local_end": end.astimezone(zone).isoformat(),
        "timezone": row["timezone"],
        "reschedule_count": row.get("reschedule_count", 0),
        "created_at": row.get("created_at"),
        "doctor": {
            "doctor_id": row["doctor_id"],
            "first_name": row["doctor_first_name"],
            "last_name": row["doctor_last_name"],
            "specialisation": row.get("specialisation"),
            "hospital_name": row.get("hospital_name"),
            "city": row.get("hospital_city"),
        },
        "patient": {
            "patient_id": row["patient_id"],
            "first_name": row["patient_first_name"],
            "last_name": row["patient_last_name"],
        },
    }


def event_detail(row, **extra):
    """What events carry: ids for routing, and names and times for the message text."""
    start = aware(row["slot_start_utc"])
    zone = ZoneInfo(row["timezone"])
    return {
        "appointmentId": row["appointment_id"],
        "slotId": row["slot_id"],
        "doctorId": row["doctor_id"],
        "patientId": row["patient_id"],
        "doctorUserId": row["doctor_user_id"],
        "patientUserId": row["patient_user_id"],
        "doctorName": _name(row, "doctor_"),
        "patientName": _name(row, "patient_"),
        "consultationType": row["consultation_type_name"],
        "startUtc": slots.iso_utc(start),
        "localStart": start.astimezone(zone).strftime("%a %d %b %Y, %H:%M"),
        "timezone": row["timezone"],
        "fee": row["consultation_fee"],
        **extra,
    }


# ---------------------------------------------------------------------------
# Seats (DynamoDB)
# ---------------------------------------------------------------------------


def _reasons(exc):
    return [reason.get("Code") for reason in exc.response.get("CancellationReasons", [])]


def claim_seat(slot_id, patient_id, appointment_id):
    """Take one seat in the slot for the patient, or raise APIError(409). Returns the slot item read last."""
    key = slots.slot_key(slot_id)
    slot_table = config.table(SLOTS)
    seat_table = config.table(SEATS)
    client = slot_table.meta.client
    attempts = config.config_int("booking/retry-max-attempts")
    base_ms = config.config_int("booking/retry-base-ms")
    for attempt in range(1, attempts + 1):
        item = slot_table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not item:
            raise APIError(404, "Slot not found")
        if item.get("status") != "OPEN":
            raise APIError(409, "This slot is no longer open")
        if slots.free_seats(item) <= 0:
            raise APIError(409, "This slot is full")
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": slot_table.name,
                            "Key": key,
                            "UpdateExpression": "SET bookedCount = bookedCount + :one, version = version + :one",
                            "ConditionExpression": "#status = :open AND version = :version",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {":one": 1, ":open": "OPEN", ":version": item["version"]},
                        }
                    },
                    {
                        "Put": {
                            "TableName": seat_table.name,
                            "Item": {
                                "slotId": slot_id,
                                "patientId": patient_id,
                                "appointmentId": appointment_id,
                                "status": "BOOKED",
                                "createdAt": int(time.time()),
                            },
                            "ConditionExpression": "attribute_not_exists(patientId)",
                        }
                    },
                ]
            )
            logs.info("seat claimed", slotId=slot_id, attempt=attempt, bookedBefore=int(item.get("bookedCount", 0)),
                      capacity=int(item.get("capacity", 0)))
            return item
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("TransactionCanceledException",
                                                                 "TransactionConflictException"):
                raise
            reasons = _reasons(exc)
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                raise APIError(409, "You already have a seat in this slot") from None
            logs.metric("BookingConflictRetries")
            logs.info("seat claim conflict, retrying", slotId=slot_id, attempt=attempt, reasons=reasons)
            if attempt < attempts:
                time.sleep(_random.uniform(0, base_ms * (2 ** (attempt - 1))) / 1000)
    logs.warning("seat claim gave up after retries", slotId=slot_id, attempts=attempts)
    # retryable: the same Idempotency-Key may try again, so this answer is not stored as final.
    raise APIError(409, "This slot is busy right now; please try again", {"retryable": True})


def release_seat(slot_id, patient_id):
    """Give the patient's seat back. True if released now, False if it was already gone."""
    key = slots.slot_key(slot_id)
    slot_table = config.table(SLOTS)
    seat_table = config.table(SEATS)
    try:
        slot_table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": seat_table.name,
                        "Key": {"slotId": slot_id, "patientId": patient_id},
                        "ConditionExpression": "attribute_exists(patientId)",
                    }
                },
                {
                    "Update": {
                        "TableName": slot_table.name,
                        "Key": key,
                        "UpdateExpression": "SET bookedCount = bookedCount - :one, version = version + :one",
                        "ConditionExpression": "bookedCount > :zero",
                        "ExpressionAttributeValues": {":one": 1, ":zero": 0},
                    }
                },
            ]
        )
        logs.info("seat released", slotId=slot_id)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            logs.info("seat already released", slotId=slot_id, reasons=_reasons(exc))
            return False
        raise
