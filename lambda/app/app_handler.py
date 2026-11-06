"""
MediSlot application Lambda - Doctor domain APIs.

Handler configured in CloudFormation: app_handler.lambda_handler

Uses:
- RDS MySQL for doctor profiles, consultation types, weekly availability,
  blackout dates, and appointments.
- S3 pre-signed PUT URLs for doctor profile photos.
- API Gateway HTTP API Lambda-authorizer context for identity and role.

The authenticated doctor ID is ALWAYS taken from the authorizer context.
Never accept doctor_id/user_id from a request body, query string, or header.
"""

import json
import logging
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ["DB_NAME"]
DB_USERNAME_PARAMETER = os.environ["DB_USERNAME_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]
DOCTOR_FILES_BUCKET = os.environ["DOCTOR_FILES_BUCKET"]

AWS_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"total_max_attempts": 2},
)
SSM = boto3.client("ssm", region_name=REGION, config=AWS_CONFIG)
S3 = boto3.client("s3", region_name=REGION, config=AWS_CONFIG)
DDB = boto3.resource("dynamodb", region_name=REGION, config=AWS_CONFIG)
SLOTS = DDB.Table(os.environ["SLOTS_TABLE"])
SEATS = DDB.Table(os.environ["SEATS_TABLE"])
WAITLIST = DDB.Table(os.environ["WAITLIST_TABLE"])
HOLD_OFFERS = DDB.Table(os.environ["HOLD_OFFERS_TABLE"])
IDEMPOTENCY = DDB.Table(os.environ["IDEMPOTENCY_TABLE"])

_db_credentials = None

PROFILE_FIELDS = {
    "first_name": (80, True),
    "last_name": (80, True),
    "phone": (25, False),
    "city": (100, True),
    "state": (100, False),
    "country": (100, True),
    "qualifications": (255, False),
    "years_experience": (None, False),
    "languages": (100, False),
    "licence_number": (50, False),
    "bio": (None, False),
    "hospital_id": (None, False),
    "timezone": (64, True),
}
CONSULTATION_NAMES = {
    "Quick Consult",
    "Standard Consult",
    "Detailed Consult",
    "Health Camp Session",
}
PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class APIError(Exception):
    def __init__(self, status, message, details=None):
        self.status = status
        self.message = message
        self.details = details


def json_default(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, timedelta):
        # MySQL TIME columns: always HH:MM:SS (str() would give "9:00:00").
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return str(value)


def response(status, payload):
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    }
    if status == 204:
        # A 204 response must not carry a body.
        del headers["Content-Type"]
        return {"statusCode": 204, "headers": headers, "body": ""}
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(payload, default=json_default),
    }


def get_db_credentials():
    global _db_credentials
    if _db_credentials is None:
        values = SSM.get_parameters(
            Names=[DB_USERNAME_PARAMETER, DB_PASSWORD_PARAMETER],
            WithDecryption=True,
        )
        found = {p["Name"]: p["Value"] for p in values.get("Parameters", [])}
        missing = [
            name for name in (DB_USERNAME_PARAMETER, DB_PASSWORD_PARAMETER)
            if name not in found
        ]
        if missing:
            raise RuntimeError("Database credential parameter is missing")
        _db_credentials = {
            "user": found[DB_USERNAME_PARAMETER],
            "password": found[DB_PASSWORD_PARAMETER],
        }
    return _db_credentials


def db_connection():
    credentials = get_db_credentials()
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=credentials["user"],
        password=credentials["password"],
        database=DB_NAME,
        # Encrypts traffic to RDS. The certificate is not verified because no
        # CA bundle is packaged with the function (same as the auth Lambda).
        ssl={"check_hostname": False},
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8,
        autocommit=False,
    )


def parse_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raise APIError(400, "Base64 request bodies are not supported for this endpoint")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise APIError(400, "Request body must be valid JSON")
    if not isinstance(value, dict):
        raise APIError(400, "Request body must be a JSON object")
    return value


def get_identity(event):
    """Read identity only from API Gateway's Lambda authorizer context."""
    rc = event.get("requestContext") or {}
    auth = rc.get("authorizer") or {}
    context = auth.get("lambda") or auth.get("claims") or auth
    doctor_id = (
        context.get("userId")
        or context.get("user_id")
        or context.get("principalId")
        or auth.get("principalId")
    )
    role = context.get("role")
    if not doctor_id or not role:
        raise APIError(401, "Missing authenticated identity")
    if role != "doctor":
        raise APIError(403, "Doctor access required")
    return str(doctor_id)


def path_and_method(event):
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = event.get("rawPath") or event.get("path") or "/"
    path = re.sub(r"/+", "/", path.rstrip("/") or "/")
    return method, path


