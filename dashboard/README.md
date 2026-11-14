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
  MediSlot HTTP API (`api_client.py`). The instance role can only read its package in S3; it reads nothing from SSM.
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
| API address | `MEDISLOT_API_BASE_URL` (the API stack's `ApiUrl` output, passed in by the pipeline) | `MEDISLOT_API_BASE_URL` |
| Session key | `MEDISLOT_SESSION_SECRET_FILE` (`/etc/medislot/session-secret`, generated at first boot, mode 600) | `MEDISLOT_SESSION_SECRET` |
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
package. On the instance, systemd runs the app with Flask's own threaded server on port 80
(`flask --app dashboard.wsgi:app run --host 0.0.0.0 --port 80 --with-threads`); there is no separate WSGI server. Expect about a minute of downtime per deploy.

## Logs

`weblog.py` writes one JSON line per page request (endpoint, status, duration, caller), per API call (path, status,
duration, API request id) and per login, registration and logout, each with the correlation id that is also sent to
the API. Passwords, tokens, emails and form contents are never logged. systemd appends the lines to
`/var/log/medislot/web.log`, and the CloudWatch agent ships that file and the boot script's log to the CloudWatch Logs
group `/medislot/<env>/web` (streams `<instance id>/web` and `<instance id>/bootstrap`, kept 30 days).
