"""Domain events on the EventBridge bus medislot-<env>-events (architecture box 8).

Every event has the same envelope, which EventBridge carries as the event detail:

    {"eventId", "eventType", "occurredAt", "correlationId", "data": {...}}

Source is "medislot" and the detail-type is the event type, so rules can match
on either. Milestone 5 only publishes; the consumers (notifications, the
waitlist processor) arrive in Milestone 6.

publish() never fails the request that calls it: the change it reports is
already committed in the database. A failed publish is logged as an error with
the full event and counted in the EventPublishFailures metric, so it can be
found and sent again.
"""

import json
import os
import uuid
from datetime import UTC, datetime

from . import config, logs

SOURCE = "medislot"
BOOKED = "AppointmentBooked"
CANCELLED = "AppointmentCancelled"
COMPLETED = "AppointmentCompleted"
NO_SHOW = "AppointmentNoShow"
SEAT_FREED = "SlotSeatFreed"
SLOT_CANCELLED = "SlotCancelled"


def _default(value):
    from .http import json_default

    return json_default(value)


def envelope(event_type, data):
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "occurredAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "correlationId": logs.correlation_id(),
        "data": data,
    }


def publish(*events):
    """Put (event_type, data) pairs on the bus. Returns the envelopes that were sent."""
    if not events:
        return []
    envelopes = [envelope(event_type, data) for event_type, data in events]
    bus = os.environ.get("EVENT_BUS_NAME")
    if not bus:
        for item in envelopes:
            logs.metric("EventPublishFailures", EventType=item["eventType"])
            logs.error("event not published: EVENT_BUS_NAME is not set", eventType=item["eventType"], event=item)
        return []
    entries = [
        {
            "Source": SOURCE,
            "DetailType": item["eventType"],
            "Detail": json.dumps(item, default=_default),
            "EventBusName": bus,
        }
        for item in envelopes
    ]
    sent = []
    for start in range(0, len(entries), 10):  # PutEvents takes at most 10 entries
        batch, batch_envelopes = entries[start:start + 10], envelopes[start:start + 10]
        try:
            result = config.client("events").put_events(Entries=batch)
            outcomes = result.get("Entries", [])
        except Exception as exc:  # noqa: BLE001 - the change is committed; report, do not fail the caller
            outcomes = [{"ErrorCode": type(exc).__name__, "ErrorMessage": str(exc)}] * len(batch)
        for item, outcome in zip(batch_envelopes, outcomes, strict=False):
            if outcome.get("ErrorCode"):
                logs.metric("EventPublishFailures", EventType=item["eventType"])
                logs.error("event not published", eventType=item["eventType"], eventId=item["eventId"],
                           error=outcome.get("ErrorMessage"), event=item)
            else:
                sent.append(item)
                logs.info("event published", eventType=item["eventType"], eventId=item["eventId"])
    return sent
