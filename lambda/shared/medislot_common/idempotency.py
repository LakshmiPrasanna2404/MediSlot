"""Idempotency keys for booking (DynamoDB idempotency table).

The key is the patient plus the client's Idempotency-Key header
("<patientId>#<key>"), so one patient's key never collides with another's.

    first request          conditional put of IN_PROGRESS (request hash, TTL)
    replay, same body      COMPLETED: the stored status and body come back, so
                           the original appointment is returned and no second
                           one is made. IN_PROGRESS: 409, try again shortly.
    replay, other body     422: the key was used for a different request
    final outcome          stored, including a 4xx such as "slot full", so a
                           replay gets the same answer
    5xx                    the key is deleted, so the client can retry cleanly

TTL: CONFIG_IDEMPOTENCY_TTL_HOURS (24 by default) on expiresAt.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass

from botocore.exceptions import ClientError

from . import config, logs
from .http import APIError, json_default

TABLE = "IDEMPOTENCY_TABLE"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")


@dataclass
class Claim:
    key: str
    request_hash: str


def request_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=json_default).encode("utf-8")).hexdigest()


def begin(patient_id, idempotency_key, body):
    """Claim the key. Returns (claim, None) for a new request or (None, (status, payload)) for a replay."""
    if not isinstance(idempotency_key, str) or not KEY_PATTERN.match(idempotency_key):
        raise APIError(400, "Idempotency-Key header is required (8 to 100 letters, digits, '.', '_', ':' or '-')")
    key = f"{patient_id}#{idempotency_key}"
    digest = request_hash(body)
    now = int(time.time())
    table = config.table(TABLE)
    try:
        table.put_item(
            Item={
                "patientId#key": key,
                "requestHash": digest,
                "status": "IN_PROGRESS",
                "createdAt": now,
                "expiresAt": now + config.config_int("idempotency/ttl-hours") * 3600,
            },
            ConditionExpression="attribute_not_exists(#k)",
            ExpressionAttributeNames={"#k": "patientId#key"},
        )
        return Claim(key, digest), None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    existing = table.get_item(Key={"patientId#key": key}, ConsistentRead=True).get("Item") or {}
    if existing.get("requestHash") != digest:
        logs.warning("idempotency key reused for another request")
        raise APIError(422, "This Idempotency-Key was already used for a different request")
    if existing.get("status") == "COMPLETED":
        stored = existing.get("response") or {}
        logs.info("idempotent replay", storedStatus=int(stored.get("statusCode", 200)))
        return None, (int(stored.get("statusCode", 200)), json.loads(stored.get("body") or "{}"))
    raise APIError(409, "This booking is still being processed; try again in a moment")


def finish(claim, status, payload):
    """Store the final answer so a replay gets the same one."""
    config.table(TABLE).update_item(
        Key={"patientId#key": claim.key},
        UpdateExpression="SET #s = :done, #r = :response",
        ExpressionAttributeNames={"#s": "status", "#r": "response"},
        ExpressionAttributeValues={
            ":done": "COMPLETED",
            ":response": {"statusCode": status, "body": json.dumps(payload, default=json_default)},
        },
    )


def forget(claim):
    """After a server error: free the key so the client can try again."""
    try:
        config.table(TABLE).delete_item(Key={"patientId#key": claim.key})
    except Exception:  # noqa: BLE001 - best effort; the key expires through its TTL anyway
        logs.exception("idempotency key could not be released")