def clean_text(value, field, max_length, required=False):
    if value is None:
        if required:
            raise APIError(400, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise APIError(400, f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise APIError(400, f"{field} is required")
    if max_length and len(value) > max_length:
        raise APIError(400, f"{field} must be at most {max_length} characters")
    return value or None


def validate_profile(body, partial):
    unknown = set(body) - set(PROFILE_FIELDS)
    if unknown:
        raise APIError(400, "Unsupported profile fields", {"fields": sorted(unknown)})

    result = {}
    for field, (max_length, required) in PROFILE_FIELDS.items():
        if field not in body:
            if not partial and required:
                raise APIError(400, f"{field} is required")
            continue

        value = body[field]
        if field == "years_experience":
            if value is None or value == "":
                result[field] = None
            elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 80:
                raise APIError(400, "years_experience must be an integer from 0 to 80")
            else:
                result[field] = value
        elif field == "hospital_id":
            if value in (None, ""):
                result[field] = None
            elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise APIError(400, "hospital_id must be a positive integer")
            else:
                result[field] = value
        elif field == "timezone":
            zone = clean_text(value, field, max_length, required=True)
            if zone:
                try:
                    ZoneInfo(zone)
                except ZoneInfoNotFoundError:
                    raise APIError(400, "timezone must be a valid IANA timezone")
            result[field] = zone
        else:
            # A field that is present must satisfy its own required flag,
            # even on a partial update (sending "" must not blank a NOT NULL column).
            result[field] = clean_text(value, field, max_length, required=required)
    return result


def doctor_profile(user_id):
    sql = """
        SELECT d.doctor_id, d.user_id, u.full_name, u.email,
               d.hospital_id, h.name AS hospital_name,
               d.specialisation_id, d.licence_number,
               d.first_name, d.last_name, d.phone,
               d.city, d.state, d.country, d.qualifications,
               d.years_experience, d.languages, d.bio, d.profile_photo_s3_key,
               d.timezone, d.is_active, d.created_at, d.updated_at
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE d.user_id = %s AND u.is_active = 1
    """
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            return cur.fetchone()


def resolve_doctor_id(user_id):
    row = doctor_profile(user_id)
    if not row:
        raise APIError(404, "Complete your doctor profile before using this endpoint")
    return row["doctor_id"]


def get_profile(user_id):
    row = doctor_profile(user_id)
    if not row:
        raise APIError(404, "Doctor profile not found. Create it with PUT /doctors/me.")
    return {"profile": row}


def save_profile(user_id, body):
    values = validate_profile(body, partial=True)
    if not values:
        raise APIError(400, "Provide at least one profile field")

    existing = doctor_profile(user_id)
    if existing:
        # Licence number is immutable after profile creation.
        if "licence_number" in values and values["licence_number"] != existing["licence_number"]:
            raise APIError(400, "licence_number cannot be changed after profile creation")
        values.pop("licence_number", None)
        if not values:
            return 200, {"message": "Doctor profile unchanged", "profile": existing}
        assignments = ", ".join(f"{key} = %s" for key in values)
        params = list(values.values()) + [existing["doctor_id"]]
        sql = f"UPDATE doctors SET {assignments} WHERE doctor_id = %s"
        with db_connection() as conn:
            with conn.cursor() as cur:
                if values.get("hospital_id") is not None:
                    cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s",
                                (values["hospital_id"],))
                    if not cur.fetchone():
                        raise APIError(400, "hospital_id does not exist")
                cur.execute(sql, params)
            conn.commit()
        return 200, {"message": "Doctor profile updated", "profile": doctor_profile(user_id)}

    required = {"first_name", "last_name", "city", "licence_number"}
    missing = sorted(required - set(values))
    if missing:
        raise APIError(400, "Required fields missing for profile creation", {"fields": missing})

    values.setdefault("country", "India")
    values.setdefault("timezone", "Asia/Kolkata")
    doctor_id = str(uuid.uuid4())
    columns = ["doctor_id", "user_id"] + list(values.keys())
    params = [doctor_id, user_id] + list(values.values())
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO doctors ({', '.join(columns)}) VALUES ({placeholders})"

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id=%s AND role='doctor' AND is_active=1",
                        (user_id,))
            if not cur.fetchone():
                raise APIError(403, "Authenticated user is not an active doctor")
            if values.get("hospital_id") is not None:
                cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s",
                            (values["hospital_id"],))
                if not cur.fetchone():
                    raise APIError(400, "hospital_id does not exist")
            cur.execute(sql, params)
        conn.commit()
    return 201, {"message": "Doctor profile created", "profile": doctor_profile(user_id)}


