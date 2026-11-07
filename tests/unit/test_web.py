"""Web app (doctor and patient dashboards). The MediSlot API is mocked with `responses`."""

import base64
import json
import re

import pytest
import responses
from conftest import ROOT

API = "https://api.example.test/dev"


def fake_jwt(role):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'sub': 'u1', 'role': role, 'iat': 1, 'exp': 2, 'jti': 'j'})}.sig"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT))
    monkeypatch.setenv("MEDISLOT_API_BASE_URL", API)
    from dashboard import create_app

    return create_app({"SECRET_KEY": "test-secret", "TESTING": True, "SESSION_DIR": str(tmp_path)})


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client, path="/"):
    page = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if match:
        return match.group(1)
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def login(client, role):
    token = csrf(client, f"/{role}/login")
    responses.add(responses.POST, f"{API}/auth/{role}s/login", json={
        "access_token": fake_jwt(role), "refresh_token": "r1", "token_type": "Bearer",
        "user": {"user_id": "u1", "full_name": f"Test {role}", "email": "x@example.com", "role": role},
    })
    return client.post(f"/{role}/login", data={"email": "x@example.com", "password": "pw", "csrf_token": token})


def test_landing_offers_both_logins(client):
    page = client.get("/").get_data(as_text=True)
    assert "Login as Doctor" in page and "Login as Patient" in page


@responses.activate
def test_doctor_login_stores_token_server_side_and_lands_on_portal(client):
    response = login(client, "doctor")
    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/schedule")
    with client.session_transaction() as sess:
        assert sess["role"] == "doctor" and sess["refresh_token"] == "r1"
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and fake_jwt("doctor") not in cookie


@responses.activate
def test_patient_session_cannot_open_doctor_pages(client):
    login(client, "patient")
    response = client.get("/doctor/schedule")
    assert response.status_code == 302 and response.headers["Location"].endswith("/")


@responses.activate
def test_doctor_session_cannot_open_patient_pages(client):
    login(client, "doctor")
    response = client.get("/patient/search")
    assert response.status_code == 302 and response.headers["Location"].endswith("/")


def test_anonymous_user_is_sent_to_the_right_login(client):
    assert client.get("/doctor/availability").headers["Location"].endswith("/doctor/login")
    assert client.get("/patient/profile").headers["Location"].endswith("/patient/login")


@responses.activate
def test_failed_login_shows_api_message(client):
    token = csrf(client, "/patient/login")
    responses.add(responses.POST, f"{API}/auth/patients/login", status=401, json={"error": "Invalid email or password"})
    response = client.post("/patient/login", data={"email": "a@b.c", "password": "x", "csrf_token": token})
    assert response.status_code == 401 and "Invalid email or password" in response.get_data(as_text=True)


@responses.activate
def test_expired_access_token_is_refreshed_once(client):
    login(client, "patient")
    responses.add(responses.GET, f"{API}/patients/me", status=401, json={"error": "Unauthorized"})
    responses.add(responses.POST, f"{API}/auth/refresh", json={"access_token": fake_jwt("patient"), "refresh_token": "r2"})
    responses.add(responses.GET, f"{API}/patients/me", json={"profile": {
        "full_name": "Meera", "email": "m@example.com", "date_of_birth": None, "gender": None, "phone": None,
        "address": None, "city": "Chennai", "emergency_contact_name": None, "emergency_contact_phone": None}})
    response = client.get("/patient/profile")
    assert response.status_code == 200 and "Chennai" in response.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert sess["refresh_token"] == "r2"


@responses.activate
def test_failed_refresh_logs_the_user_out(client):
    login(client, "doctor")
    responses.add(responses.GET, f"{API}/doctors/me/schedule", status=401, json={"error": "Unauthorized"})
    responses.add(responses.POST, f"{API}/auth/refresh", status=401, json={"error": "Invalid or expired refresh token"})
    response = client.get("/doctor/schedule")
    assert response.status_code == 302 and response.headers["Location"].endswith("/doctor/login")
    with client.session_transaction() as sess:
        assert "access_token" not in sess


@responses.activate
def test_post_without_csrf_token_is_rejected(client):
    login(client, "doctor")
    assert client.post("/doctor/blackouts", data={"date": "2030-01-01"}).status_code == 400


@responses.activate
def test_logout_revokes_tokens_through_the_api(client):
    login(client, "patient")
    token = csrf(client)
    responses.add(responses.POST, f"{API}/auth/logout", json={"message": "Logged out"})
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302
    logout_call = [c for c in responses.calls if c.request.url.endswith("/auth/logout")][0]
    assert logout_call.request.headers["Authorization"] == f"Bearer {fake_jwt('patient')}"
    assert json.loads(logout_call.request.body) == {"refresh_token": "r1"}
    with client.session_transaction() as sess:
        assert "role" not in sess


