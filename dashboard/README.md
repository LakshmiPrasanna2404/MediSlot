# MediSlot web app (doctor and patient dashboards)

One Flask application on one EC2 instance (`infrastructure/dashboard-stack.yaml`) serving both user types.

| Route | Who | What |
|---|---|---|
| `/` | anyone | Landing page: **Login as Doctor** / **Login as Patient** |
| `/doctor/login`, `/doctor/register` | anyone | Doctor login and registration |
| `/patient/login`, `/patient/register` | anyone | Patient login and registration |
| `/doctor/*` | doctor sessions | Schedule (day and week, booked patients per slot), profile and photo, consultation types, weekly availability, blackout dates |
| `/patient/*` | patient sessions | Doctor search with filters and pages, doctor profile with fees and a slot browser, own profile |
| `POST /logout` | signed in | Calls `POST /auth/logout` (revokes the refresh token and the access token) and clears the session |

## How it works

- **Pure API client.** No database credentials and no database connection. Every read and write is a call to the
  MediSlot REST API (`api_client.py`). The instance role can only read its own settings in SSM and its package in S3.
- **Server-side sessions** (Flask-Session, files under `/var/lib/medislot/sessions`). The JWT access token and the
  refresh token stay on the server; the browser only holds a random session id (HttpOnly, SameSite=Lax).
- **Route guards** (`security.py`). The role claim of the JWT in the session decides the portal. A patient session on
  any `/doctor/*` page goes back to the landing page, and the reverse. No session goes to that portal's login page.
- **Token refresh.** When the API answers 401 the app rotates the refresh token once and retries; if that fails the
  user is logged out.
- **CSRF.** Every form carries a per-session token that is checked on POST.
- **Correlation ID.** Each page view gets an ID that is sent to the API as `X-Correlation-Id`, so web, API Gateway and
  Lambda logs for one click can be found together.
- **Photos** go from the browser straight to S3 with a pre-signed POST (`static/photo.js`), never through the app.

## Settings

| Variable | On EC2 | Locally |
|---|---|---|
| API address | `MEDISLOT_API_BASE_URL_PARAMETER` (SSM `/medislot/<env>/api/base-url`, written by the API stack) | `MEDISLOT_API_BASE_URL` |
| Session key | `MEDISLOT_SESSION_SECRET_PARAMETER` (SSM SecureString `/medislot/<env>/dashboard/session-secret`) | `MEDISLOT_SESSION_SECRET` |
| Session files | `MEDISLOT_SESSION_DIR` | defaults to `/tmp/medislot-sessions` |

## Run locally against the dev API

```bash
pip install -r dashboard/requirements.txt
export MEDISLOT_API_BASE_URL=https://<api-id>.execute-api.<region>.amazonaws.com/dev
export MEDISLOT_SESSION_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
flask --app dashboard.wsgi run --port 8080
```

Tests: `python -m pytest tests/unit/test_web.py` (the API is mocked).

## Deploying

The pipeline zips this folder, uploads it to the artifacts bucket and passes the key to the dashboard stack. A new
key changes the instance's boot script, CloudFormation restarts the instance and the script installs the new
package. Expect about a minute of downtime per deploy. Boot logs: EC2 console, Actions > Monitor and troubleshoot >
Get system log. Application logs: connect with Session Manager (there is no SSH) and run
`journalctl -u medislot-web -n 200`.
