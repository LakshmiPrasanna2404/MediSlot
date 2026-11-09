"""Slot materialisation and the DynamoDB slots table.

Weekly availability is stored in the doctor's local time (RDS). Slots are
materialised into DynamoDB for a rolling window (30 days by default, from SSM).

Rules implemented here:
* Slot start times step from the pattern start in multiples of the
  consultation duration; a slot is kept only if it ends by the pattern end.
* Arithmetic is done on the local wall clock, then each slot is converted to
  UTC with the doctor's IANA zone. 09:00-17:00 local stays 09:00-17:00 local
  on every date, even when the UTC offset changes (DST).
* A local time that does not exist (spring-forward gap) produces no slot. A
  slot whose real length differs from its nominal length (it spans a DST
  change, which only happens for night-time patterns) is skipped too. An
  ambiguous local time (fall-back overlap) uses the first occurrence (fold=0).
* slotId = <doctorId>#<consultationTypeId>#<YYYYMMDD>#<HHMM> in local time, so
  it is deterministic. Writes use attribute_not_exists(slotId), which makes
  every run idempotent: re-running a date creates no duplicates.
* Blackout dates produce no slots. Existing future OPEN slots that no longer
  match a pattern (pattern removed, blackout added) are deleted only when
  nobody holds a seat in them.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

UTC = UTC
DOCTOR_SLOTS_INDEX = "GSI_Doctor_Slots"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
WRITE_WORKERS = 8


@dataclass(frozen=True)
class Pattern:
    availability_id: str
    consultation_type_id: int
    day_of_week: int  # ISO weekday: Monday=1 ... Sunday=7
    start_time: time
    end_time: time
    duration_minutes: int
    capacity: int


@dataclass(frozen=True)
class SlotSpec:
    slot_id: str
    doctor_id: str
    consultation_type_id: int
    timezone: str
    local_start: datetime  # naive wall-clock time in the doctor's zone
    start_utc: datetime
    end_utc: datetime
    capacity: int

    def item(self):
        """The DynamoDB item, attributes exactly as in the slots table design."""
        return {
            "slotId": self.slot_id,
            "doctorId": self.doctor_id,
            "consultationTypeId": str(self.consultation_type_id),
            "startTime": iso_utc(self.start_utc),
            "endTime": iso_utc(self.end_utc),
            "timezone": self.timezone,
            "status": "OPEN",
            "capacity": self.capacity,
            "bookedCount": 0,
            "heldCount": 0,
            "version": 1,
        }


def iso_utc(value):
    return value.astimezone(UTC).strftime(ISO_FORMAT)


def parse_iso_utc(value):
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=UTC)


def make_slot_id(doctor_id, consultation_type_id, local_start):
    return f"{doctor_id}#{consultation_type_id}#{local_start:%Y%m%d}#{local_start:%H%M}"


def localize(naive, zone):
    """Attach the zone to a wall-clock time; None if that time does not exist."""
    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        return None  # falls in a spring-forward gap
    return candidate


def as_time(value):
    """MySQL TIME arrives as timedelta through PyMySQL."""
    if isinstance(value, time):
        return value
    seconds = int(value.total_seconds())
    return time(seconds // 3600, seconds % 3600 // 60)


def slots_for_date(doctor_id, tz_name, day, patterns, blackouts=frozenset()):
    if day in blackouts:
        return []
    zone = ZoneInfo(tz_name)
    result = []
    for pattern in patterns:
        if pattern.day_of_week != day.isoweekday():
            continue
        step = timedelta(minutes=pattern.duration_minutes)
        cursor = datetime.combine(day, pattern.start_time)
        end = datetime.combine(day, pattern.end_time)
        while cursor + step <= end:
            start = localize(cursor, zone)
            finish = localize(cursor + step, zone)
            if start is not None and finish is not None:
                start_utc = start.astimezone(UTC)
                end_utc = finish.astimezone(UTC)
                # Aware datetimes in the same zone subtract as wall clock, so
                # compare in UTC to catch a slot that spans a DST change.
                if end_utc - start_utc == step:
                    result.append(
                        SlotSpec(
                            slot_id=make_slot_id(doctor_id, pattern.consultation_type_id, cursor),
                            doctor_id=doctor_id,
                            consultation_type_id=pattern.consultation_type_id,
                            timezone=tz_name,
                            local_start=cursor,
                            start_utc=start_utc,
                            end_utc=end_utc,
                            capacity=pattern.capacity,
                        )
                    )
            cursor += step
    return sorted(result, key=lambda spec: (spec.start_utc, spec.consultation_type_id))


def local_today(tz_name, now_utc):
    return now_utc.astimezone(ZoneInfo(tz_name)).date()


def window_end_utc(tz_name, now_utc, days):
    """UTC instant of local midnight at the end of the booking window."""
    zone = ZoneInfo(tz_name)
    last = local_today(tz_name, now_utc) + timedelta(days=days)
    midnight = localize(datetime.combine(last, time(0, 0)), zone)
    if midnight is None:  # a zone whose DST change happens at midnight
        midnight = localize(datetime.combine(last, time(1, 0)), zone)
    return midnight.astimezone(UTC)


def window_slots(doctor_id, tz_name, patterns, blackouts, now_utc, days):
    """Every future slot in the rolling window [local today, today + days)."""
    today = local_today(tz_name, now_utc)
    result = []
    for offset in range(days):
        for spec in slots_for_date(doctor_id, tz_name, today + timedelta(days=offset), patterns, blackouts):
            if spec.start_utc > now_utc:
                result.append(spec)
    return result


# ---------------------------------------------------------------------------
# RDS inputs
# ---------------------------------------------------------------------------

PATTERNS_SQL = """
    SELECT ap.availability_id, ap.consultation_type_id, ap.day_of_week,
           ap.start_time, ap.end_time, ct.duration_minutes, ct.capacity
    FROM availability_patterns ap
    JOIN consultation_types ct
      ON ct.doctor_id = ap.doctor_id AND ct.consultation_type_id = ap.consultation_type_id
    WHERE ap.doctor_id = %s AND ap.is_active = 1 AND ct.is_active = 1
    ORDER BY ap.day_of_week, ap.start_time
