# Timezone design

## Rules

- Every doctor has a home time zone, an IANA identifier such as `Asia/Kolkata` (`doctors.timezone`).
- Weekly availability is stored in the doctor's **local** time: day of week (ISO, Monday = 1), start time, end time,
  consultation type (`availability_patterns`).
- Slots are stored in **UTC** (`startTime`, `endTime` in the slots table) with the originating zone next to them
  (`timezone`).
- A pattern of 09:00–17:00 means 09:00–17:00 local on every date, even when the UTC offset changes.

## Materialisation

Code: `lambda/shared/medislot_common/slots.py`. Run by the daily materialiser and straight after a doctor edits
availability, blackouts or their time zone.

1. The window is the doctor's local today plus the next 29 days (30 days, the `BookingWindowDays` stack parameter).
2. Blackout dates are skipped.
3. For each pattern on that weekday, step through the **local wall clock** from the pattern start in steps of the
   consultation duration. A slot is kept only if it ends by the pattern end (09:00–10:40 with 30 minutes gives
   09:00, 09:30, 10:00).
4. Each local start and end is attached to the zone with Python `zoneinfo` and converted to UTC. Because the steps
   are done in local time, the local times are identical every day; only the UTC offset moves.
5. Edge cases (only night-time patterns can hit them):
   - A local time that does not exist (the spring-forward gap) gives no slot.
   - A slot whose real length differs from its nominal length (it spans the change) is skipped.
   - An ambiguous local time (the fall-back hour that happens twice) uses the first occurrence (`fold=0`), so each
     local time gives exactly one slot.
6. `slotId = <doctorId>#<consultationTypeId>#<YYYYMMDD>#<HHMM>` in local time, so it is deterministic. Each slot is
   written with `attribute_not_exists(slotId)`, so running the job again for the same date creates nothing new.
7. Future OPEN slots that no longer match a pattern (pattern removed, blackout added) are deleted only if
   `bookedCount = 0 AND heldCount = 0`. A slot somebody holds a seat in is never deleted; it is reported back so the
   doctor can cancel it.

## Worked example across a DST change

India has no daylight saving time, so the tests use New York and London.

**Dr X, America/New_York, Mondays 09:00–17:00, 30-minute Standard Consult.**
The US leaves daylight saving time on Sunday 1 November 2026 (UTC−4 becomes UTC−5).

| Local date | Local slot | UTC offset | Stored startTime | Stored endTime |
|---|---|---|---|---|
| Mon 26 Oct 2026 | 09:00–09:30 | −04:00 | 2026-10-26T13:00:00Z | 2026-10-26T13:30:00Z |
| Mon 26 Oct 2026 | 16:30–17:00 | −04:00 | 2026-10-26T20:30:00Z | 2026-10-26T21:00:00Z |
| Mon 2 Nov 2026 | 09:00–09:30 | −05:00 | 2026-11-02T14:00:00Z | 2026-11-02T14:30:00Z |
| Mon 2 Nov 2026 | 16:30–17:00 | −05:00 | 2026-11-02T21:30:00Z | 2026-11-02T22:00:00Z |

Both Mondays have 16 slots from 09:00 to 17:00 local; the UTC instants are one hour later after the change. If the
code had added 7 × 24 hours in UTC instead of stepping local dates, the second Monday would start at 08:00 local.

**Europe/London, Mondays 09:00–12:00, 60 minutes.** The UK moves to summer time on Sunday 28 March 2027.

| Local date | Local slots | Stored startTime values |
|---|---|---|
| Mon 22 Mar 2027 (GMT, +00:00) | 09:00, 10:00, 11:00 | 09:00Z, 10:00Z, 11:00Z |
| Mon 29 Mar 2027 (BST, +01:00) | 09:00, 10:00, 11:00 | 08:00Z, 09:00Z, 10:00Z |

**Night pattern on the spring-forward night (New York, Sunday 14 March 2027, 01:00–04:00, 30 minutes).** 02:00–02:59
does not exist, so the slots are 01:00, 03:00 and 03:30. 01:30 is skipped as well, because 01:30–02:00 local is
only 30 real minutes long but ends in the gap. Every slot that is kept lasts exactly 30 real minutes.

These cases are in `tests/unit/test_slot_materialisation.py`:
`test_local_time_is_kept_across_us_fall_back`, `test_local_time_is_kept_across_uk_spring_forward`,
`test_nonexistent_local_times_are_skipped_on_spring_forward_night`,
`test_ambiguous_fall_back_hour_gets_one_slot_per_local_time`.

## Reading slots back

The doctor's schedule and the patient's slot browser take local dates. The API converts a local date range into
UTC bounds (local midnight to local midnight in the doctor's zone) and queries `GSI_Doctor_Slots` with
`startTime BETWEEN`. Responses carry both the UTC instants and the local times with their offset
(`local_start: 2026-11-02T09:00:00-05:00`), so the UI never converts time itself.

## Changing a doctor's time zone

The local patterns stay the same and the slots move: every future unbooked slot is rewritten at the new UTC
instants. The change is refused while the doctor has upcoming confirmed appointments, because those were booked
at fixed instants.
