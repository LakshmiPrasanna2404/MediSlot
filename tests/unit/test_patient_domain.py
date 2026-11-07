"""Patient domain (Milestone 4): profile CRUD and isolation, doctor search, public profile, slot lookup."""

import itertools
from datetime import timedelta

import pytest
from conftest import api_event, call, load_handler

from medislot_common import db, slots


def as_user(user):
    return {"user_id": user["user_id"], "role": user["role"]}


@pytest.fixture()
def patient(register, aws):
    return register("patient", full_name="Meera Iyer", city="Chennai", phone="9876543210")


def test_profile_read_update(patient):
    fn = load_handler("patient_profile")
    status, body = call(fn, api_event("GET", "/patients/me", user=as_user(patient)))
    assert status == 200 and body["profile"]["city"] == "Chennai"
    update = {"date_of_birth": "1995-04-12", "gender": "Female", "emergency_contact_name": "Ravi",
              "emergency_contact_phone": "9000000000", "full_name": "Meera S Iyer"}
    status, body = call(fn, api_event("PUT", "/patients/me", user=as_user(patient), body=update))
    assert status == 200
    assert body["profile"]["date_of_birth"] == "1995-04-12" and body["profile"]["gender"] == "female"
    assert body["profile"]["full_name"] == "Meera S Iyer"
    assert call(fn, api_event("PUT", "/patients/me", user=as_user(patient), body={"date_of_birth": "12/04/1995"}))[0] == 400


def test_patient_cannot_read_or_change_another_patient(register, patient):
    """Identity comes only from the authorizer context: ids in the body or query are refused or ignored."""
    other = register("patient", city="Delhi")
    fn = load_handler("patient_profile")
    status, body = call(fn, api_event("GET", "/patients/me", user=as_user(patient),
                                      query={"patient_id": other["profile_id"], "userId": other["user_id"]}))
    assert status == 200 and body["profile"]["patient_id"] == patient["profile_id"]
    status, _ = call(fn, api_event("PUT", "/patients/me", user=as_user(patient),
                                   body={"patient_id": other["profile_id"], "city": "Hacked"}))
    assert status == 400
    assert db.fetch_one("SELECT city FROM patients WHERE patient_id = %s", (other["profile_id"],))["city"] == "Delhi"


def test_doctor_token_is_refused_by_patient_lambda(register, aws):
    doctor = register("doctor")
    assert call(load_handler("patient_profile"), api_event("GET", "/patients/me", user=as_user(doctor)))[0] == 403


def test_delete_without_history_removes_account_and_revokes_token(patient, aws):
    fn = load_handler("patient_profile")
    event = api_event("DELETE", "/patients/me", user=as_user(patient), jti="jti-delete-1")
    status, body = call(fn, event)
    assert status == 200 and body["message"] == "Account deleted"
    assert db.fetch_one("SELECT 1 FROM users WHERE user_id = %s", (patient["user_id"],)) is None
    revoked = aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-token-revocations").get_item(
        Key={"jti": "jti-delete-1"})
    assert revoked["Item"]["userId"] == patient["user_id"]


def test_delete_is_refused_with_upcoming_appointment(patient, register, clean_db):
    doctor = register("doctor")
    with clean_db.cursor() as cur:
        cur.execute("INSERT INTO consultation_types (doctor_id, name, fee, duration_minutes) VALUES (%s,'Quick Consult',100,15)",
                    (doctor["profile_id"],))
        cur.execute(
            """INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id,
               slot_start_utc, slot_end_utc, consultation_fee)
               VALUES (UUID(), %s, %s, 's', %s, UTC_TIMESTAMP() + INTERVAL 2 DAY,
                       UTC_TIMESTAMP() + INTERVAL 2 DAY + INTERVAL 15 MINUTE, 100)""",
            (patient["profile_id"], doctor["profile_id"], cur.lastrowid),
        )
    status, _ = call(load_handler("patient_profile"), api_event("DELETE", "/patients/me", user=as_user(patient)))
    assert status == 409


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

