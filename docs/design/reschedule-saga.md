# Reschedule saga (Milestone 6)

The patient architecture diagram, box 9. `PATCH /appointments/{id}` with `{"target_slot_id": "..."}` goes to the
Reschedule Starter Lambda, which checks the rules and starts the Step Functions state machine
`medislot-<env>-reschedule`.

## Starter checks (before the saga starts)

- The appointment belongs to the caller and is `CONFIRMED`.
- `reschedule_count < reschedule/max-count` (2).
- The current start is more than `reschedule/cutoff-hours` (6) away.
- The target slot is different, in the future, `OPEN`, for the same doctor.
- Set `appointments.reschedule_state = 'IN_PROGRESS'`, conditional on `version` (so two sagas cannot run on one
  appointment), then start the execution with the name `<appointmentId>-<rescheduleCount>`, so a repeated start
  is rejected.

## Steps and compensation

| # | Step | If it fails |
|---|---|---|
| 1 | Validate the reschedule rules again (state may have changed) | Stop. Nothing to undo. |
| 2 | Claim a seat in the new slot: same conditional transaction as booking | New slot full: stop with 409. The original appointment is untouched. |
| 3 | Move the appointment in RDS: `slot_id`, times and fee of the new slot, `reschedule_count + 1`, `version + 1`, conditional on the version read in step 1 | **Compensation A**: release the new seat. The original stays CONFIRMED. |
| 4 | Release the seat in the old slot (with retries and backoff) | **Compensation B**: move the RDS row back to the original slot (`reschedule_count - 1`), then release the new seat. The original stays CONFIRMED. |
| 5 | Publish `AppointmentRescheduled` and `SlotSeatFreed` (old slot) | Retried; the move has already happened. |
| 6 | Notify the patient and the doctor (through the events) | Retried. |

The release is last because it is the only step that cannot be undone safely: once the old seat is free, another
patient may take it. Until then the patient always holds a seat they can fall back to, so after any failure they hold
exactly one appointment, never zero and never two.

A reschedule moves the appointment. It carries no refund and no fee.

## Testing the compensation (Mandatory Test 3)

The SSM flag `config/test/fail-release` makes step 4 fail on purpose. The test sets it, reschedules, and checks that
the patient still holds exactly one CONFIRMED appointment (the original), that the new seat is free again, and that
`ReschedulesCompensated` went up (an alarm fires on any compensation). The execution history in Step Functions shows
the compensation branch.
