"""Doctor Profile Lambda (doctor role).

    GET /doctors/me   the caller's profile, with a short-lived photo URL
    PUT /doctors/me   update any subset of the profile fields

The doctor is always the caller from the authorizer context. The profile row
is created at registration, so there is no separate create call.

Changing the time zone moves every future unbooked slot to the new zone
(slots are local-time based). It is refused while the doctor has upcoming
confirmed appointments, because those were booked at fixed instants.
"""

import os

from medislot_common import config, db, logs, profiles, slots
from medislot_common.http import APIError, api_handler, identity, parse_body
from medislot_common.validation import reject_unknown, text

UPDATABLE = {
    "first_name", "last_name", "licence_number", "specialisation", "hospital_name", "hospital_address", "hospital_city",
    "years_experience", "qualifications", "languages", "timezone",
}


def photo_url(key):
    if not key:
        return None
    return config.client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["DOCTOR_FILES_BUCKET"], "Key": key},
        ExpiresIn=config.config_int("photos/view-url-seconds"),
    )


def view(row):
    return {
        "doctor_id": row["doctor_id"],
        "user_id": row["user_id"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "email": row["email"],
        "licence_number": row["licence_number"],
        "specialisation_id": row["specialisation_id"],
        "specialisation": row["specialisation"],
        "hospital": (
            {
                "hospital_id": row["hospital_id"],
                "name": row["hospital_name"],
                "address": row["hospital_address"],
                "city": row["hospital_city"],
            }
            if row["hospital_id"]
            else None
        ),
        "years_experience": row["years_experience"],
        "qualifications": row["qualifications"],
        "languages": row["languages"],
        "timezone": row["timezone"],
        "photo_url": photo_url(row["profile_photo_s3_key"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_profile(event):
    caller = identity(event, "doctor")
    with db.connection().cursor() as cur:
        return 200, {"profile": view(profiles.require_doctor(cur, caller.user_id))}


def update_profile(event):
    caller = identity(event, "doctor")
    body = parse_body(event)
    reject_unknown(body, UPDATABLE)
    if not body:
        raise APIError(400, "Provide at least one profile field")

    with db.transaction() as cur:
        current = profiles.require_doctor(cur, caller.user_id)
        if "licence_number" in body:
            licence = text(body["licence_number"], "licence_number", 50, required=True)
            if licence != current["licence_number"]:
                raise APIError(400, "licence_number cannot be changed")
        names = profiles.name_values(body, required=False)
        if names:
            cur.execute(
                f"UPDATE users SET {', '.join(f'{column} = %s' for column in names)} WHERE user_id = %s",
                (*names.values(), caller.user_id),
            )
        values = profiles.doctor_values(cur, body, creating=False)
        timezone_changed = "timezone" in values and values["timezone"] != current["timezone"]
        if timezone_changed:
            cur.execute(
                """SELECT COUNT(*) AS n FROM appointments
                   WHERE doctor_id = %s AND status = 'CONFIRMED' AND slot_start_utc > UTC_TIMESTAMP()""",
                (current["doctor_id"],),
            )
            if cur.fetchone()["n"]:
                raise APIError(409, "Time zone cannot change while you have upcoming confirmed appointments")
        if values:
            assignments = ", ".join(f"{column} = %s" for column in values)
            cur.execute(
                f"UPDATE doctors SET {assignments} WHERE doctor_id = %s",
                [*values.values(), current["doctor_id"]],
            )

    logs.info("doctor profile updated", doctorId=current["doctor_id"], fields=sorted(body))
    result = {}
    if timezone_changed:
        with db.connection().cursor() as cur:
            result["slot_sync"] = slots.sync_from_database(
                cur, config.table("SLOTS_TABLE"), current["doctor_id"], slots.now_utc(),
                config.config_int("slots/booking-window-days"),
            )
        logs.info("doctor time zone changed", doctorId=current["doctor_id"], fromTimezone=current["timezone"],
                  toTimezone=values["timezone"], slotSync=result["slot_sync"])
    with db.connection().cursor() as cur:
        result["profile"] = view(profiles.require_doctor(cur, caller.user_id))
    return 200, result


lambda_handler = api_handler(
    {
        "GET /doctors/me": get_profile,
        "PUT /doctors/me": update_profile,
    }
)