DOCTORS = [
    ("Anil Sharma", "Cardiology", "Apollo Hospital", "Hyderabad"),
    ("Anita Desai", "Dermatology", "Apollo Hospital", "Chennai"),
    ("Bhavna Rao", "Cardiology", "City Care Clinic", "Hyderabad"),
    ("Chirag Mehta", "Paediatrics", "Rainbow Children's", "Hyderabad"),
    ("Deepa Nair", "Cardiology", "Apollo Hospital", "Hyderabad"),
]


@pytest.fixture()
def directory(register, aws):
    for name, specialisation, hospital, city in DOCTORS:
        register("doctor", full_name=name, specialisation=specialisation, hospital_name=hospital, hospital_city=city)
    return register("patient")


def _search(patient, **params):
    status, body = call(load_handler("doctor_search"), api_event("GET", "/doctors", user=as_user(patient), query=params or None))
    assert status == 200, body
    return [d["full_name"] for d in body["doctors"]], body


FILTERS = {"name": "Ani", "hospital": "apollo", "specialisation": "cardiology", "city": "HYDERABAD"}


def _expected(filters):
    result = []
    for name, specialisation, hospital, city in DOCTORS:
        if "name" in filters and not name.lower().startswith(filters["name"].lower()):
            continue
        if "hospital" in filters and not hospital.lower().startswith(filters["hospital"].lower()):
            continue
        if "specialisation" in filters and specialisation.lower() != filters["specialisation"].lower():
            continue
        if "city" in filters and city.lower() != filters["city"].lower():
            continue
        result.append(name)
    return result


@pytest.mark.parametrize(
    "combo",
    [dict(c) for size in range(5) for c in itertools.combinations(FILTERS.items(), size)],
    ids=lambda c: "+".join(c) or "none",
)
def test_search_every_filter_combination(directory, combo):
    names, _ = _search(directory, **combo)
    assert names == _expected(combo)


def test_search_pagination(directory):
    first, body = _search(directory, page="1", page_size="2")
    assert first == ["Anil Sharma", "Anita Desai"] and body["has_more"] is True
    second, body = _search(directory, page="3", page_size="2")
    assert second == ["Deepa Nair"] and body["has_more"] is False


def test_search_rejects_bad_parameters(directory):
    fn = load_handler("doctor_search")
    assert call(fn, api_event("GET", "/doctors", user=as_user(directory), query={"page_size": "500"}))[0] == 400
    assert call(fn, api_event("GET", "/doctors", user=as_user(directory), query={"doctor_id": "x"}))[0] == 400


def test_search_wildcards_are_literal(directory):
    assert _search(directory, name="%")[0] == []
    assert _search(directory, name="_nil")[0] == []


def test_search_uses_indexes(directory, clean_db):
    search = load_handler("doctor_search")
    sql, values = search.build_search({"name": "Ani"}, 1, 20)
    with clean_db.cursor() as cur:
        cur.execute("EXPLAIN " + sql, values)
        plan = {row["table"]: row for row in cur.fetchall()}
    assert plan["u"]["key"] == "idx_users_role_full_name"


# ---------------------------------------------------------------------------
# Public profile and slot lookup
# ---------------------------------------------------------------------------


