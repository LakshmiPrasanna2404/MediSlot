"""Doctor and patient profile helpers shared by auth, doctor and patient Lambdas."""

from . import config
from .http import APIError
from .validation import integer, text, timezone_name

DOCTOR_PROFILE_SQL = """
    SELECT d.doctor_id, d.user_id, u.first_name, u.last_name, u.email, u.is_active,
           d.licence_number, d.years_experience, d.qualifications, d.languages,
           d.timezone, d.profile_photo_s3_key,
           d.specialisation_id, s.name AS specialisation,
           d.hospital_id, h.name AS hospital_name, h.address AS hospital_address,
           h.city AS hospital_city,
           d.created_at, d.updated_at
    FROM doctors d
    JOIN users u ON u.user_id = d.user_id
    LEFT JOIN specialisations s ON s.specialisation_id = d.specialisation_id
    LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
"""

NAME_FIELDS = ("first_name", "last_name")


def name_values(body, required):
    """Validate first_name and last_name (each 1-60 characters) into users columns.

    Registration needs both (required=True); a profile update may send either.
    """
    values = {}
    for field in NAME_FIELDS:
        if required or field in body:
            values[field] = text(body.get(field), field, 60, required=True)
    return values


def display_name(row):
    """'First Last' for logs and messages; never stored."""
    return " ".join(part for part in (row.get("first_name"), row.get("last_name")) if part)


PATIENT_PROFILE_SQL = """
    SELECT p.patient_id, p.user_id, u.first_name, u.last_name, u.email, u.is_active,
           p.date_of_birth, p.gender, p.phone, p.address, p.city,
           p.emergency_contact_name, p.emergency_contact_phone,
           p.created_at, p.updated_at
    FROM patients p
    JOIN users u ON u.user_id = p.user_id
"""

PATIENT_FIELDS = {
    "date_of_birth": None,
    "gender": 10,
    "phone": 20,
    "address": 255,
    "city": 100,
    "emergency_contact_name": 100,
    "emergency_contact_phone": 20,
}

GENDERS = {"female", "male", "other", "undisclosed"}


def resolve_specialisation(cursor, value):
    """Map a specialisation name (or numeric id) from the seeded catalogue to its id."""
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        cursor.execute("SELECT specialisation_id FROM specialisations WHERE specialisation_id = %s", (value,))
    else:
        name = text(value, "specialisation", 100, required=True)
        cursor.execute("SELECT specialisation_id FROM specialisations WHERE name = %s", (name,))
    row = cursor.fetchone()
    if row:
        return row["specialisation_id"]
    cursor.execute("SELECT name FROM specialisations ORDER BY name")
    raise APIError(400, "Unknown specialisation", {"allowed": [r["name"] for r in cursor.fetchall()]})


def find_or_create_hospital(cursor, name, address, city):
    """Return hospital_id for (name, city), creating the hospital if it is new.

    UNIQUE(name, city) makes this safe under concurrency: a racing insert
    lands on the same row through ON DUPLICATE KEY UPDATE.
    """
    name = text(name, "hospital_name", 200, required=True)
    city = text(city, "hospital_city", 100, required=True)
    address = text(address, "hospital_address", 255)
    cursor.execute(
        """
        INSERT INTO hospitals (name, address, city) VALUES (%s, %s, %s) AS new
        ON DUPLICATE KEY UPDATE
            hospital_id = LAST_INSERT_ID(hospitals.hospital_id),
            address = COALESCE(new.address, hospitals.address)
        """,
        (name, address, city),
    )
    return cursor.lastrowid


def doctor_values(cursor, body, creating):
    """Validate doctor profile fields and turn them into doctors-table columns."""
    values = {}
    if "years_experience" in body:
        values["years_experience"] = integer(body["years_experience"], "years_experience", 0, 80)
    if "qualifications" in body:
        values["qualifications"] = text(body["qualifications"], "qualifications", 255)
    if "languages" in body:
        values["languages"] = text(body["languages"], "languages", 100)
    if "timezone" in body:
        values["timezone"] = timezone_name(body["timezone"])
    elif creating:
        values["timezone"] = config.config_value("doctors/default-timezone")
    if "specialisation" in body:
        values["specialisation_id"] = resolve_specialisation(cursor, body["specialisation"])
    hospital_keys = {"hospital_name", "hospital_address", "hospital_city"}
    if hospital_keys & set(body):
        if not body.get("hospital_name"):
            raise APIError(400, "hospital_name is required when setting a hospital")
        values["hospital_id"] = find_or_create_hospital(
            cursor, body.get("hospital_name"), body.get("hospital_address"), body.get("hospital_city")
        )
    return values


def patient_values(body):
    """Validate patient profile fields (names match the patients table)."""
    from .validation import iso_date  # local import keeps module load light

    values = {}
    for field, max_length in PATIENT_FIELDS.items():
        if field not in body:
            continue
        if field == "date_of_birth":
            values[field] = iso_date(body[field], field, required=False)
        elif field == "gender":
            gender = text(body[field], field, 10)
            if gender is not None and gender.lower() not in GENDERS:
                raise APIError(400, "gender must be one of " + ", ".join(sorted(GENDERS)))
            values[field] = gender.lower() if gender else None
        else:
            values[field] = text(body[field], field, max_length)
    return values


def doctor_by_user(cursor, user_id):
    cursor.execute(DOCTOR_PROFILE_SQL + " WHERE d.user_id = %s", (user_id,))
    return cursor.fetchone()


def doctor_by_id(cursor, doctor_id):
    cursor.execute(DOCTOR_PROFILE_SQL + " WHERE d.doctor_id = %s", (doctor_id,))
    return cursor.fetchone()


def patient_by_user(cursor, user_id):
    cursor.execute(PATIENT_PROFILE_SQL + " WHERE p.user_id = %s", (user_id,))
    return cursor.fetchone()


def require_doctor(cursor, user_id):
    row = doctor_by_user(cursor, user_id)
    if not row or not row["is_active"]:
        raise APIError(404, "Doctor profile not found")
    return row


def require_patient(cursor, user_id):
    row = patient_by_user(cursor, user_id)
    if not row or not row["is_active"]:
        raise APIError(404, "Patient profile not found")
    return row