@responses.activate
def test_availability_form_is_sent_as_patterns(client):
    login(client, "doctor")
    token = csrf(client)
    responses.add(responses.PUT, f"{API}/doctors/me/availability",
                  json={"patterns_saved": 1, "slot_sync": {"created": 8, "removed": 0, "keptBecauseBooked": []}})
    client.post("/doctor/availability", data={
        "csrf_token": token,
        "day_of_week": ["1", ""], "start_time": ["09:00", ""], "end_time": ["13:00", ""], "consultation_type_id": ["7", "7"],
    })
    sent = json.loads([c for c in responses.calls if c.request.method == "PUT"][0].request.body)
    assert sent == {"patterns": [{"consultation_type_id": 7, "day_of_week": 1, "start_time": "09:00", "end_time": "13:00"}]}


@responses.activate
def test_search_page_passes_filters_and_renders_results(client):
    login(client, "patient")
    responses.add(responses.GET, f"{API}/doctors", json={"doctors": [{
        "doctor_id": "d1", "full_name": "Anil Sharma", "specialisation": "Cardiology", "hospital_name": "Apollo",
        "city": "Hyderabad", "years_experience": 9, "fee_from": 500, "photo_url": None}], "page": 1, "has_more": False})
    page = client.get("/patient/search?name=Ani&city=Hyderabad").get_data(as_text=True)
    assert "Anil Sharma" in page
    request = [c for c in responses.calls if "/doctors?" in c.request.url][0].request
    assert "name=Ani" in request.url and "city=Hyderabad" in request.url


@responses.activate
def test_doctor_pages_render(client):
    login(client, "doctor")
    responses.add(responses.GET, f"{API}/doctors/me/schedule", json={
        "timezone": "Asia/Kolkata", "view": "day", "from": "2026-11-09", "to": "2026-11-09",
        "days": [{"date": "2026-11-09", "slots": [{
            "slot_id": "s", "local_start": "2026-11-09T10:00:00+05:30", "local_end": "2026-11-09T11:00:00+05:30",
            "consultation_type_name": "Health Camp Session", "capacity": 3, "booked_count": 1, "held_count": 0,
            "free_seats": 2, "status": "OPEN", "patients": [{"patient_name": "Asha Rao", "status": "CONFIRMED"}]}]}]})
    page = client.get("/doctor/schedule").get_data(as_text=True)
    assert "Asha Rao" in page and "10:00" in page and "1/3 booked" in page

    responses.add(responses.GET, f"{API}/doctors/me/availability", json={"timezone": "Asia/Kolkata", "patterns": [
        {"consultation_type_id": 7, "day_of_week": 1, "start_time": "09:00", "end_time": "13:00"}]})
    responses.add(responses.GET, f"{API}/doctors/me/consultation-types", json={"consultation_types": [
        {"consultation_type_id": 7, "name": "Standard Consult", "duration_minutes": 30, "capacity": 1, "fee": 500,
         "description": None, "is_active": 1}]})
    assert "Standard Consult" in client.get("/doctor/availability").get_data(as_text=True)
    assert "Standard Consult" in client.get("/doctor/consultation-types").get_data(as_text=True)


@responses.activate
def test_patient_doctor_page_shows_fees_and_slots(client):
    login(client, "patient")
    responses.add(responses.GET, f"{API}/doctors/d1", json={"doctor": {
        "doctor_id": "d1", "full_name": "Kavya Reddy", "specialisation": "General Medicine", "timezone": "Asia/Kolkata",
        "hospital_name": None, "hospital_address": None, "city": None, "years_experience": None, "qualifications": None,
        "languages": None, "photo_url": None, "consultation_types": [
            {"consultation_type_id": 1, "name": "Quick Consult", "duration_minutes": 15, "capacity": 1, "fee": 250,
             "description": None}]}})
    responses.add(responses.GET, f"{API}/doctors/d1/slots", json={"timezone": "Asia/Kolkata", "slots": [{
        "slot_id": "s1", "local_start": "2026-11-10T09:00:00+05:30", "consultation_type_name": "Quick Consult",
        "fee": 250, "capacity": 1, "free_seats": 1}]})
    page = client.get("/patient/doctors/d1").get_data(as_text=True)
    assert "Kavya Reddy" in page and "₹250.00" in page and "09:00" in page and "1 of 1 free" in page