@pytest.fixture()
def bookable_doctor(register, aws, clean_db):
    doctor = register("doctor", full_name="Kavya Reddy", specialisation="General Medicine", timezone="Asia/Kolkata")
    types = load_handler("consultation_types")
    _, quick = call(types, api_event("POST", "/doctors/me/consultation-types", user=as_user(doctor),
                                     body={"name": "Quick Consult", "fee": 250}))
    _, camp = call(types, api_event("POST", "/doctors/me/consultation-types", user=as_user(doctor),
                                    body={"name": "Health Camp Session", "fee": 50, "capacity": 5}))
    quick_id = quick["consultation_type"]["consultation_type_id"]
    camp_id = camp["consultation_type"]["consultation_type_id"]
    patterns = []
    for day in range(1, 8):
        patterns.append({"consultation_type_id": quick_id, "day_of_week": day, "start_time": "09:00", "end_time": "10:00"})
        patterns.append({"consultation_type_id": camp_id, "day_of_week": day, "start_time": "14:00", "end_time": "15:00"})
    status, _ = call(load_handler("availability"), api_event("PUT", "/doctors/me/availability", user=as_user(doctor),
                                                             body={"patterns": patterns}))
    assert status == 200
    return doctor, quick_id, camp_id


def test_public_profile_includes_consultation_types_and_fees(bookable_doctor, register):
    doctor, quick_id, camp_id = bookable_doctor
    patient = register("patient")
    fn = load_handler("doctor_search")
    status, body = call(fn, api_event("GET", "/doctors/{doctorId}", user=as_user(patient),
                                      path_params={"doctorId": doctor["profile_id"]}))
    assert status == 200
    types = {t["consultation_type_id"]: t for t in body["doctor"]["consultation_types"]}
    assert types[quick_id]["fee"] == 250 and types[quick_id]["duration_minutes"] == 15
    assert types[camp_id]["capacity"] == 5
    assert "licence_number" not in body["doctor"]
    missing = call(fn, api_event("GET", "/doctors/{doctorId}", user=as_user(patient),
                                 path_params={"doctorId": "6f1c1e9e-1111-4c7a-9c4e-2d3a7b9c0d11"}))
    assert missing[0] == 404
    assert call(fn, api_event("GET", "/doctors/{doctorId}", user=as_user(patient), path_params={"doctorId": "nope"}))[0] == 400


def test_slot_lookup_returns_free_seats_and_filters(bookable_doctor, register, aws):
    doctor, quick_id, camp_id = bookable_doctor
    patient = register("patient")
    day = slots.local_today("Asia/Kolkata", slots.now_utc()) + timedelta(days=3)
    camp_slot = f"{doctor['profile_id']}#{camp_id}#{day:%Y%m%d}#1400"
    aws.resource("dynamodb", region_name="us-east-1").Table("medislot-test-slots").update_item(
        Key={"slotId": camp_slot}, UpdateExpression="SET bookedCount = :b, heldCount = :h",
        ExpressionAttributeValues={":b": 3, ":h": 1})

    fn = load_handler("slot_lookup")
    resource = "/doctors/{doctorId}/slots"
    path = {"doctorId": doctor["profile_id"]}
    status, body = call(fn, api_event("GET", resource, user=as_user(patient), path_params=path,
                                      query={"from": day.isoformat(), "to": day.isoformat()}))
    assert status == 200
    assert len(body["slots"]) == 5  # four 15-minute quick slots + one camp slot
    camp = next(s for s in body["slots"] if s["slot_id"] == camp_slot)
    assert camp["capacity"] == 5 and camp["free_seats"] == 1 and camp["fee"] == 50
    assert camp["consultation_type_name"] == "Health Camp Session"

    status, body = call(fn, api_event("GET", resource, user=as_user(patient), path_params=path,
                                      query={"from": day.isoformat(), "to": day.isoformat(),
                                             "consultationTypeId": str(quick_id)}))
    assert status == 200 and {s["consultation_type_id"] for s in body["slots"]} == {quick_id}

    too_long = {"from": day.isoformat(), "to": (day + timedelta(days=40)).isoformat()}
    assert call(fn, api_event("GET", resource, user=as_user(patient), path_params=path, query=too_long))[0] == 400
    reversed_range = {"from": day.isoformat(), "to": (day - timedelta(days=1)).isoformat()}
    assert call(fn, api_event("GET", resource, user=as_user(patient), path_params=path, query=reversed_range))[0] == 400