def create_photo_upload(doctor_id, body):
    content_type = body.get("content_type")
    if content_type not in PHOTO_TYPES:
        raise APIError(400, "content_type must be image/jpeg, image/png, or image/webp")

    filename = body.get("filename", "profile")
    if not isinstance(filename, str) or len(filename) > 120:
        raise APIError(400, "filename must be a string of at most 120 characters")

    key = f"doctor-profiles/{doctor_id}/{uuid.uuid4().hex}{PHOTO_TYPES[content_type]}"
    try:
        upload_url = S3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": DOCTOR_FILES_BUCKET,
                "Key": key,
                "ContentType": content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=300,
        )
    except ClientError:
        logger.exception("Could not create profile photo upload URL")
        raise APIError(502, "Could not create profile photo upload URL")

    # Store the key so the profile can refer to the uploaded object.
    # The client must PUT the file to upload_url using the same Content-Type.
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE doctors SET profile_photo_s3_key=%s WHERE doctor_id=%s",
                (key, doctor_id),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT doctor_id FROM doctors WHERE doctor_id=%s", (doctor_id,))
                if not cur.fetchone():
                    raise APIError(404, "Create the doctor profile before uploading a photo")
        conn.commit()

    return {
        "message": "Upload URL created. Upload the image with an HTTP PUT request.",
        "upload_url": upload_url,
        "method": "PUT",
        "headers": {"Content-Type": content_type, "x-amz-server-side-encryption": "AES256"},
        "s3_key": key,
        "expires_in_seconds": 300,
    }


REGULAR_DURATIONS = (15, 30, 60)


def check_consultation_rules(name, duration, capacity):
    """Mirror of the chk_consultation_capacity CHECK constraint in the schema.

    Checked here so a bad combination returns a clear 400 instead of failing
    inside MySQL.
    """
    if name == "Health Camp Session":
        if duration != 60:
            raise APIError(400, "Health Camp Session duration must be 60 minutes")
        if not 2 <= capacity <= 10:
            raise APIError(400, "Health Camp Session capacity must be between 2 and 10")
    else:
        if duration not in REGULAR_DURATIONS:
            raise APIError(
                400,
                "Quick, Standard, and Detailed Consult duration must be 15, 30, or 60 minutes",
            )
        if capacity != 1:
            raise APIError(400, "Quick, Standard, and Detailed Consult capacity must be 1")


