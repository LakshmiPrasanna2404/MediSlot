"""Doctor Search Lambda (patient role).

    GET /doctors?name=&hospital=&specialisation=&city=&page=&page_size=
    GET /doctors/{doctorId}          public profile with consultation types and fees

Filters (any combination, all optional):
    name            doctor's full name starts with this text
    hospital        hospital name starts with this text
    specialisation  exact specialisation name (case-insensitive)
    city            exact hospital city (case-insensitive)

Matching is case-insensitive through the utf8mb4_0900_ai_ci collation, and
every filter is written so MySQL can use an index (prefix LIKE, equality).
docs/data-model.md maps each filter combination to the index it uses.
Pagination: page (from 1) and page_size (SSM default and maximum).
"""

import os

from medislot_common import config, db
from medislot_common.http import APIError, api_handler, identity, path_param, query
from medislot_common.validation import integer, text, uuid_value

SEARCH_SQL = """
    SELECT d.doctor_id, u.full_name, s.name AS specialisation,
           h.name AS hospital_name, h.address AS hospital_address, h.city,
           d.years_experience, d.qualifications, d.languages, d.profile_photo_s3_key,
           (SELECT MIN(ct.fee) FROM consultation_types ct
             WHERE ct.doctor_id = d.doctor_id AND ct.is_active = 1) AS fee_from
    FROM users u
    JOIN doctors d ON d.user_id = u.user_id
    LEFT JOIN specialisations s ON s.specialisation_id = d.specialisation_id
    LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
"""


def _like_prefix(value):
    """Escape LIKE wildcards in user input, then add a trailing %."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _photo_url(key):
    if not key:
        return None
    return config.client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["DOCTOR_FILES_BUCKET"], "Key": key},
        ExpiresIn=config.config_int("photos/view-url-seconds"),
    )


def build_search(params, page, page_size):
    """Return (sql, values). Kept separate so tests can EXPLAIN it."""
    clauses = ["u.role = 'doctor'", "u.is_active = 1"]
    values = []
    name = text(params.get("name"), "name", 120)
    hospital = text(params.get("hospital"), "hospital", 200)
    specialisation = text(params.get("specialisation"), "specialisation", 100)
    city = text(params.get("city"), "city", 100)
    if name:
        clauses.append("u.full_name LIKE %s")
        values.append(_like_prefix(name))
    if hospital:
        clauses.append("h.name LIKE %s")
        values.append(_like_prefix(hospital))
    if specialisation:
        clauses.append("s.name = %s")
        values.append(specialisation)
    if city:
        clauses.append("h.city = %s")
        values.append(city)
    sql = SEARCH_SQL + " WHERE " + " AND ".join(clauses) + " ORDER BY u.full_name, d.doctor_id LIMIT %s OFFSET %s"
    # One extra row tells us whether another page exists.
    values.extend([page_size + 1, (page - 1) * page_size])
    return sql, values


def search(event):
    identity(event, "patient")
    params = query(event)
    allowed = {"name", "hospital", "specialisation", "city", "page", "page_size"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise APIError(400, "Unsupported query parameters", {"parameters": unknown})
    max_size = config.config_int("search/max-page-size")
    page = integer(params.get("page"), "page", 1, 10000) or 1
    page_size = integer(params.get("page_size"), "page_size", 1, max_size) or config.config_int("search/default-page-size")
    sql, values = build_search(params, page, page_size)
    rows = db.fetch_all(sql, values)
    has_more = len(rows) > page_size
    doctors = []
    for row in rows[:page_size]:
        row["photo_url"] = _photo_url(row.pop("profile_photo_s3_key"))
        doctors.append(row)
    return 200, {"doctors": doctors, "page": page, "page_size": page_size, "has_more": has_more}


def public_profile(event):
    identity(event, "patient")
    doctor_id = uuid_value(path_param(event, "doctorId"), "doctorId")
    with db.connection().cursor() as cur:
        cur.execute(
            """SELECT d.doctor_id, u.full_name, s.name AS specialisation,
                      h.name AS hospital_name, h.address AS hospital_address, h.city,
                      d.years_experience, d.qualifications, d.languages, d.timezone,
                      d.profile_photo_s3_key
               FROM doctors d
               JOIN users u ON u.user_id = d.user_id
               LEFT JOIN specialisations s ON s.specialisation_id = d.specialisation_id
               LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
               WHERE d.doctor_id = %s AND u.is_active = 1""",
            (doctor_id,),
        )
        doctor = cur.fetchone()
        if not doctor:
            raise APIError(404, "Doctor not found")
        cur.execute(
            """SELECT consultation_type_id, name, description, duration_minutes, capacity, fee
               FROM consultation_types WHERE doctor_id = %s AND is_active = 1
               ORDER BY duration_minutes, consultation_type_id""",
            (doctor_id,),
        )
        doctor["consultation_types"] = cur.fetchall()
    doctor["photo_url"] = _photo_url(doctor.pop("profile_photo_s3_key"))
    return 200, {"doctor": doctor}


lambda_handler = api_handler(
    {
        "GET /doctors": search,
        "GET /doctors/{doctorId}": public_profile,
    }
)
