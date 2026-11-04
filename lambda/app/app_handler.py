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
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import ClientError

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

_db_credentials = None

PROFILE_FIELDS = {
    "first_name": (80, True),
    "last_name": (80, True),
    "phone": (25, False),
    "city": (100, True),
    "state": (100, False),
    "country": (100, True),
    "qualification": (180, False),
    "experience_years": (None, False),
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
    return str(value)


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
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
        if field == "experience_years":
            if value is None or value == "":
                result[field] = None
            elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 80:
                raise APIError(400, "experience_years must be an integer from 0 to 80")
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
            zone = clean_text(value, field, max_length, required=not partial)
            if zone:
                try:
                    ZoneInfo(zone)
                except ZoneInfoNotFoundError:
                    raise APIError(400, "timezone must be a valid IANA timezone")
            result[field] = zone
        else:
            result[field] = clean_text(
                value, field, max_length, required=(required and not partial)
            )
    return result


def doctor_profile(doctor_id):
    sql = """
        SELECT d.doctor_id, u.full_name, u.email,
               d.hospital_id, h.hospital_name,
               d.first_name, d.last_name, d.phone,
               d.city, d.state, d.country, d.qualification,
               d.experience_years, d.bio, d.profile_photo_s3_key,
               d.timezone, d.is_active, d.created_at, d.updated_at
        FROM doctors d
        JOIN users u ON u.user_id = d.doctor_id
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE d.doctor_id = %s
    """
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (doctor_id,))
            row = cur.fetchone()
    return row


def get_profile(doctor_id):
    row = doctor_profile(doctor_id)
    if not row:
        raise APIError(404, "Doctor profile not found. Create it with PUT /doctors/me.")
    return {"profile": row}


def save_profile(doctor_id, body):
    values = validate_profile(body, partial=True)
    if not values:
        raise APIError(400, "Provide at least one profile field")

    existing = doctor_profile(doctor_id)
    if existing:
        assignments = ", ".join(f"{key} = %s" for key in values)
        params = list(values.values()) + [doctor_id]
        sql = f"UPDATE doctors SET {assignments} WHERE doctor_id = %s"
        with db_connection() as conn:
            with conn.cursor() as cur:
                if "hospital_id" in values and values["hospital_id"] is not None:
                    cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s",
                                (values["hospital_id"],))
                    if not cur.fetchone():
                        raise APIError(400, "hospital_id does not exist")
                cur.execute(sql, params)
            conn.commit()
        return 200, {"message": "Doctor profile updated", "profile": doctor_profile(doctor_id)}

    # First profile creation. doctor_id comes from the authorizer and must
    # already exist as a doctor-role user created by the authentication flow.
    required = {"first_name", "last_name", "city"}
    missing = sorted(required - set(values))
    if missing:
        raise APIError(400, "Required fields missing for profile creation",
                       {"fields": missing})

    values.setdefault("country", "India")
    values.setdefault("timezone", "Asia/Kolkata")
    columns = ["doctor_id"] + list(values.keys())
    params = [doctor_id] + list(values.values())
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO doctors ({', '.join(columns)}) VALUES ({placeholders})"

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id=%s AND role='doctor' AND is_active=1",
                        (doctor_id,))
            if not cur.fetchone():
                raise APIError(403, "Authenticated user is not an active doctor")
            if values.get("hospital_id") is not None:
                cur.execute("SELECT hospital_id FROM hospitals WHERE hospital_id=%s",
                            (values["hospital_id"],))
                if not cur.fetchone():
                    raise APIError(400, "hospital_id does not exist")
            cur.execute(sql, params)
        conn.commit()
    return 201, {"message": "Doctor profile created", "profile": doctor_profile(doctor_id)}


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


def validate_consultation(body, partial=False):
    allowed = {"consultation_name", "duration_minutes", "fee", "capacity", "description"}
    unknown = set(body) - allowed
    if unknown:
        raise APIError(400, "Unsupported consultation fields", {"fields": sorted(unknown)})
    result = {}

    if "consultation_name" in body:
        name = clean_text(body["consultation_name"], "consultation_name", 40, True)
        if name not in CONSULTATION_NAMES:
            raise APIError(400, "Invalid consultation_name",
                           {"allowed": sorted(CONSULTATION_NAMES)})
        result["consultation_name"] = name
    elif not partial:
        raise APIError(400, "consultation_name is required")

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
        if not fee.is_finite() or fee < 0 or fee > Decimal("99999999.99"):
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

    name = result.get("consultation_name")
    capacity = result.get("capacity")
    if name and capacity is not None:
        if name == "Health Camp Session":
            if not 2 <= capacity <= 10:
                raise APIError(400, "Health Camp Session capacity must be between 2 and 10")
        elif capacity != 1:
            raise APIError(400, "Quick, Standard, and Detailed Consult capacity must be 1")
    return result


