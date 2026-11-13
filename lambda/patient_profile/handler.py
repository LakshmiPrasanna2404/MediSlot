"""Patient Profile Lambda (patient role).

    GET    /patients/me
    PUT    /patients/me
    DELETE /patients/me

The patient is always the caller from the authorizer context; there is no
route that takes a patient id, so one patient cannot read or change another
patient's profile. The profile row is created at registration.

DELETE is refused while the patient has upcoming confirmed appointments.
A patient with appointment history is deactivated (history stays for audit,
login and refresh stop working); one without history is removed. Either way
the access token used for the request is revoked at once.
"""

import time

from medislot_common import config, db, logs, profiles
from medislot_common.http import APIError, api_handler, identity, parse_body
from medislot_common.validation import reject_unknown

UPDATABLE = {*profiles.NAME_FIELDS, *profiles.PATIENT_FIELDS}


def view(row):
    return {key: row[key] for key in (
        "patient_id", "user_id", "first_name", "last_name", "email", "date_of_birth", "gender", "phone", "address", "city",
        "emergency_contact_name", "emergency_contact_phone", "created_at", "updated_at",
    )}


def get_profile(event):
    caller = identity(event, "patient")
    with db.connection().cursor() as cur:
        return 200, {"profile": view(profiles.require_patient(cur, caller.user_id))}


def update_profile(event):
    caller = identity(event, "patient")
    body = parse_body(event)
    reject_unknown(body, UPDATABLE)
    if not body:
        raise APIError(400, "Provide at least one profile field")
    values = profiles.patient_values(body)
    with db.transaction() as cur:
        current = profiles.require_patient(cur, caller.user_id)
        names = profiles.name_values(body, required=False)
        if names:
            cur.execute(
                f"UPDATE users SET {', '.join(f'{column} = %s' for column in names)} WHERE user_id = %s",
                (*names.values(), caller.user_id),
            )
        if values:
            assignments = ", ".join(f"{column} = %s" for column in values)
            cur.execute(
                f"UPDATE patients SET {assignments} WHERE patient_id = %s",
                [*values.values(), current["patient_id"]],
            )
        logs.info("patient profile updated", patientId=current["patient_id"], fields=sorted(body))
        return 200, {"profile": view(profiles.require_patient(cur, caller.user_id))}


def delete_profile(event):
    caller = identity(event, "patient")
    with db.transaction() as cur:
        current = profiles.require_patient(cur, caller.user_id)
        cur.execute(
            """SELECT
                 SUM(status = 'CONFIRMED' AND slot_start_utc > UTC_TIMESTAMP()) AS upcoming,
                 COUNT(*) AS total
               FROM appointments WHERE patient_id = %s""",
            (current["patient_id"],),
        )
        counts = cur.fetchone()
        if int(counts["upcoming"] or 0) > 0:
            logs.warning("account removal refused", patientId=current["patient_id"], upcoming=int(counts["upcoming"]))
            raise APIError(409, "Cancel your upcoming appointments before deleting your account")
        if int(counts["total"] or 0) > 0:
            cur.execute("UPDATE users SET is_active = 0 WHERE user_id = %s", (caller.user_id,))
            outcome = "deactivated"
        else:
            cur.execute("DELETE FROM users WHERE user_id = %s", (caller.user_id,))  # cascades to patients
            outcome = "deleted"
    config.table("TOKEN_REVOCATIONS_TABLE").put_item(
        Item={"jti": caller.jti, "userId": caller.user_id, "createdAt": int(time.time()), "expiresAt": caller.exp}
    )
    logs.info("patient account removed", patientId=current["patient_id"], outcome=outcome,
              pastAppointments=int(counts["total"] or 0), accessTokenRevoked=caller.jti)
    return 200, {"message": f"Account {outcome}"}


lambda_handler = api_handler(
    {
        "GET /patients/me": get_profile,
        "PUT /patients/me": update_profile,
        "DELETE /patients/me": delete_profile,
    }
)
