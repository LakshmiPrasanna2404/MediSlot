"""Doctor domain (Milestone 3): profile, photo, consultation types, availability, blackouts, schedule."""

from datetime import timedelta

import pytest
from conftest import FakeContext, api_event, call, load_handler

from medislot_common import db, slots


@pytest.fixture()
def doctor(register, aws):
    return register("doctor", timezone="Asia/Kolkata", specialisation="Dermatology",
                    hospital_name="Lotus Clinic", hospital_city="Hyderabad")


def as_user(user):
    return {"user_id": user["user_id"], "role": user["role"]}


def test_profile_get_and_update(doctor):
    fn = load_handler("doctor_profile")
    status, body = call(fn, api_event("GET", "/doctors/me", user=as_user(doctor)))
    assert status == 200
    assert body["profile"]["specialisation"] == "Dermatology"
    assert body["profile"]["hospital"]["city"] == "Hyderabad"

    update = {"years_experience": 12, "languages": "English, Telugu", "hospital_name": "Apollo",
              "hospital_city": "Hyderabad", "hospital_address": "Jubilee Hills", "full_name": "Dr Renamed"}
    status, body = call(fn, api_event("PUT", "/doctors/me", user=as_user(doctor), body=update))
    assert status == 200
    profile = body["profile"]
    assert profile["years_experience"] == 12 and profile["full_name"] == "Dr Renamed"
    assert profile["hospital"]["name"] == "Apollo" and profile["hospital"]["address"] == "Jubilee Hills"


def test_profile_rejects_licence_change_and_unknown_fields(doctor):
    fn = load_handler("doctor_profile")
    assert call(fn, api_event("PUT", "/doctors/me", user=as_user(doctor), body={"licence_number": "OTHER"}))[0] == 400
    assert call(fn, api_event("PUT", "/doctors/me", user=as_user(doctor), body={"doctor_id": "x"}))[0] == 400
    assert call(fn, api_event("PUT", "/doctors/me", user=as_user(doctor), body={"timezone": "Mars/Base"}))[0] == 400


def test_patient_token_is_refused_by_doctor_lambda(register, aws):
    patient = register("patient")
    status, _ = call(load_handler("doctor_profile"), api_event("GET", "/doctors/me", user=as_user(patient)))
    assert status == 403


def test_photo_upload_returns_presigned_post_and_stores_key(doctor):
    fn = load_handler("photo_upload")
    status, body = call(fn, api_event("POST", "/doctors/me/photo", user=as_user(doctor), body={"content_type": "image/png"}))
    assert status == 200
    assert body["upload"]["method"] == "POST" and "policy" in body["upload"]["fields"]
    assert body["s3_key"].startswith(f"doctor-profiles/{doctor['profile_id']}/") and body["s3_key"].endswith(".png")
    stored = db.fetch_one("SELECT profile_photo_s3_key FROM doctors WHERE doctor_id = %s", (doctor["profile_id"],))
    assert stored["profile_photo_s3_key"] == body["s3_key"]
    assert call(fn, api_event("POST", "/doctors/me/photo", user=as_user(doctor), body={"content_type": "text/html"}))[0] == 400


def _create_type(doctor, **body):
    payload = {"name": "Standard Consult", "fee": 500}
    payload.update(body)
    return call(load_handler("consultation_types"),
                api_event("POST", "/doctors/me/consultation-types", user=as_user(doctor), body=payload))


@pytest.mark.parametrize(
    "body,status",
    [
        ({"name": "Quick Consult", "fee": 200}, 201),
        ({"name": "Standard Consult", "fee": 500, "duration_minutes": 30}, 201),
        ({"name": "Standard Consult", "fee": 500, "duration_minutes": 60}, 400),
        ({"name": "Detailed Consult", "fee": 900, "capacity": 2}, 400),
        ({"name": "Health Camp Session", "fee": 100, "capacity": 10}, 201),
        ({"name": "Health Camp Session", "fee": 100, "capacity": 11}, 400),
        ({"name": "Health Camp Session", "fee": 100, "capacity": 1}, 400),
        ({"name": "Astrology", "fee": 100}, 400),
        ({"name": "Quick Consult", "fee": -1}, 400),
    ],
)
def test_consultation_type_rules(doctor, body, status):
    assert _create_type(doctor, **body)[0] == status