def consultation_types(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT consultation_type_id, consultation_name, duration_minutes,
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
    consultation_id = str(uuid.uuid4())
    columns = ["consultation_type_id", "doctor_id"] + list(values)
    params = [consultation_id, doctor_id] + list(values.values())
    sql = f"INSERT INTO consultation_types ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
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
            cur.execute(
                f"UPDATE consultation_types SET {sets} WHERE consultation_type_id=%s AND doctor_id=%s",
                list(values.values()) + [consultation_id, doctor_id],
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT consultation_type_id FROM consultation_types WHERE consultation_type_id=%s AND doctor_id=%s",
                    (consultation_id, doctor_id),
                )
                if not cur.fetchone():
                    raise APIError(404, "Consultation type not found")
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
            consultation_id = str(uuid.UUID(p["consultation_type_id"]))
        except (KeyError, ValueError, TypeError):
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
    blackout_date = parse_iso_date(body.get("blackout_date"), "blackout_date")
    start_value = body.get("start_time")
    end_value = body.get("end_time")
    if (start_value is None) != (end_value is None):
        raise APIError(400, "Provide both start_time and end_time, or neither for a full-day blackout")
    start = parse_clock(start_value, "start_time") if start_value else None
    end = parse_clock(end_value, "end_time") if end_value else None
    if start and end <= start:
        raise APIError(400, "end_time must be later than start_time")
    reason = clean_text(body.get("reason"), "reason", 255)
    blackout_id = str(uuid.uuid4())
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO blackout_dates
                   (blackout_id, doctor_id, blackout_date, start_time, end_time, reason)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (blackout_id, doctor_id, blackout_date, start, end, reason),
            )
        conn.commit()
    return {"message": "Blackout date created", "blackout_id": blackout_id}


def list_blackouts(doctor_id):
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT blackout_id, blackout_date, start_time, end_time, reason, created_at
                   FROM blackout_dates WHERE doctor_id=%s ORDER BY blackout_date""",
                (doctor_id,),
            )
            rows = cur.fetchall()
    return {"blackout_dates": rows}


def delete_blackout(doctor_id, blackout_id):
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
    profile = doctor_profile(doctor_id)
    if not profile:
        raise APIError(404, "Doctor profile not found")
    zone_name = profile["timezone"] or "Asia/Kolkata"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise APIError(500, "Doctor timezone stored in database is invalid")

    local_start = datetime.combine(requested_day, time.min, tzinfo=zone)
    local_end = datetime.combine(requested_day + timedelta(days=1), time.min, tzinfo=zone)
    # MySQL appointment_start is stored as UTC-naive DATETIME(6).
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.appointment_id, a.patient_id, a.consultation_type_id,
                          a.slot_id, a.appointment_start, a.appointment_end,
                          a.fee_amount, a.status, p_user.full_name AS patient_name,
                          p.phone AS patient_phone, ct.consultation_name
                   FROM appointments a
                   JOIN patients p ON p.patient_id=a.patient_id
                   JOIN users p_user ON p_user.user_id=p.patient_id
                   JOIN consultation_types ct ON ct.consultation_type_id=a.consultation_type_id
                   WHERE a.doctor_id=%s
                     AND a.appointment_start >= %s
                     AND a.appointment_start < %s
                   ORDER BY a.appointment_start""",
                (doctor_id, utc_start, utc_end),
            )
            appointments = cur.fetchall()

    return {
        "date": requested_day.isoformat(),
        "timezone": zone_name,
        "appointments": appointments,
        "count": len(appointments),
    }


def lambda_handler(event, context):
    try:
        method, path = path_and_method(event)

        if method == "OPTIONS":
            return response(204, {})

        doctor_id = get_identity(event)
        body = parse_body(event)
        query = event.get("queryStringParameters") or {}
        path_params = event.get("pathParameters") or {}

        # Doctor profile
        if path == "/doctors/me" and method == "GET":
            return response(200, get_profile(doctor_id))
        if path == "/doctors/me" and method in ("PUT", "POST"):
            status, result = save_profile(doctor_id, body)
            return response(status, result)

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
            r"/doctors/me/consultation-types/([0-9a-fA-F-]{36})", path
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
            r"/doctors/me/blackouts/([0-9a-fA-F-]{36})", path
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
    except Exception:
        logger.exception("Unhandled MediSlot application error")
        return response(500, {"error": "Internal server error"})
