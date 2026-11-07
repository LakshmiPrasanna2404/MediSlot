# API reference

One REST API in API Gateway: `medislot-<env>-api`, stage `<env>`. The base URL is the `ApiUrl` output of the
`medislot-<env>-api` stack, also stored in SSM at `/medislot/<env>/api/base-url`. The source of truth is the
OpenAPI body in `infrastructure/api-stack.yaml`.

## Conventions

- JSON in and out. Request bodies are checked against a JSON model in API Gateway (unknown fields are rejected)
  and again in the Lambda.
- Protected routes need `Authorization: Bearer <access token>`. The caller's identity always comes from the token
  through the authorizer, never from the body, path or query.
- Times: instants are UTC ISO 8601 with `Z`. Slots also carry `local_start` and `local_end` in the doctor's zone
  with the offset. Dates (`date`, `from`, `to`) are local dates in the doctor's zone.
- Errors: `{"error": "message"}`, sometimes with `"details"`. Every response carries `X-Correlation-Id`: the value
  the client sent, otherwise the API Gateway request ID. The same ID is in every log line for the request.

| Status | Meaning |
|---|---|
| 400 | Invalid body, parameter or business rule (message says which) |
| 401 | Missing, invalid, expired or logged-out token; wrong login |
| 403 | Valid token, wrong role for the route |
| 404 | Unknown route or resource (including another doctor's resource) |
| 409 | Conflict with current state (duplicate email, type in use, upcoming appointments) |
| 429 | Throttled (register and login: 10 requests per second, burst 20; other routes: 50, burst 100) |
| 5xx | Server error; 503 if the database is unreachable |

## Route status

| Method | Path | Role | Lambda | Milestone | Status |
|---|---|---|---|---|---|
| POST | /auth/doctors/register | public | Auth | 2 | Deployed |
| POST | /auth/patients/register | public | Auth | 2 | Deployed |
| POST | /auth/doctors/login | public | Auth | 2 | Deployed |
| POST | /auth/patients/login | public | Auth | 2 | Deployed |
| POST | /auth/refresh | public | Auth | 2 | Deployed |
| POST | /auth/logout | doctor, patient | Auth | 2 | Deployed |
| GET, PUT | /doctors/me | doctor | Doctor Profile | 3 | Deployed |
| POST | /doctors/me/photo | doctor | Photo Upload | 3 | Deployed |
| GET, POST | /doctors/me/consultation-types | doctor | Consultation Types | 3 | Deployed |
| PUT, DELETE | /doctors/me/consultation-types/{consultationTypeId} | doctor | Consultation Types | 3 | Deployed |
| GET, PUT | /doctors/me/availability | doctor | Availability | 3 | Deployed |
| GET, POST | /doctors/me/blackouts | doctor | Availability | 3 | Deployed |
| DELETE | /doctors/me/blackouts/{blackoutId} | doctor | Availability | 3 | Deployed |
| GET | /doctors/me/schedule | doctor | Schedule | 3 | Deployed |
| GET | /doctors/me/appointments | doctor | Schedule | 3 | Deployed (list fills from Milestone 5) |
| GET, PUT, DELETE | /patients/me | patient | Patient Profile | 4 | Deployed |
| GET | /doctors | patient | Doctor Search | 4 | Deployed |
| GET | /doctors/{doctorId} | patient | Doctor Search | 4 | Deployed |
| GET | /doctors/{doctorId}/slots | patient | Slot Lookup | 4 | Deployed |
| POST | /appointments | patient | Booking | 5 | Planned |
| GET | /appointments, /appointments/{id} | patient | Booking | 5 | Planned |
| DELETE | /appointments/{id} | patient | Cancel | 5 | Planned |
| GET | /patients/me/refunds | patient | Refund History | 5 | Planned |
| POST | /doctors/me/slots/{slotId}/cancel | doctor | Slot Cancel (Step Functions) | 5 | Planned |
| POST | /doctors/me/appointments/{id}/outcome | doctor | Outcome | 6 | Planned |
| PATCH | /appointments/{id} | patient | Reschedule Starter (Step Functions) | 6 | Planned |
| POST, DELETE | /slots/{slotId}/waitlist | patient | Waitlist | 6 | Planned |
| POST | /waitlist-offers/{offerId}/accept | patient | Waitlist | 6 | Planned |

The authorizer already knows the roles for the planned routes, so they need no authorizer change later.

---

## Authentication (Milestone 2)

### POST /auth/patients/register, POST /auth/doctors/register

```json
{
  "full_name": "Asha Rao",
  "email": "asha@example.com",
  "password": "at least 10 chars, a letter and a digit",
  "date_of_birth": "1990-04-02", "gender": "female", "phone": "+91 98450 00000",
  "address": "12 MG Road", "city": "Bengaluru",
  "emergency_contact_name": "Ravi Rao", "emergency_contact_phone": "+91 98450 00001"
}
```

Patient: `full_name`, `email` and `password` are required. Doctor: those plus `licence_number`. Optional doctor
fields are `specialisation` (from the seeded list), `hospital_name`, `hospital_address`, `hospital_city`,
`years_experience`, `qualifications`, `languages`, `timezone` (IANA, default `Asia/Kolkata`).

`201` → `{"message": "Registration successful", "user": {"user_id", "full_name", "email", "role"}, "patient_id" | "doctor_id"}`
`400` invalid field or weak password · `409` email (any role) or licence number already registered · `429`.

### POST /auth/{doctors|patients}/login

`{"email", "password"}` → `200`

```json
{"token_type": "Bearer", "access_token": "<JWT>", "expires_in": 900,
 "refresh_token": "<opaque>", "refresh_expires_in": 604800}
```

`401` for any wrong combination, including the right password on the other portal.

### POST /auth/refresh

`{"refresh_token"}` → `200`, the same shape as login, with a **new** refresh token. The old one stops working.
`401` if unknown, expired, already used, or the account is deactivated.

### POST /auth/logout (protected, both roles)

`{"refresh_token"}` (optional) → `200 {"message": "Logged out"}`. The access token used for this call is
denylisted until it expires; the refresh token is deleted. `400` if the refresh token belongs to another user.

---

## Doctor domain (Milestone 3)

### GET /doctors/me · PUT /doctors/me

`200 {"profile": {doctor_id, user_id, full_name, email, licence_number, specialisation_id, specialisation,
hospital: {hospital_id, name, address, city} | null, years_experience, qualifications, languages, timezone,
photo_url, created_at}}`. `photo_url` is a pre-signed GET valid for 15 minutes.

PUT takes any of `full_name`, `specialisation`, `hospital_name`, `hospital_address`, `hospital_city`,
`years_experience`, `qualifications`, `languages`, `timezone`. Hospitals are matched on (name, city) and created if
new. `licence_number` cannot change (400).
A changed `timezone` moves all future unbooked slots and returns `slot_sync` (below). `409` while the doctor has
upcoming confirmed appointments.

### POST /doctors/me/photo

`{"content_type": "image/jpeg" | "image/png" | "image/webp"}` → `200`

```json
{"upload": {"url": "https://<bucket>.s3.amazonaws.com/", "method": "POST", "fields": {"key": "...", "policy": "...", "...": "..."}},
 "s3_key": "doctor-profiles/<doctorId>/<random>.jpg", "max_bytes": 5242880, "expires_in_seconds": 300}
```

The browser posts the file straight to S3 with those fields. S3 enforces the size (1 byte to 5 MB) and content type.

### Consultation types

The four types are fixed by the brief:

| name | duration_minutes | capacity |
|---|---|---|
| Quick Consult | 15 | 1 |
| Standard Consult | 30 | 1 |
| Detailed Consult | 60 | 1 |
| Health Camp Session | 60 | 2–10 |

- `GET /doctors/me/consultation-types` → `200 {"consultation_types": [{consultation_type_id, name, description, fee,
  duration_minutes, capacity, is_active, created_at, updated_at}]}`
- `POST` `{"name", "fee", "capacity"?, "description"?}` → `201 {"consultation_type": {...}}`. 400 for an unknown name,
  a wrong duration or capacity, or a fee that is not a number from 0 to 99999999.99 (stored with 2 decimals).
- `PUT /{consultationTypeId}` `{"fee"?, "description"?, "capacity"?}` → `200`. `name` and `duration_minutes` cannot
  change (400). Capacity cannot change while an active availability pattern uses the type (409). `404` if the type
  is not the caller's.
- `DELETE /{consultationTypeId}` → `204`. Soft delete (`is_active = 0`); booked appointments keep their type.
  `409` while an active pattern uses it.

### Weekly availability

`GET /doctors/me/availability` → `200 {"timezone": "Asia/Kolkata", "patterns": [{availability_id,
consultation_type_id, consultation_type_name, duration_minutes, day_of_week, start_time, end_time}]}`

`PUT /doctors/me/availability` replaces the whole week:

```json
{"patterns": [
  {"consultation_type_id": 3, "day_of_week": 1, "start_time": "09:00", "end_time": "13:00"},
  {"consultation_type_id": 5, "day_of_week": 6, "start_time": "10:00", "end_time": "14:00"}
]}
```

`day_of_week` is ISO (1 = Monday … 7 = Sunday). Times are local to the doctor's zone. Checks: active type owned by
the caller, end after start, the window holds at least one slot, no overlap on the same day, at most 50 patterns.
`200 {"patterns_saved": 2, "slot_sync": {...}}`

**`slot_sync`** reports what changed in the slots table for the 30-day window:
`{"doctorId", "desired", "created", "alreadyExisted", "removed", "keptBecauseBooked"}`. Slots with booked or held
seats are never removed; they are counted in `keptBecauseBooked`.

### Blackout dates

- `GET /doctors/me/blackouts` → `200 {"blackouts": [{blackout_id, date, reason, created_at}]}`
- `POST` `{"date": "2026-12-25", "reason"?}` → `201 {"blackout": {...}, "slot_sync": {...}}`. The date must be today or
  later in the doctor's zone and within a year; `409` if it is already a blackout date.
- `DELETE /{blackoutId}` → `200 {"message": "Blackout date removed", "slot_sync": {...}}`; `404` if not the caller's.

### GET /doctors/me/schedule?date=YYYY-MM-DD&view=day|week

Defaults: today in the doctor's zone, `day`. `week` is seven days from `date`.

```json
{"doctor_id": "...", "timezone": "Asia/Kolkata", "view": "day", "from": "2026-11-09", "to": "2026-11-09",
 "days": [{"date": "2026-11-09", "slots": [{
   "slot_id": "<doctorId>#3#20261109#0900", "consultation_type_id": 3, "consultation_type_name": "Standard Consult",
   "start_utc": "2026-11-09T03:30:00Z", "end_utc": "2026-11-09T04:00:00Z",
   "local_start": "2026-11-09T09:00:00+05:30", "local_end": "2026-11-09T09:30:00+05:30",
   "status": "OPEN", "capacity": 1, "booked_count": 0, "held_count": 0, "free_seats": 1,
   "patients": [{"appointment_id", "status", "patient_id", "patient_name", "patient_phone"}]}]}]}
```

### GET /doctors/me/appointments?from&to&status

Local dates (default today plus 29 days, at most 92 days). `status` is one of `CONFIRMED`, `COMPLETED`,
`CANCELLED_BY_PATIENT`, `CANCELLED_BY_DOCTOR`, `NO_SHOW`. `200 {"timezone", "from", "to", "appointments": [...]}`.

---

## Patient domain (Milestone 4)

### GET /patients/me · PUT /patients/me · DELETE /patients/me

The patient profile is created at registration, so "create" is `POST /auth/patients/register`.

- GET → `200 {"profile": {patient_id, user_id, full_name, email, date_of_birth, gender, phone, address, city,
  emergency_contact_name, emergency_contact_phone, created_at, updated_at}}`
- PUT any of `full_name`, `date_of_birth` (YYYY-MM-DD), `gender` (from a fixed list), `phone`, `address`, `city`,
  `emergency_contact_name`, `emergency_contact_phone` → `200 {"profile": {...}}`. Email cannot change here.
- DELETE → `200 {"message": "Account deleted"}`, or `"Account deactivated"` when the patient has appointment
  history, which must be kept for the refund ledger and the doctor's records. `409` while upcoming confirmed
  appointments exist. The access token used is denylisted at once.

**Isolation:** there is no `/patients/{id}` route at all. A patient token can reach only `/patients/me`, which
resolves the patient from the token. A doctor token on `/patients/me` gets 403. Tested in
`tests/unit/test_patient_domain.py` and `tests/integration/test_domains.py`.

### GET /doctors?name&hospital&specialisation&city&page&page_size

All filters are optional and combine with AND. `name` and `hospital` match from the start of the name
(`asha` finds "Asha Rao"); `specialisation` and `city` are exact (case-insensitive under the table collation).
`page` starts at 1; `page_size` defaults to 20, at most 50. Any other parameter gets 400.

```json
{"doctors": [{"doctor_id", "full_name", "specialisation", "hospital_name", "hospital_address", "city",
              "years_experience", "qualifications", "languages", "fee_from", "photo_url"}],
 "page": 1, "page_size": 20, "has_more": false}
```

Sorted by name, then doctor ID, so pages are stable. The indexes behind every filter combination are in
[data-model.md](data-model.md#indexes-and-the-queries-they-serve).

### GET /doctors/{doctorId}

Public profile, without email or licence number: `200 {"doctor": {doctor_id, full_name, specialisation,
hospital_name, hospital_address, city, years_experience, qualifications, languages, timezone, photo_url,
consultation_types: [{consultation_type_id, name, description, duration_minutes, capacity, fee}]}}`.
Only active types are listed. `404` for an unknown or deactivated doctor.

### GET /doctors/{doctorId}/slots?from&to&consultationTypeId

Local dates in the doctor's zone, default today plus 6 days, at most 31 days. Returns every future `OPEN` slot for
an active type, **with its free seats**, so a full slot is listed with `free_seats: 0` (the patient can join its
waitlist from Milestone 6).

```json
{"doctor_id": "...", "timezone": "Asia/Kolkata", "from": "2026-11-09", "to": "2026-11-15",
 "slots": [{"slot_id", "consultation_type_id", "consultation_type_name", "fee", "start_utc", "end_utc",
            "local_start", "local_end", "timezone", "status", "capacity", "booked_count", "held_count",
            "free_seats"}]}
```

`free_seats = capacity − booked_count − held_count`. Read from DynamoDB `GSI_Doctor_Slots`.

---

## Internal functions (not behind API Gateway)

| Function | Invoked by | Input |
|---|---|---|
| `medislot-<env>-slot-materialiser` | EventBridge Scheduler daily at 00:15 UTC; Availability Lambdas call the same code inline | `{}` for every doctor with active patterns, or `{"doctorId": "..."}` |
| `medislot-<env>-db-migrate` | Deploy pipeline | `{"action": "check"}`, `{"action": "status"}` or `{"action": "migrate"}` |