def test_consultation_type_update_and_delete(doctor):
    fn = load_handler("consultation_types")
    _, created = _create_type(doctor, name="Health Camp Session", fee=100, capacity=4)
    type_id = created["consultation_type"]["consultation_type_id"]
    path = {"consultationTypeId": str(type_id)}
    resource = "/doctors/me/consultation-types/{consultationTypeId}"
    status, body = call(fn, api_event("PUT", resource, user=as_user(doctor), path_params=path, body={"fee": 150, "capacity": 6}))
    assert status == 200 and body["consultation_type"]["capacity"] == 6
    assert call(fn, api_event("PUT", resource, user=as_user(doctor), path_params=path, body={"name": "Quick Consult"}))[0] == 400

    _put_availability(doctor, [{"consultation_type_id": type_id, "day_of_week": 1, "start_time": "10:00", "end_time": "12:00"}])
    assert call(fn, api_event("PUT", resource, user=as_user(doctor), path_params=path, body={"capacity": 8}))[0] == 409
    assert call(fn, api_event("DELETE", resource, user=as_user(doctor), path_params=path))[0] == 409
    _put_availability(doctor, [])
    assert call(fn, api_event("DELETE", resource, user=as_user(doctor), path_params=path))[0] == 204
    _, listing = call(fn, api_event("GET", "/doctors/me/consultation-types", user=as_user(doctor)))
    assert listing["consultation_types"][0]["is_active"] == 0


def test_other_doctors_type_is_not_found(register, doctor):
    _, created = _create_type(doctor, name="Quick Consult", fee=200)
    other = register("doctor")
    fn = load_handler("consultation_types")
    event = api_event("DELETE", "/doctors/me/consultation-types/{consultationTypeId}", user=as_user(other),
                      path_params={"consultationTypeId": str(created["consultation_type"]["consultation_type_id"])})
    assert call(fn, event)[0] == 404


def _put_availability(doctor, patterns):
    return call(load_handler("availability"),
                api_event("PUT", "/doctors/me/availability", user=as_user(doctor), body={"patterns": patterns}))


def test_availability_validation(doctor):
    _, created = _create_type(doctor, name="Detailed Consult", fee=900)
    type_id = created["consultation_type"]["consultation_type_id"]
    base = {"consultation_type_id": type_id, "day_of_week": 2}
    overlapping = [{**base, "start_time": "09:00", "end_time": "11:00"}, {**base, "start_time": "10:30", "end_time": "12:00"}]
    status, body = _put_availability(doctor, overlapping)
    assert status == 400 and "overlap" in body["error"]
    assert _put_availability(doctor, [{**base, "start_time": "09:00", "end_time": "09:30"}])[0] == 400  # shorter than 60 min
    assert _put_availability(doctor, [{**base, "day_of_week": 8, "start_time": "09:00", "end_time": "10:00"}])[0] == 400
    assert _put_availability(doctor, [{**base, "consultation_type_id": 999999, "start_time": "09:00", "end_time": "10:00"}])[0] == 400


def test_availability_change_syncs_slots_and_blackouts_suppress_them(doctor, aws):
    _, created = _create_type(doctor, name="Standard Consult", fee=500)
    type_id = created["consultation_type"]["consultation_type_id"]
    every_day = [{"consultation_type_id": type_id, "day_of_week": d, "start_time": "09:00", "end_time": "10:00"} for d in range(1, 8)]
    status, body = _put_availability(doctor, every_day)
    assert status == 200 and body["patterns_saved"] == 7
    table = aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-slots")
    total = table.scan(Select="COUNT")["Count"]
    assert body["slot_sync"]["created"] == total and total >= 58

    tomorrow = slots.local_today("Asia/Kolkata", slots.now_utc()) + timedelta(days=1)
    availability = load_handler("availability")
    status, body = call(availability, api_event("POST", "/doctors/me/blackouts", user=as_user(doctor),
                                                body={"date": tomorrow.isoformat(), "reason": "Conference"}))
    assert status == 201 and body["slot_sync"]["removed"] == 2
    assert call(availability, api_event("POST", "/doctors/me/blackouts", user=as_user(doctor),
                                        body={"date": tomorrow.isoformat()}))[0] == 409
    assert call(availability, api_event("POST", "/doctors/me/blackouts", user=as_user(doctor),
                                        body={"date": "2020-01-01"}))[0] == 400

    blackout_id = body["blackout"]["blackout_id"]
    status, body = call(availability, api_event("DELETE", "/doctors/me/blackouts/{blackoutId}", user=as_user(doctor),
                                                path_params={"blackoutId": str(blackout_id)}))
    assert status == 200 and body["slot_sync"]["created"] == 2
    assert table.scan(Select="COUNT")["Count"] == total


