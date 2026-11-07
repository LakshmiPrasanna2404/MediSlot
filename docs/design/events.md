# Event catalogue and scheduled jobs

Box 8 of the architecture diagrams. One EventBridge bus, `medislot-<env>-events`. Rules route events **straight to
Lambda** (no SQS between them, a design decision for this project). Failures are handled by the EventBridge retry
policy and the Lambda on-failure destination; see below.

## Envelope

Every event has the same `detail` envelope:

```json
{
  "eventId": "uuid",
  "eventType": "AppointmentBooked",
  "occurredAt": "2026-11-09T04:31:12Z",
  "correlationId": "the request's correlation ID",
  "data": { "...": "event-specific fields" }
}
```

`source` = `medislot.<producer>`, `detail-type` = the event type.

## Events

| Event | Published by | `data` | Consumers |
|---|---|---|---|
| AppointmentBooked | Booking; offer acceptance | appointmentId, patientId, doctorId, slotId, startUtc | Notification (patient and doctor) |
| AppointmentRescheduled | Reschedule saga | appointmentId, patientId, doctorId, fromSlotId, toSlotId, startUtc | Notification (patient and doctor) |
| AppointmentCancelled | Cancel; slot-cancel workflow | appointmentId, patientId, doctorId, slotId, cancelledBy (patient or doctor), refundAmount, tier | Notification (patient and doctor if the patient cancelled; patient only if the doctor did) |
| SlotSeatFreed | Cancel; reschedule saga (old slot); offer expiry | slotId, doctorId | Waitlist Processor |
| SlotCancelled | Slot-cancel workflow | slotId, doctorId, affectedAppointments | Notification |
| WaitlistOfferIssued | Waitlist Processor | offerId, slotId, patientId, expiresAt | Notification (patient) |
| WaitlistOfferExpired | Offer expiry | offerId, slotId, patientId | Notification (patient) |
| AppointmentCompleted | Outcome | appointmentId, patientId, doctorId | (recorded, available to consumers) |
| AppointmentNoShow | Outcome; no-show job | appointmentId, patientId, doctorId, refundAmount | Notification (patient) |

When a doctor cancels a whole slot, `SlotCancelled` and `AppointmentCancelled` are published but not
`SlotSeatFreed`, because the slot is closed and no seat should be offered.

## Notifications

All eight notifications from the frozen rule set go to SNS (topics per recipient type) and are verified there.
Before each send, the Notification Lambda puts `dispatchKey = <entityId>#<eventType or interval>#<recipient>` into
`dispatch-log` with `attribute_not_exists`. A repeated event, retry or job run finds the key and sends nothing, so
each notification goes out once.

## Failure handling without queues

| Layer | Behaviour |
|---|---|
| EventBridge rule target | Retry policy: up to 8 attempts within 1 hour |
| Lambda asynchronous invocation | 2 retries, then an on-failure destination that publishes to the alarm SNS topic |
| Handlers | Idempotent (conditional writes and the dispatch log), so every retry is safe |
| Alarms | Failed and dropped deliveries above zero (in place of DLQ depth); Lambda error rate |

## Scheduled jobs (EventBridge Scheduler)

Every job is safe to run twice.

| Job | Trigger | Guard against a second run |
|---|---|---|
| Slot materialiser (**Milestone 3, deployed**) | Daily, `cron(15 0 * * ? *)` UTC; each doctor's window is computed in their own time zone | Conditional put per deterministic slotId |
| Offer expiry | One-time schedule at the offer's expiresAt, plus a sweeper every minute as a backstop | Conditional LIVE → EXPIRED |
| Reminders (24 h and 2 h) | One-time schedules per appointment, plus a sweeper every 5 minutes | dispatch-log conditional put; appointment still CONFIRMED and its time unchanged |
| No-show | One-time schedule at slot end + 30 min (SSM), plus a sweeper every 15 minutes | Conditional CONFIRMED → NO_SHOW |