def validate_consultation(body, partial=False):
    allowed = {"name", "duration_minutes", "fee", "capacity", "description"}
    unknown = set(body) - allowed
    if unknown:
        raise APIError(400, "Unsupported consultation fields", {"fields": sorted(unknown)})
    result = {}

    if "name" in body:
        name = clean_text(body["name"], "name", 40, True)
        if name not in CONSULTATION_NAMES:
            raise APIError(400, "Invalid name",
                           {"allowed": sorted(CONSULTATION_NAMES)})
        result["name"] = name
    elif not partial:
        raise APIError(400, "name is required")

    if "duration_minutes" in body:
        v = body["duration_minutes"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 480:
            raise APIError(400, "duration_minutes must be an integer from 1 to 480")
        result["duration_minutes"] = v
    elif not partial:
        raise APIError(400, "duration_minutes is required")

    if "fee" in body:
        try:
            fee = Decimal(str(body["fee"]))
        except (InvalidOperation, ValueError):
            raise APIError(400, "fee must be a valid number")
        if not fee.is_finite():
            raise APIError(400, "fee must be a valid number")
        fee = fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if fee < 0 or fee > Decimal("99999999.99"):
            raise APIError(400, "fee must be between 0 and 99999999.99")
        result["fee"] = fee
    elif not partial:
        raise APIError(400, "fee is required")

    if "capacity" in body:
        v = body["capacity"]
        if isinstance(v, bool) or not isinstance(v, int):
            raise APIError(400, "capacity must be an integer")
        result["capacity"] = v
    elif not partial:
        result["capacity"] = 1

    if "description" in body:
        result["description"] = clean_text(body["description"], "description", 500)
    elif not partial:
        result["description"] = None

    if not partial:
        # Every rule input is present on create. Partial updates are checked
        # against the stored row in update_consultation().
        check_consultation_rules(
            result["name"], result["duration_minutes"], result["capacity"]
        )
    return result


def consultation_types(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT consultation_type_id, name, duration_minutes,
                          fee, capacity, description, is_active, created_at, updated_at
                   FROM consultation_types
                   WHERE doctor_id=%s
                   ORDER BY created_at""",
                (doctor_id,),
            )
            rows = cur.fetchall()
    return {"consultation_types": rows}


def create_consultation(doctor_id, body):
    values = validate_consultation(body)
    columns = ["doctor_id"] + list(values)
    params = [doctor_id] + list(values.values())
    sql = f"INSERT INTO consultation_types ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            consultation_id = cur.lastrowid
        conn.commit()
    return {"message": "Consultation type created",
            "consultation_type_id": consultation_id}


def update_consultation(doctor_id, consultation_id, body):
    values = validate_consultation(body, partial=True)
    if not values:
        raise APIError(400, "Provide at least one consultation field")
    sets = ", ".join(f"{k}=%s" for k in values)
    with db_connection() as conn:
        with conn.cursor() as cur:
            # Read the stored row so the name/duration/capacity rules are
            # checked against the final combination, not just the changed field.
            cur.execute(
                """SELECT name, duration_minutes, capacity
                   FROM consultation_types
                   WHERE consultation_type_id=%s AND doctor_id=%s
                   FOR UPDATE""",
                (consultation_id, doctor_id),
            )
            current = cur.fetchone()
            if not current:
                raise APIError(404, "Consultation type not found")
            merged = {**current, **values}
            check_consultation_rules(
                merged["name"],
                int(merged["duration_minutes"]),
                int(merged["capacity"]),
            )
            cur.execute(
                f"UPDATE consultation_types SET {sets} WHERE consultation_type_id=%s AND doctor_id=%s",
                list(values.values()) + [consultation_id, doctor_id],
            )
        conn.commit()
    return {"message": "Consultation type updated"}


def delete_consultation(doctor_id, consultation_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE consultation_types SET is_active=0
                   WHERE consultation_type_id=%s AND doctor_id=%s""",
                (consultation_id, doctor_id),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id=%s AND doctor_id=%s",
                    (consultation_id, doctor_id),
                )
                if not cur.fetchone():
                    raise APIError(404, "Consultation type not found")
            # A deactivated consultation type must stop appearing in the
            # doctor's weekly availability.
            cur.execute(
                """UPDATE availability_patterns SET is_active=0
                   WHERE consultation_type_id=%s AND doctor_id=%s AND is_active=1""",
                (consultation_id, doctor_id),
            )
        conn.commit()
    return {"message": "Consultation type deactivated"}


def parse_iso_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIError(400, f"{field} must use YYYY-MM-DD format")


def parse_clock(value, field):
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIError(400, f"{field} must use HH:MM or HH:MM:SS format")


def get_availability(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT availability_id, consultation_type_id, day_of_week,
                          start_time, end_time, effective_from, effective_until,
                          is_active, created_at, updated_at
                   FROM availability_patterns
                   WHERE doctor_id=%s AND is_active=1
                   ORDER BY day_of_week, start_time""",
                (doctor_id,),
            )
            rows = cur.fetchall()
    return {"availability_patterns": rows}


def replace_availability(doctor_id, body):
    patterns = body.get("patterns")
    if not isinstance(patterns, list):
        raise APIError(400, "patterns must be a JSON array")
    if len(patterns) > 100:
        raise APIError(400, "A maximum of 100 weekly patterns is allowed")

    validated = []
    for index, p in enumerate(patterns):
        if not isinstance(p, dict):
            raise APIError(400, f"patterns[{index}] must be an object")
        allowed = {"consultation_type_id", "day_of_week", "start_time",
                   "end_time", "effective_from", "effective_until"}
        if set(p) - allowed:
            raise APIError(400, f"patterns[{index}] contains unsupported fields")
        try:
            day = p["day_of_week"]
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7:
                raise ValueError()
            start = parse_clock(p["start_time"], "start_time")
            end = parse_clock(p["end_time"], "end_time")
            if end <= start:
                raise ValueError()
            effective_from = parse_iso_date(p["effective_from"], "effective_from")
            effective_until = (
                parse_iso_date(p["effective_until"], "effective_until")
                if p.get("effective_until") else None
            )
            if effective_until and effective_until < effective_from:
                raise ValueError()
            consultation_id = int(p["consultation_type_id"])
            if consultation_id <= 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise APIError(400, f"patterns[{index}] has invalid or missing values")
        validated.append((str(uuid.uuid4()), doctor_id, consultation_id, day,
                          start, end, effective_from, effective_until))

    with db_connection() as conn:
        with conn.cursor() as cur:
            for row in validated:
                cur.execute(
                    "SELECT consultation_type_id FROM consultation_types "
                    "WHERE consultation_type_id=%s AND doctor_id=%s AND is_active=1",
                    (row[2], doctor_id),
                )
                if not cur.fetchone():
                    raise APIError(400, "Each consultation_type_id must belong to this doctor and be active")
            cur.execute(
                "UPDATE availability_patterns SET is_active=0 WHERE doctor_id=%s AND is_active=1",
                (doctor_id,),
            )
            if validated:
                cur.executemany(
                    """INSERT INTO availability_patterns
                       (availability_id, doctor_id, consultation_type_id, day_of_week,
                        start_time, end_time, effective_from, effective_until)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    validated,
                )
        conn.commit()
    return {"message": "Weekly availability replaced",
            "patterns_saved": len(validated)}


def create_blackout(doctor_id, body):
    unknown = set(body) - {"date", "reason"}
    if unknown:
        raise APIError(400, "Unsupported blackout fields", {"fields": sorted(unknown)})
    blackout_date = parse_iso_date(body.get("date"), "date")
    reason = clean_text(body.get("reason"), "reason", 255)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO blackout_dates (doctor_id, `date`, reason)
                   VALUES (%s,%s,%s)""",
                (doctor_id, blackout_date, reason),
            )
            blackout_id = cur.lastrowid
        conn.commit()
    return {"message": "Blackout date created", "blackout_id": blackout_id}


def list_blackouts(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT blackout_id, `date`, reason, created_at
                   FROM blackout_dates WHERE doctor_id=%s ORDER BY `date`""",
                (doctor_id,),
            )
            rows = cur.fetchall()
    return {"blackout_dates": rows}