"""


def load_doctor_inputs(cursor, doctor_id, from_date, to_date):
    """Return (timezone, patterns, blackout dates) for one doctor."""
    cursor.execute("SELECT timezone FROM doctors WHERE doctor_id = %s", (doctor_id,))
    row = cursor.fetchone()
    if not row:
        raise LookupError(f"doctor {doctor_id} not found")
    cursor.execute(PATTERNS_SQL, (doctor_id,))
    patterns = [
        Pattern(
            availability_id=r["availability_id"],
            consultation_type_id=int(r["consultation_type_id"]),
            day_of_week=int(r["day_of_week"]),
            start_time=as_time(r["start_time"]),
            end_time=as_time(r["end_time"]),
            duration_minutes=int(r["duration_minutes"]),
            capacity=int(r["capacity"]),
        )
        for r in cursor.fetchall()
    ]
    cursor.execute(
        "SELECT `date` FROM blackout_dates WHERE doctor_id = %s AND `date` BETWEEN %s AND %s",
        (doctor_id, from_date, to_date),
    )
    blackouts = frozenset(r["date"] for r in cursor.fetchall())
    return row["timezone"], patterns, blackouts


# ---------------------------------------------------------------------------
# DynamoDB slots table
# ---------------------------------------------------------------------------


def query_doctor_slots(table, doctor_id, start_iso, end_iso):
    """Slots with start_iso <= startTime < end_iso, via GSI_Doctor_Slots."""
    kwargs = {
        "IndexName": DOCTOR_SLOTS_INDEX,
        "KeyConditionExpression": Key("doctorId").eq(doctor_id) & Key("startTime").between(start_iso, end_iso),
    }
    items = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return [item for item in items if item["startTime"] < end_iso]


def _is_condition_failure(exc):
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def create_slots(table, specs):
    """Conditional put per slot. Returns (created, already_existing)."""
    client = table.meta.client

    def put(spec):
        try:
            client.put_item(
                TableName=table.name,
                Item=spec.item(),
                ConditionExpression="attribute_not_exists(slotId)",
            )
            return True
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise

    if not specs:
        return 0, 0
    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool:
        outcomes = list(pool.map(put, specs))
    created = sum(1 for ok in outcomes if ok)
    return created, len(outcomes) - created


def delete_unbooked(table, slot_ids):
    """Delete OPEN slots nobody holds a seat in. Returns (removed, kept_ids)."""
    client = table.meta.client

    def delete(slot_id):
        try:
            client.delete_item(
                TableName=table.name,
                Key={"slotId": slot_id},
                ConditionExpression="#status = :open AND bookedCount = :zero AND heldCount = :zero",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":open": "OPEN", ":zero": 0},
            )
            return slot_id, True
        except ClientError as exc:
            if _is_condition_failure(exc):
                return slot_id, False
            raise

    if not slot_ids:
        return 0, []
    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool:
        outcomes = list(pool.map(delete, slot_ids))
    return sum(1 for _, ok in outcomes if ok), sorted(slot_id for slot_id, ok in outcomes if not ok)


def sync_doctor(table, doctor_id, tz_name, patterns, blackouts, now_utc, days):
    """Make the doctor's future slots match their patterns and blackouts.

    Used by the daily materialiser and right after a doctor edits availability,
    blackouts or time zone. Safe to run any number of times.
    """
    desired = window_slots(doctor_id, tz_name, patterns, blackouts, now_utc, days)
    desired_by_id = {spec.slot_id: spec for spec in desired}
    start_iso = iso_utc(now_utc)
    end_iso = iso_utc(max(window_end_utc(tz_name, now_utc, days), now_utc + timedelta(days=days + 2)))
    existing = [item for item in query_doctor_slots(table, doctor_id, start_iso, end_iso) if item["startTime"] > start_iso]

    stale = []
    orphans = []
    existing_ids = set()
    for item in existing:
        existing_ids.add(item["slotId"])
        spec = desired_by_id.get(item["slotId"])
        if spec is None:
            if item.get("status") == "OPEN":
                orphans.append(item["slotId"])
        elif item["startTime"] != iso_utc(spec.start_utc) or item.get("timezone") != tz_name:
            stale.append(item["slotId"])  # same local slot, but the doctor changed time zone

    removed, kept_orphans = delete_unbooked(table, orphans)
    removed_stale, kept_stale = delete_unbooked(table, stale)
    stale_ids = set(stale)
    recreate = [desired_by_id[slot_id] for slot_id in stale if slot_id not in set(kept_stale)]
    to_create = [spec for spec in desired if spec.slot_id not in existing_ids] + recreate
    created, already = create_slots(table, to_create)
    unchanged = sum(1 for spec in desired if spec.slot_id in existing_ids and spec.slot_id not in stale_ids)
    return {
        "doctorId": doctor_id,
        "desired": len(desired),
        "created": created,
        "alreadyExisted": unchanged + already,
        "removed": removed + removed_stale,
        "keptBecauseBooked": kept_orphans + kept_stale,
    }


def free_seats(item):
    return max(0, int(item.get("capacity", 0)) - int(item.get("bookedCount", 0)) - int(item.get("heldCount", 0)))


def slot_view(item):
    """API representation of a slot item, with local times for display."""
    zone = ZoneInfo(item["timezone"])
    start = parse_iso_utc(item["startTime"])
    end = parse_iso_utc(item["endTime"])
    return {
        "slot_id": item["slotId"],
        "doctor_id": item["doctorId"],
        "consultation_type_id": int(item["consultationTypeId"]),
        "start_utc": item["startTime"],
        "end_utc": item["endTime"],
        "local_start": start.astimezone(zone).isoformat(),
        "local_end": end.astimezone(zone).isoformat(),
        "timezone": item["timezone"],
        "status": item.get("status"),
        "capacity": int(item.get("capacity", 0)),
        "booked_count": int(item.get("bookedCount", 0)),
        "held_count": int(item.get("heldCount", 0)),
        "free_seats": free_seats(item),
    }


def doctor_local_range(tz_name, first_day, days):
    """UTC [start, end) bounds of `days` local days starting at first_day."""
    zone = ZoneInfo(tz_name)

    def midnight(day):
        value = localize(datetime.combine(day, time(0, 0)), zone)
        if value is None:
            value = localize(datetime.combine(day, time(1, 0)), zone)
        return value.astimezone(UTC)

    return midnight(first_day), midnight(first_day + timedelta(days=days))


def now_utc():
    return datetime.now(UTC)


def window_dates(tz_name, now, days):
    """(first, last) local dates of the rolling window, for blackout lookups."""
    first = local_today(tz_name, now)
    return first, first + timedelta(days=days)


def sync_from_database(cursor, table, doctor_id, now, days):
    """Load the doctor's inputs from RDS and sync their slots."""
    tz_name = cursor_timezone(cursor, doctor_id)
    first, last = window_dates(tz_name, now, days)
    tz_name, patterns, blackouts = load_doctor_inputs(cursor, doctor_id, first, last)
    return sync_doctor(table, doctor_id, tz_name, patterns, blackouts, now, days)


def cursor_timezone(cursor, doctor_id):
    cursor.execute("SELECT timezone FROM doctors WHERE doctor_id = %s", (doctor_id,))
    row = cursor.fetchone()
    if not row:
        raise LookupError(f"doctor {doctor_id} not found")
    return row["timezone"]

