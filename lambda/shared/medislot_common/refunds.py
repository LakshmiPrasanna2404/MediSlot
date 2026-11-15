"""Refund tiers and the refund_ledger table (RDS).

The tier comes from the time between the appointment start and the moment of
cancellation, both in UTC. Hours and percentages are stack parameters
(CONFIG_REFUNDS_TIER_HOURS = "48,24,6", CONFIG_REFUNDS_TIER_PERCENTS =
"100,75,50,0"), so a change is a parameter change, not a code change:

    more than 48 hours before              100%
    24 hours up to and including 48 hours   75%
    6 hours up to but not including 24      50%
    less than 6 hours                        0%

Exactly 48:00:00 before is therefore 75%, 24:00:00 is 75% and 6:00:00 is 50%:
the first boundary belongs to the tier below it, every other boundary to the
tier above it (each lower bound is inclusive).

A doctor's cancellation always refunds 100% (CONFIG_REFUNDS_DOCTOR_CANCEL_PERCENT)
and a no-show refunds 0%. Nothing is charged: the ledger records what would be
returned. One row per appointment (UNIQUE appointment_id), so a retry or a
double click can never refund twice.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal

import pymysql

from . import config

PATIENT_CANCEL = "PATIENT_CANCEL"
DOCTOR_CANCEL = "DOCTOR_CANCEL"
NO_SHOW = "NO_SHOW"
CENT = Decimal("0.01")


def tier_settings():
    hours = [int(part) for part in config.config_value("refunds/tier-hours").split(",")]
    percents = [int(part) for part in config.config_value("refunds/tier-percents").split(",")]
    if len(hours) != 3 or len(percents) != 4 or hours != sorted(hours, reverse=True):
        raise RuntimeError("refund tiers need three descending hour bounds and four percentages")
    return hours, percents


def patient_cancel_percent(seconds_before, hours=None, percents=None):
    """The refund percentage for a patient who cancels this many seconds before the start."""
    if hours is None or percents is None:
        hours, percents = tier_settings()
    if seconds_before > hours[0] * 3600:
        return percents[0]
    if seconds_before >= hours[1] * 3600:
        return percents[1]
    if seconds_before >= hours[2] * 3600:
        return percents[2]
    return percents[3]


def tier_name(percent):
    if percent >= 100:
        return "FULL"
    if percent <= 0:
        return "NONE"
    return "PARTIAL"


def amounts(fee, percent):
    """Base fee, refund and deduction, rounded half up to 2 decimals."""
    base = Decimal(str(fee)).quantize(CENT, rounding=ROUND_HALF_UP)
    refund = (base * Decimal(percent) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    return base, refund, base - refund


def for_patient_cancel(fee, start_utc, now_utc):
    percent = patient_cancel_percent((start_utc - now_utc).total_seconds())
    return _refund(fee, percent, PATIENT_CANCEL)


def for_doctor_cancel(fee):
    return _refund(fee, config.config_int("refunds/doctor-cancel-percent"), DOCTOR_CANCEL)


def for_no_show(fee):
    return _refund(fee, 0, NO_SHOW)


def _refund(fee, percent, reason):
    base, refund, deduction = amounts(fee, percent)
    return {
        "tier": tier_name(percent),
        "refund_percent": percent,
        "base_fee_amount": base,
        "refund_amount": refund,
        "cancellation_fee_amount": deduction,
        "reason": reason,
    }


def record(cursor, appointment_id, patient_id, refund):
    """Write the ledger row; if one exists already (a retried step), return that one."""
    try:
        cursor.execute(
            """INSERT INTO refund_ledger (refund_id, appointment_id, patient_id, tier, base_fee_amount, refund_percent,
                                          cancellation_fee_amount, refund_amount, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(uuid.uuid4()), appointment_id, patient_id, refund["tier"], refund["base_fee_amount"],
             refund["refund_percent"], refund["cancellation_fee_amount"], refund["refund_amount"], refund["reason"]),
        )
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            pass  # one row per appointment: keep the first
        else:
            raise
    cursor.execute("SELECT * FROM refund_ledger WHERE appointment_id = %s", (appointment_id,))
    return cursor.fetchone()


def view(row):
    if not row:
        return None
    return {
        "refund_id": row["refund_id"],
        "appointment_id": row["appointment_id"],
        "tier": row["tier"],
        "reason": row["reason"],
        "base_fee_amount": row["base_fee_amount"],
        "refund_percent": row["refund_percent"],
        "cancellation_fee_amount": row["cancellation_fee_amount"],
        "refund_amount": row["refund_amount"],
        "created_at": row["created_at"],
    }
