"""Slot materialisation: alignment, DST, blackouts, the rolling window and idempotency.

Worked DST example (also in docs/design/timezone.md): a New York doctor
works Monday 09:00-17:00 local. Sunday 2026-11-01 the US leaves daylight
saving time (UTC-4 -> UTC-5). On Monday 2026-10-26 09:00 local is 13:00Z; on
Monday 2026-11-02 09:00 local is 14:00Z. The local time is the same, the UTC
instant moves by an hour.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from conftest import FakeContext, load_handler

from medislot_common import slots


def pattern(day, start, end, minutes=30, capacity=1, type_id=1):
    return slots.Pattern("a-1", type_id, day, time.fromisoformat(start), time.fromisoformat(end), minutes, capacity)


def test_slots_align_to_duration_and_must_fit():
    specs = slots.slots_for_date("doc", "Asia/Kolkata", date(2026, 11, 9), [pattern(1, "09:00", "10:40", 30)])
    assert [s.local_start.strftime("%H:%M") for s in specs] == ["09:00", "09:30", "10:00"]  # 10:30 would end at 11:00
    assert specs[0].slot_id == "doc#1#20261109#0900"
    assert slots.iso_utc(specs[0].start_utc) == "2026-11-09T03:30:00Z"  # IST is UTC+5:30


def test_pattern_only_applies_on_its_weekday():
    assert slots.slots_for_date("doc", "Asia/Kolkata", date(2026, 11, 10), [pattern(1, "09:00", "10:00")]) == []


def test_local_time_is_kept_across_us_fall_back():
    weekly = [pattern(1, "09:00", "17:00", 30)]
    before = slots.slots_for_date("doc", "America/New_York", date(2026, 10, 26), weekly)
    after = slots.slots_for_date("doc", "America/New_York", date(2026, 11, 2), weekly)
    assert [s.local_start.time() for s in before] == [s.local_start.time() for s in after]
    assert len(before) == len(after) == 16
    assert slots.iso_utc(before[0].start_utc) == "2026-10-26T13:00:00Z"
    assert slots.iso_utc(after[0].start_utc) == "2026-11-02T14:00:00Z"
    assert slots.iso_utc(after[-1].end_utc) == "2026-11-02T22:00:00Z"


def test_local_time_is_kept_across_uk_spring_forward():
    weekly = [pattern(1, "09:00", "12:00", 60)]
    before = slots.slots_for_date("doc", "Europe/London", date(2027, 3, 22), weekly)
    after = slots.slots_for_date("doc", "Europe/London", date(2027, 3, 29), weekly)
    assert [slots.iso_utc(s.start_utc) for s in before] == ["2027-03-22T09:00:00Z", "2027-03-22T10:00:00Z", "2027-03-22T11:00:00Z"]
    assert [slots.iso_utc(s.start_utc) for s in after] == ["2027-03-29T08:00:00Z", "2027-03-29T09:00:00Z", "2027-03-29T10:00:00Z"]


def test_nonexistent_local_times_are_skipped_on_spring_forward_night():
    # 2027-03-14 (Sunday): New York clocks jump 02:00 -> 03:00.
    specs = slots.slots_for_date("doc", "America/New_York", date(2027, 3, 14), [pattern(7, "01:00", "04:00", 30)])
    assert [s.local_start.strftime("%H:%M") for s in specs] == ["01:00", "03:00", "03:30"]
    for spec in specs:
        assert spec.end_utc - spec.start_utc == timedelta(minutes=30)


def test_ambiguous_fall_back_hour_gets_one_slot_per_local_time():
    # 2026-11-01 (Sunday): New York 01:00-02:00 happens twice.
    specs = slots.slots_for_date("doc", "America/New_York", date(2026, 11, 1), [pattern(7, "00:00", "03:00", 60)])
    ids = [s.slot_id for s in specs]
    assert len(ids) == len(set(ids))
    for spec in specs:
        assert spec.end_utc - spec.start_utc == timedelta(minutes=60)


def test_blackout_date_produces_no_slots():
    day = date(2026, 11, 9)
    assert slots.slots_for_date("doc", "Asia/Kolkata", day, [pattern(1, "09:00", "10:00")], frozenset({day})) == []


def test_window_covers_30_local_days_and_skips_past_slots():
    now = datetime(2026, 11, 9, 3, 45, tzinfo=UTC)  # 09:15 IST, Monday
    weekly = [pattern(d, "09:00", "10:00", 30) for d in range(1, 8)]
    specs = slots.window_slots("doc", "Asia/Kolkata", weekly, frozenset(), now, 30)
    local_days = sorted({s.local_start.date() for s in specs})
    assert local_days[0] == date(2026, 11, 9) and local_days[-1] == date(2026, 12, 8)
    assert len(local_days) == 30
    assert all(s.start_utc > now for s in specs)
    assert specs[0].local_start.strftime("%H:%M") == "09:30"


def _slot_table(aws):
    return aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-slots")


def test_sync_is_idempotent_and_removes_only_unbooked_orphans(aws):
    table = _slot_table(aws)
    now = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    weekly = [pattern(1, "09:00", "11:00", 30)]
    first = slots.sync_doctor(table, "doc", "Asia/Kolkata", weekly, frozenset(), now, 30)
    assert first["created"] == 4 * 5  # 5 Mondays in the window, 4 slots each
    again = slots.sync_doctor(table, "doc", "Asia/Kolkata", weekly, frozenset(), now, 30)
    assert again["created"] == 0 and again["alreadyExisted"] == 20
    assert table.scan(Select="COUNT")["Count"] == 20

    booked = "doc#1#20261116#0900"
    table.update_item(Key={"slotId": booked}, UpdateExpression="SET bookedCount = :one", ExpressionAttributeValues={":one": 1})
    removed = slots.sync_doctor(table, "doc", "Asia/Kolkata", [], frozenset(), now, 30)
    assert removed["removed"] == 19
    assert removed["keptBecauseBooked"] == [booked]
    assert [i["slotId"] for i in table.scan()["Items"]] == [booked]


def test_sync_moves_slots_when_time_zone_changes(aws):
    table = _slot_table(aws)
    now = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    weekly = [pattern(1, "09:00", "09:30", 30)]
    slots.sync_doctor(table, "doc", "Asia/Kolkata", weekly, frozenset(), now, 7)
    assert table.get_item(Key={"slotId": "doc#1#20261109#0900"})["Item"]["startTime"] == "2026-11-09T03:30:00Z"
    slots.sync_doctor(table, "doc", "Europe/London", weekly, frozenset(), now, 7)
    item = table.get_item(Key={"slotId": "doc#1#20261109#0900"})["Item"]
    assert item["startTime"] == "2026-11-09T09:00:00Z" and item["timezone"] == "Europe/London"


@pytest.fixture()
def doctor_with_patterns(register, clean_db):
    def _make(tz_name="Asia/Kolkata"):
        user = register("doctor", timezone=tz_name)
        doctor_id = user["profile_id"]
        with clean_db.cursor() as cur:
            cur.execute(
                """INSERT INTO consultation_types (doctor_id, name, fee, duration_minutes, capacity)
                   VALUES (%s, 'Health Camp Session', 100, 60, 3)""",
                (doctor_id,),
            )
            type_id = cur.lastrowid
            for day in range(1, 8):
                cur.execute(
                    """INSERT INTO availability_patterns (availability_id, doctor_id, consultation_type_id,
                       day_of_week, start_time, end_time) VALUES (UUID(), %s, %s, %s, '10:00', '12:00')""",
                    (doctor_id, type_id, day),
                )
        return doctor_id, type_id

    return _make


def test_materialiser_lambda_run_twice_creates_no_duplicates(aws, doctor_with_patterns):
    doctor_id, type_id = doctor_with_patterns()
    materialiser = load_handler("slot_materialiser")
    first = materialiser.lambda_handler({}, FakeContext())
    second = materialiser.lambda_handler({}, FakeContext())
    table = _slot_table(aws)
    count = table.scan(Select="COUNT")["Count"]
    assert first["doctors"] == 1 and first["created"] == count
    assert second["created"] == 0 and second["alreadyExisted"] == count
    assert 58 <= count <= 60  # 2 slots a day for 30 days, minus any that already started today
    item = table.scan()["Items"][0]
    assert item["capacity"] == 3 and item["bookedCount"] == 0 and item["heldCount"] == 0
    assert item["consultationTypeId"] == str(type_id) and item["timezone"] == "Asia/Kolkata"
