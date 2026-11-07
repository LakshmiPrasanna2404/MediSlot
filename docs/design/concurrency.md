# Concurrency strategy

Written at Milestone 1 and implemented in Milestones 5 (booking) and 6 (reschedule, waitlist). The full
Concurrency Design Note with measurements follows in Milestone 5. Table and attribute names are the final
DynamoDB schema.

## The contended resource

The **slot item** in the `slots` table: `capacity`, `bookedCount`, `heldCount`, `version`, `status`.
A seat is free when `status = OPEN` and `bookedCount + heldCount < capacity`. Capacity is 1, or 2–10 for a Health
Camp Session.

## Booking: optimistic concurrency with a conditional transaction

1. Strongly consistent read of the slot: remember `version`.
2. One `TransactWriteItems`:
   - **Update the slot**: `SET bookedCount = bookedCount + 1, version = version + 1`, condition
     `version = :v AND #status = :open AND bookedCount + heldCount < capacity`.
   - **Put the seat** (`seats`: slotId + patientId), condition `attribute_not_exists(patientId)`: one seat per
     patient per slot.
3. Read the cancellation reasons. An existing seat or a full or closed slot returns **409** at once. A version
   mismatch (another booking won the race) is retried.
4. **Bounded retry**: up to `booking/max-conflict-retries` (5) attempts, with exponential backoff and full jitter
   (`booking/backoff-base-ms`, 20 ms). Each retry adds to the `BookingConflictRetries` metric, which shows the
   contention is real and handled rather than avoided. After the last attempt the request gets 409.
5. With 20 parallel requests on a capacity-3 slot, the condition can be true for at most three committed
   transactions, because each one moves `bookedCount` and `version`. There is no lock and no queue: requests run in
   parallel and DynamoDB's conditional write decides.

## Idempotency

Table `idempotency`, key `patientId#key` = `<patientId>#<Idempotency-Key header>`, TTL 24 hours (SSM).

- The first request puts `{status: IN_PROGRESS, requestHash}` with `attribute_not_exists`.
- On success the stored record becomes `COMPLETED` with the response. A replay with the same hash returns the stored
  status and body, which is the same appointment, and creates nothing new.
- A replay while still `IN_PROGRESS` gets 409 (retry later). The same key with a different body gets 422.
- Deterministic outcomes (409 full, 4xx validation) are stored too. After a 5xx the record is deleted so the client
  can retry cleanly.
- RDS backs this up: `UNIQUE(patient_id, idempotency_key)` on appointments.

## Seat first, then the system of record

After the seat is claimed, the appointment row is written to RDS. If the RDS write fails, the seat is released with a
compensating update (`bookedCount - 1`, the seat item deleted). `UNIQUE(patient_id, active_slot_id)` in RDS is a
second guard against two confirmed appointments for one patient in one slot.

**Three upcoming appointments per doctor.** Counted in RDS (index on patient_id, slot_start_utc, status) inside a
transaction that first locks the patient's own row (`SELECT … FOR UPDATE` on `patients`). That serialises one
patient's parallel requests only, never the slot, so it does not turn booking into a single queue.

## Reschedule

A Step Functions saga (see [reschedule-saga.md](reschedule-saga.md)): claim the new seat with the same conditional
transaction, move the appointment in RDS (conditional on `version`), release the old seat last, and compensate if a
step fails. The patient always holds exactly one appointment.

## Waitlist exclusivity

When `SlotSeatFreed` arrives, the Waitlist Processor runs **one transaction**:

- Slot: `heldCount + 1, version + 1` with the condition `version = :v AND bookedCount + heldCount < capacity`.
- Waitlist entry (the first by `LSI_QueueOrder`): status `WAITING` → `OFFERED`, conditional on WAITING.
- Hold offer: put `{status: LIVE, expiresAt: now + 15 min (SSM)}`.

If any condition fails the whole transaction fails, so one freed seat becomes exactly one live offer. While the seat
is held, `heldCount` counts against capacity, so normal booking cannot take it. The offer expires through its
one-time schedule (plus a sweeper backstop), conditionally `LIVE` → `EXPIRED`, and the seat passes to the next
patient in line.

## What is and is not guaranteed

| Guaranteed | How |
|---|---|
| No overbooking, including multi-seat Health Camp slots | Conditional transaction on the slot item |
| One seat per patient per slot | Seat item with attribute_not_exists, plus the generated active_slot_id unique key in RDS |
| One appointment per Idempotency-Key | Stored key, request hash and response |
| Exactly one appointment after a reschedule, even on failure | Saga with compensation; the irreversible release runs last |
| One live offer per freed seat | One transaction over slot, waitlist entry and offer |

| Not atomic across stores | Mitigation |
|---|---|
| DynamoDB seat and RDS row are separate writes | Release the seat if the RDS write fails; seats carry a timestamp so a stale seat with no appointment can be reclaimed |
| An event may be lost if a Lambda dies between the RDS commit and publishing | Publish with retries before returning; every consumer is idempotent |
