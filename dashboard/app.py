"""MediSlot operations dashboard.

A read-only admin page. One admin user signs in; the page then reads
CloudWatch metrics, alarms and recent Lambda error lines through
/api/summary. The app never writes to AWS.

Credentials live in SSM SecureString parameters under
/medislot/{env}/dashboard/: username, password-hash (bcrypt) and
session-secret. See dashboard/README.md.

The page is served over plain HTTP, so network access (the EC2 security
group allows one admin CIDR) is the real protection and the login is a
second layer.
"""

import hmac
import logging
import secrets
import threading
import time
from datetime import timedelta

import bcrypt
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .collectors import RANGES, CloudWatchCollector
from .settings import Settings

log = logging.getLogger(__name__)

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
SESSION_HOURS = 8
FIELD_LIMIT = 200
NOT_CONFIGURED = (
    "The dashboard is not configured yet. Create the SSM parameters "
    "described in dashboard/README.md."
)

# Checked when the username is wrong, so a wrong username and a wrong
# password cost the same time.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_hex(16).encode("ascii"), bcrypt.gensalt(12))

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class Credentials:
    def __init__(self, username, password_hash, session_secret):
        self.username = username
        self.password_hash = password_hash
        self.session_secret = session_secret


def load_credentials_from_ssm(settings):
    import boto3

    ssm = boto3.client("ssm", region_name=settings.region)
    names = {
        key: f"{settings.ssm_prefix}/{key}"
        for key in ("username", "password-hash", "session-secret")
    }
    response = ssm.get_parameters(Names=list(names.values()), WithDecryption=True)
    found = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
    missing = [name for name in names.values() if name not in found]
    if missing:
        raise RuntimeError("Missing SSM parameters: " + ", ".join(missing))
    password_hash = found[names["password-hash"]]
    session_secret = found[names["session-secret"]]
    if not password_hash.startswith("$2"):
        raise RuntimeError("password-hash must be a bcrypt hash")
    if len(session_secret) < 32:
        raise RuntimeError("session-secret must be at least 32 characters")
    return Credentials(found[names["username"]], password_hash, session_secret)


def create_app(settings=None, collector=None, credentials_loader=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config.update(
        SESSION_COOKIE_NAME="medislot_dashboard",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.secure_cookie,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    # Replaced by the SSM value as soon as the credentials load.
    app.secret_key = secrets.token_hex(32)

    loader = credentials_loader or (lambda: load_credentials_from_ssm(settings))
    state = {"credentials": None, "collector": collector}
    lock = threading.Lock()
    failures = {}

    def credentials():
        with lock:
            if state["credentials"] is None:
                try:
                    state["credentials"] = loader()
                    app.secret_key = state["credentials"].session_secret
                except Exception:  # noqa: BLE001 - reported as "not configured"
                    log.exception("Could not load dashboard credentials")
            return state["credentials"]

    def get_collector():
        with lock:
            if state["collector"] is None:
                state["collector"] = CloudWatchCollector(settings)
            return state["collector"]

    # -- helpers -------------------------------------------------------
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def csrf_ok():
        expected = session.get("csrf", "")
        sent = request.form.get("csrf", "")
        return bool(expected) and hmac.compare_digest(expected, sent)

    def recent_failures(ip, now):
        window = [t for t in failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
        if window:
            failures[ip] = window
        else:
            failures.pop(ip, None)
        return window

    def password_matches(creds, username, password):
        user_ok = hmac.compare_digest(username.encode("utf-8"), creds.username.encode("utf-8"))
        stored = creds.password_hash.encode("utf-8") if user_ok else _DUMMY_HASH
        try:
            password_ok = bcrypt.checkpw(password.encode("utf-8"), stored)
        except ValueError:
            password_ok = False
        return user_ok and password_ok

    def login_page(error=None, status=200, configured=True):
        return (
            render_template(
                "login.html",
                csrf=csrf_token(),
                error=error,
                configured=configured,
                environment=settings.environment,
            ),
            status,
        )

    def signed_in():
        return session.get("auth") is True

    # -- security headers ---------------------------------------------
    @app.after_request
    def add_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- routes --------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        if credentials() is None:
            return "not configured\n", 503, {"Content-Type": "text/plain; charset=utf-8"}
        return "ok\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        # Load first, so the session secret is final before the CSRF token is issued.
        creds = credentials()
        if request.method == "GET":
            if creds is None:
                return login_page(NOT_CONFIGURED, 503, configured=False)
            if signed_in():
                return redirect(url_for("index"))
            return login_page()

        if creds is None:
            return login_page(NOT_CONFIGURED, 503, configured=False)
        if not csrf_ok():
            return login_page("Your session expired. Please try again.", 400)

        now = time.time()
        ip = request.remote_addr or "unknown"
        if len(recent_failures(ip, now)) >= MAX_FAILURES:
            return login_page("Too many failed attempts. Try again in 15 minutes.", 429)

        username = request.form.get("username", "")[:FIELD_LIMIT]
        password = request.form.get("password", "")[:FIELD_LIMIT]
        if not password_matches(creds, username, password):
            failures.setdefault(ip, []).append(now)
            log.warning("Failed dashboard sign-in from %s", ip)
            return login_page("Invalid username or password.", 401)

        failures.pop(ip, None)
        session.clear()
        session["auth"] = True
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        if not csrf_ok():
            return redirect(url_for("login"))
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        if not signed_in():
            return redirect(url_for("login"))
        return render_template(
            "dashboard.html",
            csrf=csrf_token(),
            environment=settings.environment,
            region=settings.region,
        )

    @app.get("/api/summary")
    def api_summary():
        if not signed_in():
            return jsonify(error="Sign in required"), 401
        range_key = request.args.get("range", "1h")
        if range_key not in RANGES:
            return jsonify(error="range must be one of: " + ", ".join(RANGES)), 400
        try:
            return jsonify(get_collector().summary(range_key))
        except Exception:  # noqa: BLE001 - details go to the server log only
            log.exception("Could not collect dashboard data")
            return jsonify(error="Could not read CloudWatch right now."), 502

    credentials()
    return app
