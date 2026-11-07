"""Photo Upload Lambda (doctor role).

    POST /doctors/me/photo   {"content_type": "image/jpeg" | "image/png" | "image/webp"}

Returns a pre-signed S3 POST. The browser uploads the image straight to S3,
so binary content never passes through API Gateway. The POST policy pins the
object key and content type and caps the size (SSM photos/max-upload-bytes).
The new key is stored on the doctor's profile.
"""

import os
import uuid

from medislot_common import config, db, profiles
from medislot_common.http import APIError, api_handler, identity, parse_body
from medislot_common.validation import reject_unknown

PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def create_upload(event):
    caller = identity(event, "doctor")
    body = parse_body(event)
    reject_unknown(body, {"content_type"})
    content_type = body.get("content_type")
    if content_type not in PHOTO_TYPES:
        raise APIError(400, "content_type must be image/jpeg, image/png or image/webp")

    with db.connection().cursor() as cur:
        doctor = profiles.require_doctor(cur, caller.user_id)
    key = f"doctor-profiles/{doctor['doctor_id']}/{uuid.uuid4().hex}.{PHOTO_TYPES[content_type]}"
    max_bytes = config.config_int("photos/max-upload-bytes")
    expires = config.config_int("photos/upload-url-seconds")
    upload = config.client("s3").generate_presigned_post(
        Bucket=os.environ["DOCTOR_FILES_BUCKET"],
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=expires,
    )
    db.execute(
        "UPDATE doctors SET profile_photo_s3_key = %s WHERE doctor_id = %s",
        (key, doctor["doctor_id"]),
    )
    return 200, {
        "upload": {"url": upload["url"], "method": "POST", "fields": upload["fields"]},
        "s3_key": key,
        "max_bytes": max_bytes,
        "expires_in_seconds": expires,
    }


lambda_handler = api_handler({"POST /doctors/me/photo": create_upload})