def delete_blackout(doctor_id, blackout_id):
    try:
        blackout_id = int(blackout_id)
    except (TypeError, ValueError):
        raise APIError(400, "blackout_id must be an integer")
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM blackout_dates WHERE blackout_id=%s AND doctor_id=%s",
                (blackout_id, doctor_id),
            )
            if cur.rowcount == 0:
                raise APIError(404, "Blackout date not found")
        conn.commit()
    return {"message": "Blackout date deleted"}


def schedule_day(doctor_id, query):
    day_text = query.get("date")
    if not day_text:
        raise APIError(400, "date query parameter is required (YYYY-MM-DD)")
    requested_day = parse_iso_date(day_text, "date")
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT timezone FROM doctors WHERE doctor_id=%s AND is_active=1", (doctor_id,))
            profile = cur.fetchone()
    if not profile:
        raise APIError(404, "Doctor profile not found")
    zone_name = profile["timezone"] or "Asia/Kolkata"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise APIError(500, "Doctor timezone stored in database is invalid")

    local_start = datetime.combine(requested_day, time.min, tzinfo=zone)
    local_end = datetime.combine(requested_day + timedelta(days=1), time.min, tzinfo=zone)
    # MySQL slot_start_utc/slot_end_utc are stored as UTC-naive DATETIME.
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.appointment_id, a.patient_id, a.consultation_type_id,
                          a.slot_id, a.slot_start_utc, a.slot_end_utc,
                          a.consultation_fee, a.status, p_user.full_name AS patient_name,
                          p.phone AS patient_phone, ct.name AS consultation_name
                   FROM appointments a
                   JOIN patients p ON p.patient_id=a.patient_id
                   JOIN users p_user ON p_user.user_id=p.user_id
                   JOIN consultation_types ct ON ct.consultation_type_id=a.consultation_type_id
                   WHERE a.doctor_id=%s
                     AND a.slot_start_utc >= %s
                     AND a.slot_start_utc < %s
                   ORDER BY a.slot_start_utc""",
                (doctor_id, utc_start, utc_end),
            )
            appointments = cur.fetchall()

    return {
        "date": requested_day.isoformat(),
        "timezone": zone_name,
        "appointments": appointments,
        "count": len(appointments),
    }


# ---------------------------------------------------------------------------
# Patient and public discovery APIs
# ---------------------------------------------------------------------------
PATIENT_FIELDS = {
    "date_of_birth": (10, False), "gender": (10, False), "phone": (20, False),
    "address": (255, False), "city": (100, False),
    "emergency_contact_name": (100, False), "emergency_contact_phone": (20, False),
}


def get_actor(event, required_role=None):
    rc = event.get("requestContext") or {}
    auth = rc.get("authorizer") or {}
    ctx = auth.get("lambda") or auth.get("claims") or auth
    uid = ctx.get("userId") or ctx.get("user_id") or ctx.get("principalId") or auth.get("principalId")
    role = ctx.get("role")
    if not uid or not role:
        raise APIError(401, "Missing authenticated identity")
    if required_role and role != required_role:
        raise APIError(403, f"{required_role.title()} access required")
    if role not in ("doctor", "patient"):
        raise APIError(403, "Unsupported account role")
    return str(uid), role


def patient_profile(patient_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT p.patient_id, u.full_name, u.email, p.date_of_birth,
                p.gender, p.phone, p.address, p.city, p.emergency_contact_name,
                p.emergency_contact_phone, p.created_at, p.updated_at
                FROM patients p JOIN users u ON u.user_id=p.user_id
                WHERE p.user_id=%s AND u.is_active=1""", (patient_id,))
            return cur.fetchone()


