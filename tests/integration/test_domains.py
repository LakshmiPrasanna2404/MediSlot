"""Milestones 3 and 4 against the deployed API: doctor setup, slots, search, discovery, isolation."""

import itertools
import json
import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bookable(api, doctor):
    token = doctor["access_token"]
    created = api.call("POST", "/doctors/me/consultation-types", token=token,
                       json={"name": "Health Camp Session", "fee": 75, "capacity": 3})
    assert created.status_code == 201, created.text
    type_id = created.json()["consultation_type"]["consultation_type_id"]
    patterns = [{"consultation_type_id": type_id, "day_of_week": d, "start_time": "10:00", "end_time": "12:00"}
                for d in range(1, 8)]
    saved = api.call("PUT", "/doctors/me/availability", token=token, json={"patterns": patterns})
    assert saved.status_code == 200, saved.text
    assert saved.json()["slot_sync"]["created"] >= 58
    return {"type_id": type_id, "doctor_id": doctor["profile_id"]}


def test_doctor_profile_round_trip(api, doctor):
    token = doctor["access_token"]
    updated = api.call("PUT", "/doctors/me", token=token, json={"years_experience": 11, "languages": "English, Marathi"})
    assert updated.status_code == 200
    profile = api.call("GET", "/doctors/me", token=token).json()["profile"]
    assert profile["years_experience"] == 11 and profile["specialisation"] == "Cardiology"


def test_photo_upload_url(api, doctor):
    response = api.call("POST", "/doctors/me/photo", token=doctor["access_token"], json={"content_type": "image/jpeg"})
    assert response.status_code == 200
    assert response.json()["upload"]["url"].startswith("https://")


def test_schedule_shows_materialised_slots(api, doctor, bookable):
    day = (date.today() + timedelta(days=2)).isoformat()
    response = api.call("GET", "/doctors/me/schedule", token=doctor["access_token"], params={"date": day})
    assert response.status_code == 200
    (only,) = response.json()["days"]
    assert [s["local_start"][11:16] for s in only["slots"]] == ["10:00", "11:00"]
    assert all(s["capacity"] == 3 and s["free_seats"] == 3 for s in only["slots"])


@pytest.mark.skipif(not os.environ.get("AWS_REGION") and not os.environ.get("AWS_DEFAULT_REGION"), reason="needs AWS credentials")
def test_materialiser_twice_creates_no_duplicates(api, bookable):
    import boto3

    env = os.environ.get("MEDISLOT_ENV", "dev")
    client = boto3.client("lambda")
    payload = json.dumps({"doctorId": bookable["doctor_id"]})
    runs = []
    for _ in range(2):
        result = client.invoke(FunctionName=f"medislot-{env}-slot-materialiser", Payload=payload)
        assert "FunctionError" not in result, result["Payload"].read()
        runs.append(json.loads(result["Payload"].read()))
    assert runs[1]["created"] == 0
    assert runs[1]["alreadyExisted"] >= 58


def test_blackout_suppresses_and_restores_slots(api, doctor, bookable):
    token = doctor["access_token"]
    day = (date.today() + timedelta(days=5)).isoformat()
    added = api.call("POST", "/doctors/me/blackouts", token=token, json={"date": day, "reason": "Conference"})
    assert added.status_code == 201 and added.json()["slot_sync"]["removed"] == 2
    schedule = api.call("GET", "/doctors/me/schedule", token=token, params={"date": day}).json()
    assert schedule["days"][0]["slots"] == []
    blackout_id = added.json()["blackout"]["blackout_id"]
    removed = api.call("DELETE", f"/doctors/me/blackouts/{blackout_id}", token=token)
    assert removed.status_code == 200 and removed.json()["slot_sync"]["created"] == 2


def test_patient_profile_crud_and_isolation(api, patient, new_patient):
    token = patient["access_token"]
    assert api.call("PUT", "/patients/me", token=token, json={"phone": "9000000001"}).status_code == 200
    assert api.call("GET", "/patients/me", token=token).json()["profile"]["phone"] == "9000000001"
    other = new_patient(city="Nagpur")
    # No route takes a patient id, and ids in the body are refused.
    assert api.call("GET", f"/patients/{other['profile_id']}", token=token).status_code in (403, 404)
    assert api.call("PUT", "/patients/me", token=token, json={"patient_id": other["profile_id"], "city": "X"}).status_code == 400
    assert api.call("GET", "/patients/me", token=other["access_token"]).json()["profile"]["city"] == "Nagpur"
    assert api.call("DELETE", "/patients/me", token=other["access_token"]).status_code == 200
    assert api.call("GET", "/patients/me", token=other["access_token"]).status_code == 401  # token revoked on delete


def test_search_every_filter_combination_finds_the_test_doctor(api, patient, doctor):
    profile = api.call("GET", "/doctors/me", token=doctor["access_token"]).json()["profile"]
    filters = {
        "name": profile["full_name"][:12],
        "hospital": profile["hospital"]["name"][:12],
        "specialisation": "Cardiology",
        "city": "Itestcity",
    }
    for size in range(1, 5):
        for combo in itertools.combinations(filters.items(), size):
            response = api.call("GET", "/doctors", token=patient["access_token"], params=dict(combo))
            assert response.status_code == 200, combo
            ids = [d["doctor_id"] for d in response.json()["doctors"]]
            if any(key in dict(combo) for key in ("name", "hospital", "city")):
                assert doctor["profile_id"] in ids, combo


def test_public_profile_and_slot_lookup(api, patient, bookable):
    token = patient["access_token"]
    profile = api.call("GET", f"/doctors/{bookable['doctor_id']}", token=token).json()["doctor"]
    assert any(t["consultation_type_id"] == bookable["type_id"] and t["fee"] == 75 for t in profile["consultation_types"])
    day = (date.today() + timedelta(days=3)).isoformat()
    slots = api.call("GET", f"/doctors/{bookable['doctor_id']}/slots", token=token,
                     params={"from": day, "to": day, "consultationTypeId": bookable["type_id"]})
    assert slots.status_code == 200
    assert [s["free_seats"] for s in slots.json()["slots"]] == [3, 3]