def test_schedule_day_view_lists_slots_with_booked_patients(doctor, register, aws, clean_db):
    _, created = _create_type(doctor, name="Health Camp Session", fee=100, capacity=3)
    type_id = created["consultation_type"]["consultation_type_id"]
    _put_availability(doctor, [{"consultation_type_id": type_id, "day_of_week": d, "start_time": "10:00",
                                "end_time": "11:00"} for d in range(1, 8)])
    day = slots.local_today("Asia/Kolkata", slots.now_utc()) + timedelta(days=2)
    slot_id = f"{doctor['profile_id']}#{type_id}#{day:%Y%m%d}#1000"
    patient = register("patient", full_name="Asha Rao")
    start, end = slots.doctor_local_range("Asia/Kolkata", day, 1)
    with clean_db.cursor() as cur:
        cur.execute(
            """INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id,
                   slot_start_utc, slot_end_utc, consultation_fee)
               VALUES (UUID(), %s, %s, %s, %s, %s, %s, 100)""",
            (patient["profile_id"], doctor["profile_id"], slot_id, type_id,
             (start + timedelta(hours=10)).replace(tzinfo=None), (start + timedelta(hours=11)).replace(tzinfo=None)),
        )
    aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-slots").update_item(
        Key={"slotId": slot_id}, UpdateExpression="SET bookedCount = :n", ExpressionAttributeValues={":n": 1})

    fn = load_handler("schedule")
    status, body = call(fn, api_event("GET", "/doctors/me/schedule", user=as_user(doctor), query={"date": day.isoformat()}))
    assert status == 200 and body["timezone"] == "Asia/Kolkata"
    (only_day,) = body["days"]
    (slot,) = only_day["slots"]
    assert slot["slot_id"] == slot_id and slot["local_start"].startswith(f"{day.isoformat()}T10:00")
    assert slot["capacity"] == 3 and slot["booked_count"] == 1 and slot["free_seats"] == 2
    assert slot["consultation_type_name"] == "Health Camp Session"
    assert [p["patient_name"] for p in slot["patients"]] == ["Asha Rao"]

    status, week = call(fn, api_event("GET", "/doctors/me/schedule", user=as_user(doctor),
                                      query={"date": day.isoformat(), "view": "week"}))
    assert status == 200 and len(week["days"]) == 7

    status, listing = call(fn, api_event("GET", "/doctors/me/appointments", user=as_user(doctor),
                                         query={"status": "CONFIRMED"}))
    assert status == 200 and [a["patient_name"] for a in listing["appointments"]] == ["Asha Rao"]
    assert call(fn, api_event("GET", "/doctors/me/appointments", user=as_user(doctor), query={"status": "BOGUS"}))[0] == 400


def test_log_lines_carry_correlation_and_api_request_ids(doctor):
    """Without X-Ray, the IDs in each log line are how one request is followed end to end."""
    import io
    import json
    import logging

    from medislot_common import logs

    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logs.JsonFormatter())
    logger = logs.get_logger()
    level = logger.level
    logger.setLevel(logging.INFO)  # tests run at WARNING; "request completed" is INFO
    logger.addHandler(capture)
    try:
        event = api_event("GET", "/doctors/me", user=as_user(doctor))
        event["headers"]["X-Correlation-Id"] = "web-test-0001"
        result = load_handler("doctor_profile").lambda_handler(event, FakeContext())
    finally:
        logger.removeHandler(capture)
        logger.setLevel(level)

    assert result["headers"]["X-Correlation-Id"] == "web-test-0001"
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines
    for line in lines:
        assert line["correlationId"] == "web-test-0001"
        assert line["apiRequestId"] == event["requestContext"]["requestId"]