def get_patient_profile(patient_id):
    row = patient_profile(patient_id)
    if not row:
        raise APIError(404, "Patient profile not found")
    return {"profile": row}



def resolve_patient_id(user_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM patients WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    if not row:
        raise APIError(404, "Complete your patient profile before using this endpoint")
    return row["patient_id"]


def save_patient_profile(patient_id, body):
    unknown = set(body) - set(PATIENT_FIELDS)
    if unknown:
        raise APIError(400, "Unsupported patient profile fields", {"fields": sorted(unknown)})
    values = {}
    for key, (limit, _) in PATIENT_FIELDS.items():
        if key not in body:
            continue
        value = body[key]
        if key == "date_of_birth":
            if value in (None, ""):
                values[key] = None
            else:
                try:
                    values[key] = date.fromisoformat(value).isoformat()
                except (TypeError, ValueError):
                    raise APIError(400, "date_of_birth must use YYYY-MM-DD")
        else:
            values[key] = clean_text(value, key, limit)
    if not values:
        raise APIError(400, "Provide at least one patient profile field")
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT patient_id FROM patients WHERE user_id=%s", (patient_id,))
            patient_row = cur.fetchone()
            if not patient_row:
                actual_patient_id = str(uuid.uuid4())
                cur.execute("INSERT INTO patients (patient_id, user_id) VALUES (%s,%s)",
                            (actual_patient_id, patient_id))
            else:
                actual_patient_id = patient_row["patient_id"]
            assignments = ", ".join(f"{k}=%s" for k in values)
            cur.execute(f"UPDATE patients SET {assignments} WHERE patient_id=%s",
                        list(values.values()) + [actual_patient_id])
        conn.commit()
    return {"message": "Patient profile saved", "profile": patient_profile(patient_id)}


def delete_patient_profile(patient_id):
    # Account deactivation preserves appointment/refund history and referential integrity.
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_active=0 WHERE user_id=%s AND role='patient'", (patient_id,))
            if cur.rowcount != 1:
                raise APIError(404, "Patient account not found")
        conn.commit()
    return {"message": "Patient account deactivated"}


def discover_doctors(query):
    terms = []
    params = []
    if query.get("city"):
        terms.append("d.city=%s"); params.append(clean_text(query["city"], "city", 100, True))
    if query.get("specialisation_id"):
        try: sid = int(query["specialisation_id"])
        except (TypeError, ValueError): raise APIError(400, "specialisation_id must be an integer")
        terms.append("d.specialisation_id=%s"); params.append(sid)
    where = " AND ".join(["u.is_active=1", "d.is_active=1"] + terms)
    limit = min(max(int(query.get("limit", 25)), 1), 100)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT d.doctor_id, u.full_name, d.hospital_id, h.name AS hospital_name,
                d.city AS city, h.address AS hospital_address,
                d.specialisation_id, s.name AS specialisation, d.licence_number,
                d.years_experience, d.qualifications, d.languages, d.profile_photo_s3_key
                FROM doctors d JOIN users u ON u.user_id=d.user_id
                LEFT JOIN hospitals h ON h.hospital_id=d.hospital_id
                LEFT JOIN specialisations s ON s.specialisation_id=d.specialisation_id
                WHERE {where} ORDER BY u.full_name LIMIT %s""", params + [limit])
            return {"doctors": cur.fetchall()}


def doctor_public_profile(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT d.doctor_id, u.full_name, d.hospital_id,
                h.name AS hospital_name, d.city, h.address AS hospital_address,
                d.specialisation_id, s.name AS specialisation, d.licence_number,
                d.years_experience, d.qualifications, d.languages, d.profile_photo_s3_key
                FROM doctors d JOIN users u ON u.user_id=d.user_id
                LEFT JOIN hospitals h ON h.hospital_id=d.hospital_id
                LEFT JOIN specialisations s ON s.specialisation_id=d.specialisation_id
                WHERE d.doctor_id=%s AND u.is_active=1""", (doctor_id,))
            row = cur.fetchone()
    if not row:
        raise APIError(404, "Doctor not found")
    return {"doctor": row}


def doctor_slots(doctor_id, query):
    start = query.get("from")
    end = query.get("to")
    if not start or not end:
        raise APIError(400, "from and to query parameters are required")
    try:
        datetime.fromisoformat(start.replace("Z", "+00:00"))
        datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        raise APIError(400, "from and to must be ISO-8601 timestamps")
    if end <= start:
        raise APIError(400, "to must be later than from")
    result = SLOTS.query(
        IndexName="GSI_Doctor_Slots",
        KeyConditionExpression=Key("doctorId").eq(doctor_id)
            & Key("startTime").between(start, end),
    )
    return {"slots": result.get("Items", [])}


