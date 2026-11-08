"""Landing page, login, registration and logout for both roles."""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import api_client
from .api_client import ApiError

bp = Blueprint("auth", __name__)

PORTAL = {"doctor": "doctor.schedule", "patient": "patient.search"}

DOCTOR_FIELDS = ["full_name", "email", "password", "licence_number", "specialisation", "hospital_name",
                 "hospital_address", "hospital_city", "years_experience", "qualifications", "languages", "timezone"]
PATIENT_FIELDS = ["full_name", "email", "password", "date_of_birth", "gender", "phone", "address", "city",
                  "emergency_contact_name", "emergency_contact_phone"]


def form_body(fields):
    """Non-empty form values only; numbers stay strings (the API validates them)."""
    body = {}
    for field in fields:
        value = request.form.get(field, "").strip() if field != "password" else request.form.get(field, "")
        if value != "":
            body[field] = value
    if "years_experience" in body and body["years_experience"].isdigit():
        body["years_experience"] = int(body["years_experience"])
    return body


@bp.get("/")
def landing():
    return render_template("landing.html")


def _login(role):
    if request.method == "POST":
        try:
            tokens = api_client.public_call(
                "POST", f"/auth/{role}s/login",
                {"email": request.form.get("email", ""), "password": request.form.get("password", "")},
            )
        except ApiError as exc:
            flash(exc.message, "error")
            return render_template("login.html", role=role, email=request.form.get("email", "")), exc.status
        session.clear()
        api_client.store_tokens(tokens)
        session["name"] = tokens["user"]["full_name"]
        session["user_id"] = tokens["user"]["user_id"]
        if session["role"] != role:  # never expected: the API issues the token for this portal
            session.clear()
            flash("Wrong portal for this account.", "error")
            return redirect(url_for("auth.landing"))
        return redirect(url_for(PORTAL[role]))
    return render_template("login.html", role=role)


def _register(role):
    if request.method == "POST":
        body = form_body(DOCTOR_FIELDS if role == "doctor" else PATIENT_FIELDS)
        try:
            api_client.public_call("POST", f"/auth/{role}s/register", body)
        except ApiError as exc:
            message = exc.message
            if isinstance(exc.details, dict) and exc.details.get("allowed"):
                message += ": choose one of " + ", ".join(exc.details["allowed"])
            flash(message, "error")
            return render_template(f"register_{role}.html", role=role, form=request.form), exc.status
        flash("Account created. Please log in.", "success")
        return redirect(url_for(f"auth.{role}_login"))
    return render_template(f"register_{role}.html", role=role, form={})


@bp.route("/doctor/login", methods=["GET", "POST"])
def doctor_login():
    return _login("doctor")


@bp.route("/patient/login", methods=["GET", "POST"])
def patient_login():
    return _login("patient")


@bp.route("/doctor/register", methods=["GET", "POST"])
def doctor_register():
    return _register("doctor")


@bp.route("/patient/register", methods=["GET", "POST"])
def patient_register():
    return _register("patient")


@bp.post("/logout")
def logout():
    api_client.logout()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.landing"))
