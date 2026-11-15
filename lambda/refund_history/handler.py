"""Refund History Lambda (patient role, architecture box 5).

    GET /patients/me/refunds?page=1&page_size=20

The caller's refund_ledger rows, newest first (index
idx_refund_ledger_patient_created), with the appointment each one belongs to,
plus the totals over all of them.
"""

from medislot_common import appointments, config, db, logs, profiles, refunds
from medislot_common.http import APIError, api_handler, identity, query
from medislot_common.validation import integer


def history(event):
    caller = identity(event, "patient")
    params = query(event)
    unknown = sorted(set(params) - {"page", "page_size"})
    if unknown:
        raise APIError(400, "Unsupported query parameters", {"parameters": unknown})
    default_size = config.config_int("refunds/default-page-size")
    max_size = config.config_int("refunds/max-page-size")
    page = integer(params.get("page"), "page", minimum=1) or 1
    size = integer(params.get("page_size"), "page_size", minimum=1, maximum=max_size) or default_size

    with db.connection().cursor() as cur:
        patient = profiles.require_patient(cur, caller.user_id)
        cur.execute(
            """SELECT COUNT(*) AS refunds, COALESCE(SUM(refund_amount), 0) AS refunded,
                      COALESCE(SUM(cancellation_fee_amount), 0) AS deducted
               FROM refund_ledger WHERE patient_id = %s""",
            (patient["patient_id"],),
        )
        totals = cur.fetchone()
        cur.execute(
            """SELECT r.* FROM refund_ledger r WHERE r.patient_id = %s
               ORDER BY r.created_at DESC, r.refund_id LIMIT %s OFFSET %s""",
            (patient["patient_id"], size + 1, (page - 1) * size),
        )
        rows = cur.fetchall()
        has_more = len(rows) > size
        rows = rows[:size]
        details = {}
        if rows:
            ids = [row["appointment_id"] for row in rows]
            cur.execute(appointments.VIEW_SQL + f" WHERE a.appointment_id IN ({', '.join(['%s'] * len(ids))})", ids)
            details = {row["appointment_id"]: appointments.view(row) for row in cur.fetchall()}

    result = []
    for row in rows:
        entry = refunds.view(row)
        entry["appointment"] = details.get(row["appointment_id"])
        result.append(entry)
    logs.info("refund history read", page=page, pageSize=size, count=len(result))
    return 200, {
        "refunds": result,
        "page": page,
        "page_size": size,
        "has_more": has_more,
        "totals": {
            "refunds": int(totals["refunds"]),
            "refunded": totals["refunded"],
            "deducted": totals["deducted"],
        },
    }


lambda_handler = api_handler({"GET /patients/me/refunds": history})
