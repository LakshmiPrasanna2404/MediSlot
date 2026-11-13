"""Slot Materialiser Lambda (scheduled job).

Triggered daily by EventBridge Scheduler. Rolls the booking window forward:
for every doctor with active weekly patterns it creates any missing slot for
local today .. today + N - 1 (N = the BookingWindowDays parameter, 30), skips
blackout dates, and removes future OPEN slots nobody holds a seat in that no
longer match a pattern.

Safe to run twice: the table key is (doctorId, startTime) and every create is
a conditional put (attribute_not_exists), so a second run for the same date
creates nothing and a doctor can never get two slots at the same time.

Event:
    {}                        all doctors (the daily schedule sends this)
    {"doctorId": "<uuid>"}    one doctor (used by tests and for a manual re-run)
"""

from medislot_common import config, db, logs, slots


def doctors_to_process(cursor, event):
    if event.get("doctorId"):
        return [str(event["doctorId"])]
    cursor.execute(
        """SELECT DISTINCT ap.doctor_id
           FROM availability_patterns ap
           JOIN doctors d ON d.doctor_id = ap.doctor_id
           JOIN users u ON u.user_id = d.user_id
           WHERE ap.is_active = 1 AND u.is_active = 1
           ORDER BY ap.doctor_id"""
    )
    return [row["doctor_id"] for row in cursor.fetchall()]


def lambda_handler(event, context):
    event = event or {}
    run_id = getattr(context, "aws_request_id", "local")
    logs.set_correlation_id(f"materialiser-{run_id}", run_id)
    days = config.config_int("slots/booking-window-days")
    now = slots.now_utc()
    table = config.table("SLOTS_TABLE")

    totals = {"doctors": 0, "created": 0, "updated": 0, "alreadyExisted": 0, "removed": 0, "keptBecauseBooked": 0,
              "failed": []}
    with db.connection().cursor() as cursor:
        doctor_ids = doctors_to_process(cursor, event)
        logs.info("materialiser started", doctors=len(doctor_ids), windowDays=days, onlyDoctor=event.get("doctorId"))
        for doctor_id in doctor_ids:
            try:
                summary = slots.sync_from_database(cursor, table, doctor_id, now, days)
            except Exception:  # noqa: BLE001 - one bad doctor must not stop the others
                logs.exception("materialisation failed for doctor", doctorId=doctor_id)
                totals["failed"].append(doctor_id)
                continue
            totals["doctors"] += 1
            totals["created"] += summary["created"]
            totals["updated"] += summary["updated"]
            totals["alreadyExisted"] += summary["alreadyExisted"]
            totals["removed"] += summary["removed"]
            totals["keptBecauseBooked"] += len(summary["keptBecauseBooked"])
            logs.info("doctor slots synced", **summary)

    logs.metric("SlotsMaterialised", totals["created"])
    logs.info("materialiser finished", windowDays=days, **totals)
    if totals["failed"]:
        # Fail the invocation so the scheduler's retry policy runs it again;
        # doctors that already succeeded are untouched by the retry.
        raise RuntimeError(f"Materialisation failed for {len(totals['failed'])} doctor(s)")
    return totals