def patient_appointments(patient_id, appointment_id=None):
    sql = """SELECT appointment_id, patient_id, doctor_id, slot_id, consultation_type_id,
        availability_id, slot_start_utc, slot_end_utc, consultation_fee, status,
        reschedule_count, version, created_at, updated_at
        FROM appointments WHERE patient_id=%s"""
    params = [patient_id]
    if appointment_id:
        sql += " AND appointment_id=%s"; params.append(appointment_id)
    sql += " ORDER BY slot_start_utc DESC"
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params); rows = cur.fetchall()
    if appointment_id and not rows:
        raise APIError(404, "Appointment not found")
    return {"appointments": rows} if not appointment_id else {"appointment": rows[0]}


def join_waitlist(patient_id, slot_id):
    slot = SLOTS.get_item(Key={"slotId": slot_id}, ConsistentRead=True).get("Item")
    if not slot:
        raise APIError(404, "Slot not found")
    if slot.get("status") not in ("OPEN", "FULL"):
        raise APIError(409, "Slot is not accepting waitlist entries")
    now = int(datetime.now(timezone.utc).timestamp())
    # queueOrder is allocated atomically on the slot item; patientId is the SK.
    try:
        updated = SLOTS.update_item(
            Key={"slotId": slot_id},
            UpdateExpression="SET queueOrder = if_not_exists(queueOrder, :zero) + :one",
            ConditionExpression="attribute_exists(slotId)",
            ExpressionAttributeValues={":zero": 0, ":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        queue_order = int(updated["Attributes"]["queueOrder"])
        WAITLIST.put_item(Item={"slotId": slot_id, "patientId": patient_id,
            "queueOrder": queue_order, "status": "WAITING", "createdAt": now},
            ConditionExpression="attribute_not_exists(patientId)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise APIError(409, "Already on the waitlist for this slot")
        raise
    return {"message": "Added to waitlist", "slot_id": slot_id, "queue_order": queue_order}


def patient_waitlists(patient_id):
    result = WAITLIST.query(IndexName="GSI_Patient_Waitlists",
        KeyConditionExpression=Key("patientId").eq(patient_id))
    return {"waitlists": result.get("Items", [])}


def leave_waitlist(patient_id, slot_id):
    WAITLIST.delete_item(Key={"slotId": slot_id, "patientId": patient_id},
        ConditionExpression="attribute_exists(patientId)")
    return {"message": "Removed from waitlist"}


def accept_waitlist_offer(patient_id, offer_id):
    offer = HOLD_OFFERS.get_item(Key={"offerId": offer_id}, ConsistentRead=True).get("Item")
    if not offer or offer.get("patientId") != patient_id:
        raise APIError(404, "Waitlist offer not found")
    if offer.get("status") != "LIVE" or int(offer.get("expiresAt", 0)) <= int(datetime.now(timezone.utc).timestamp()):
        raise APIError(409, "Waitlist offer has expired")
    raise APIError(501, "Offer acceptance requires the configured booking transaction workflow; no partial booking was made")


def lambda_handler(event, context):
    try:
        method, path = path_and_method(event)

        if method == "OPTIONS":
            return response(204, {})

        body = parse_body(event)
        query = event.get("queryStringParameters") or {}
        path_params = event.get("pathParameters") or {}
        # Public doctor discovery and slot lookup (route authorization is NONE in API Gateway).
        public_doctor = re.fullmatch(r"/doctors/([0-9a-fA-F-]{36})", path)
        public_slots = re.fullmatch(r"/doctors/([0-9a-fA-F-]{36})/slots", path)
        if path == "/doctors" and method == "GET":
            return response(200, discover_doctors(query))
        if public_doctor and method == "GET":
            return response(200, doctor_public_profile(public_doctor.group(1)))
        if public_slots and method == "GET":
            return response(200, doctor_slots(public_slots.group(1), query))

        actor_id, actor_role = get_actor(event)

        # Patient domain APIs
        if actor_role == "patient":
            if path == "/patients/me" and method == "GET":
                return response(200, get_patient_profile(actor_id))
            if path == "/patients/me" and method == "PUT":
                return response(200, save_patient_profile(actor_id, body))
            if path == "/patients/me" and method == "DELETE":
                return response(200, delete_patient_profile(actor_id))
            # Patient IDs in the relational schema are distinct from users.user_id.
            # Resolve only after allowing PUT /patients/me to create the profile.
            patient_id = resolve_patient_id(actor_id)
            if path == "/appointments" and method == "GET":
                return response(200, patient_appointments(patient_id))
            if path == "/appointments" and method == "POST":
                raise APIError(501, "Booking endpoint is registered but requires the final slot-to-appointment transaction mapping; no booking was created")
            appointment_match = re.fullmatch(r"/appointments/([0-9a-fA-F-]{36})", path)
            if appointment_match and method == "GET":
                return response(200, patient_appointments(patient_id, appointment_match.group(1)))
            if appointment_match and method == "PATCH":
                raise APIError(501, "Rescheduling must run through the configured Step Functions saga; no appointment was changed")
            if appointment_match and method == "DELETE":
                raise APIError(501, "Cancellation/refund requires the configured refund transaction and slot-release workflow; no appointment was changed")
            if path == "/patients/me/waitlists" and method == "GET":
                return response(200, patient_waitlists(patient_id))
            waitlist_match = re.fullmatch(r"/slots/([^/]+)/waitlist", path)
            if waitlist_match and method == "POST":
                return response(201, join_waitlist(patient_id, waitlist_match.group(1)))
            if waitlist_match and method == "DELETE":
                return response(200, leave_waitlist(patient_id, waitlist_match.group(1)))
            offer_match = re.fullmatch(r"/waitlist-offers/([0-9a-fA-F-]{36})/accept", path)
            if offer_match and method == "POST":
                return response(200, accept_waitlist_offer(patient_id, offer_match.group(1)))
            if path == "/patients/me/refunds" and method == "GET":
                with db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT refund_id, appointment_id, tier, base_fee_amount, refund_percent, cancellation_fee_amount, refund_amount, reason, created_at FROM refund_ledger WHERE patient_id=%s ORDER BY created_at DESC", (patient_id,))
                        return response(200, {"refunds": cur.fetchall()})
            raise APIError(404, "Patient route not found")

        if actor_role != "doctor":
            raise APIError(403, "Unsupported account role")
        # Doctor profile can be created before a doctors row exists.
        if path == "/doctors/me" and method == "GET":
            return response(200, get_profile(actor_id))
        if path == "/doctors/me" and method in ("PUT", "POST"):
            status, result = save_profile(actor_id, body)
            return response(status, result)

        doctor_id = resolve_doctor_id(actor_id)

        # Profile photo upload URL
        if path == "/doctors/me/photo" and method == "POST":
            return response(200, create_photo_upload(doctor_id, body))

        # Consultation type CRUD
        if path == "/doctors/me/consultation-types":
            if method == "GET":
                return response(200, consultation_types(doctor_id))
            if method == "POST":
                return response(201, create_consultation(doctor_id, body))

        consultation_match = re.fullmatch(
            r"/doctors/me/consultation-types/(\\d+)", path
        )
        if consultation_match:
            consultation_id = consultation_match.group(1)
            if method == "PUT":
                return response(200, update_consultation(doctor_id, consultation_id, body))
            if method == "DELETE":
                return response(200, delete_consultation(doctor_id, consultation_id))

        # Weekly availability: GET reads; PUT replaces all active patterns.
        if path == "/doctors/me/availability":
            if method == "GET":
                return response(200, get_availability(doctor_id))
            if method == "PUT":
                return response(200, replace_availability(doctor_id, body))

        # Blackout dates
        if path == "/doctors/me/blackouts":
            if method == "GET":
                return response(200, list_blackouts(doctor_id))
            if method == "POST":
                return response(201, create_blackout(doctor_id, body))

        blackout_match = re.fullmatch(
            r"/doctors/me/blackouts/(\\d+)", path
        )
        if blackout_match and method == "DELETE":
            return response(200, delete_blackout(doctor_id, blackout_match.group(1)))

        # Doctor day schedule. The date is interpreted in the doctor's IANA timezone.
        if path == "/doctors/me/schedule" and method == "GET":
            return response(200, schedule_day(doctor_id, query))

        return response(404, {"error": "Route not found", "method": method, "path": path})

    except APIError as exc:
        payload = {"error": exc.message}
        if exc.details:
            payload["details"] = exc.details
        return response(exc.status, payload)
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        if code == 1062:
            return response(409, {"error": "This record already exists"})
        if code in (3819, 4025):
            return response(400, {"error": "The values violate a database rule"})
        if code in (1048, 1264, 1265, 1366, 1406, 1452):
            return response(
                400,
                {"error": "A value is missing, out of range, or refers to a record that does not exist"},
            )
        logger.exception("Database error %s", code)
        if code in (2003, 2006, 2013):
            return response(503, {"error": "Database temporarily unavailable"})
        return response(500, {"error": "Internal server error"})
    except Exception:
        logger.exception("Unhandled MediSlot application error")
        return response(500, {"error": "Internal server error"})