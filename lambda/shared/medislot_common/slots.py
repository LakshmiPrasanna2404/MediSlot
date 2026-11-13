"""Slot materialisation and the DynamoDB doctor-slots table.

Weekly availability is stored in the doctor's local time (RDS). Slots are
materialised into DynamoDB for a rolling window (30 days by default, an app stack parameter).

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
* The table key is (doctorId, startTime): partition key doctorId, sort key
  the UTC start time. A doctor can therefore never have two slots that start
  at the same moment; the database refuses a second one. Creates use
  attribute_not_exists(startTime), so re-running a date creates nothing new.
* slotId = <doctorId>#<startTime>, e.g. 7f1c...#2026-11-16T03:30:00Z. It is
  stored on the item and used by appointments, seats and the waitlist; the
  table key is read back from it with slot_key().
* Blackout dates produce no slots. Existing future OPEN slots that no longer
  match a pattern (pattern removed, blackout added, time zone changed) are
  deleted, and slots whose type, length or seats changed are rewritten, but
  only when nobody holds a seat in them.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

UTC = UTC
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
        """The DynamoDB item, attributes exactly as in the doctor-slots table design."""
        return {
            "doctorId": self.doctor_id,
            "startTime": iso_utc(self.start_utc),
            "slotId": self.slot_id,
            "consultationTypeId": str(self.consultation_type_id),
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


def make_slot_id(doctor_id, start_utc):
    return f"{doctor_id}#{iso_utc(start_utc)}"


def slot_key(slot_id):
    """The table key for a slotId: {"doctorId": ..., "startTime": ...}."""
    doctor_id, separator, start_time = str(slot_id).partition("#")
    if not separator or not doctor_id or not start_time:
        raise ValueError(f"not a slot id: {slot_id!r}")
    return {"doctorId": doctor_id, "startTime": start_time}


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
                            slot_id=make_slot_id(doctor_id, start_utc),
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
           ap.start_time, ap.end_time, ct.duration_minutes, dct.capacity
    FROM availability_patterns ap
    JOIN doctor_consultation_types dct
      ON dct.doctor_id = ap.doctor_id AND dct.consultation_type_id = ap.consultation_type_id
    JOIN consultation_types ct ON ct.consultation_type_id = ap.consultation_type_id
    WHERE ap.doctor_id = %s AND ap.is_active = 1 AND dct.is_active = 1
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


def query_doctor_slots(table, doctor_id, start_iso, end_iso, consistent=False):
    """The doctor's slots with start_iso <= startTime < end_iso, in time order.

    One Query on the table's own key (doctorId, startTime); no index needed.
    """
    kwargs = {
        "KeyConditionExpression": Key("doctorId").eq(doctor_id) & Key("startTime").between(start_iso, end_iso),
        "ConsistentRead": consistent,
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


UNBOOKED = "#status = :open AND bookedCount = :zero AND heldCount = :zero"
UNBOOKED_NAMES = {"#status": "status"}
UNBOOKED_VALUES = {":open": "OPEN", ":zero": 0}


def _run(specs, write):
    if not specs:
        return []
    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool:
        return list(pool.map(write, specs))


def create_slots(table, specs):
    """Put each new slot unless one already starts then. Returns (created, already_existing)."""
    client = table.meta.client

    def put(spec):
        try:
            client.put_item(TableName=table.name, Item=spec.item(), ConditionExpression="attribute_not_exists(startTime)")
            return True
        except ClientError as exc:
            if _is_condition_failure(exc):
                return False
            raise

    outcomes = _run(specs, put)
    created = sum(1 for ok in outcomes if ok)
    return created, len(outcomes) - created


def replace_unbooked(table, specs):
    """Rewrite slots whose type, length or seats changed, if nobody holds a seat.

    Returns (replaced, kept_slot_ids).
    """
    client = table.meta.client

    def put(spec):
        try:
            client.put_item(
                TableName=table.name,
                Item=spec.item(),
                ConditionExpression=UNBOOKED,
                ExpressionAttributeNames=UNBOOKED_NAMES,
                ExpressionAttributeValues=UNBOOKED_VALUES,
            )
            return spec.slot_id, True
        except ClientError as exc:
            if _is_condition_failure(exc):
                return spec.slot_id, False
            raise

    outcomes = _run(specs, put)
    return sum(1 for _, ok in outcomes if ok), sorted(slot_id for slot_id, ok in outcomes if not ok)


def delete_unbooked(table, slot_ids):
    """Delete OPEN slots nobody holds a seat in. Returns (removed, kept_slot_ids)."""
    client = table.meta.client

    def delete(slot_id):
        try:
            client.delete_item(
                TableName=table.name,
                Key=slot_key(slot_id),
                ConditionExpression=UNBOOKED,
                ExpressionAttributeNames=UNBOOKED_NAMES,
                ExpressionAttributeValues=UNBOOKED_VALUES,
            )
            return slot_id, True
        except ClientError as exc:
            if _is_condition_failure(exc):
                return slot_id, False
            raise

    outcomes = _run(slot_ids, delete)
    return sum(1 for _, ok in outcomes if ok), sorted(slot_id for slot_id, ok in outcomes if not ok)


def _same_slot(item, spec):
    """True when an existing item already is the slot the patterns ask for."""
    wanted = spec.item()
    return all(str(item.get(name)) == str(wanted[name])
               for name in ("consultationTypeId", "endTime", "timezone", "capacity", "slotId"))


def sync_doctor(table, doctor_id, tz_name, patterns, blackouts, now_utc, days):
    """Make the doctor's future slots match their patterns and blackouts.

    Used by the daily materialiser and right after a doctor edits availability,
    blackouts or time zone. Safe to run any number of times: the table holds
    at most one slot per (doctorId, startTime), so nothing is ever duplicated.
    """
    desired = window_slots(doctor_id, tz_name, patterns, blackouts, now_utc, days)
    desired_by_start = {iso_utc(spec.start_utc): spec for spec in desired}
    start_iso = iso_utc(now_utc)
    end_iso = iso_utc(max(window_end_utc(tz_name, now_utc, days), now_utc + timedelta(days=days + 2)))
    existing = [item for item in query_doctor_slots(table, doctor_id, start_iso, end_iso, consistent=True)
                if item["startTime"] > start_iso]

    orphans, changed, existing_starts = [], [], set()
    for item in existing:
        existing_starts.add(item["startTime"])
        spec = desired_by_start.get(item["startTime"])
        if spec is None:
            if item.get("status") == "OPEN":
                orphans.append(item["slotId"])  # no pattern wants this time any more
        elif not _same_slot(item, spec):
            changed.append(spec)  # same time, but another type, length, seat count or zone

    removed, kept_orphans = delete_unbooked(table, orphans)
    updated, kept_changed = replace_unbooked(table, changed)
    to_create = [spec for start, spec in desired_by_start.items() if start not in existing_starts]
    created, already = create_slots(table, to_create)
    return {
        "doctorId": doctor_id,
        "desired": len(desired),
        "created": created,
        "updated": updated,
        "alreadyExisted": len(existing_starts & set(desired_by_start)) - len(changed) + already,
        "removed": removed,
        "keptBecauseBooked": kept_orphans + kept_changed,
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

