"""Route guards and CSRF protection."""

import hmac
import secrets
from functools import wraps

from flask import abort, flash, redirect, request, session, url_for

from .api_client import SessionExpired


def init_csrf(app):
    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf_token")
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not sent or not hmac.compare_digest(expected, sent):
                abort(400, "Form expired, please try again")

    @app.context_processor
    def csrf_field():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"], "session_role": session.get("role"),
                "session_name": session.get("name")}


def role_required(role):
    """Guard a whole portal.

    No session: go to that role's login page. A session for the other role
    (a patient on /doctor/*, or the reverse): back to the landing page.
    An expired session found while calling the API: log in again.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            current = session.get("role")
            if current is None:
                return redirect(url_for(f"auth.{role}_login"))
            if current != role:
                flash("That page belongs to the other portal.", "warning")
                return redirect(url_for("auth.landing"))
            try:
                return view(*args, **kwargs)
            except SessionExpired:
                flash("Your session has ended. Please log in again.", "warning")
                return redirect(url_for(f"auth.{role}_login"))

        return wrapper

    return decorator
