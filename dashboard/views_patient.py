"""Patient dashboard (/patient/*): doctor search, doctor profile with slot browser, own profile."""

from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import api_client
from .api_client import ApiError
from .security import role_required
from .views_auth import form_body

bp = Blueprint("patient", __name__, url_prefix="/patient")
PROFILE_FIELDS = ["first_name", "last_name", "date_of_birth", "gender", "phone", "address", "city",
                  "emergency_contact_name", "emergency_contact_phone"]
SEARCH_FILTERS = ("name", "hospital", "specialisation", "city")


@bp.get("/")
@role_required("patient")
def home():
    return redirect(url_for("patient.search"))


@bp.get("/search")
@role_required("patient")
def search():
    filters = {key: request.args.get(key, "").strip() for key in SEARCH_FILTERS}
    params = {key: value for key, value in filters.items() if value}
    page = request.args.get("page", "1")
    params["page"] = page if page.isdigit() and int(page) > 0 else "1"
    result = {"doctors": [], "page": 1, "has_more": False}
    try:
        result = api_client.call("GET", "/doctors", params=params)
    except ApiError as exc:
        flash(exc.message, "error")
    return render_template("patient/search.html", filters=filters, result=result)


@bp.get("/doctors/<doctor_id>")
@role_required("patient")
def doctor(doctor_id):
    profile = api_client.call("GET", f"/doctors/{doctor_id}")["doctor"]
    start = request.args.get("from") or date.today().isoformat()
    try:
        end = request.args.get("to") or (date.fromisoformat(start) + timedelta(days=6)).isoformat()
    except ValueError:
        start, end = date.today().isoformat(), (date.today() + timedelta(days=6)).isoformat()
    params = {"from": start, "to": end}
    if request.args.get("consultationTypeId"):
        params["consultationTypeId"] = request.args["consultationTypeId"]
    slots = {"slots": [], "timezone": profile.get("timezone")}
    try:
        slots = api_client.call("GET", f"/doctors/{doctor_id}/slots", params=params)
    except ApiError as exc:
        flash(exc.message, "error")
    by_day = {}
    for slot in slots["slots"]:
        by_day.setdefault(slot["local_start"][:10], []).append(slot)
    return render_template("patient/doctor.html", doctor=profile, slots_by_day=by_day, timezone=slots.get("timezone"),
                           params=params)


@bp.route("/profile", methods=["GET", "POST"])
@role_required("patient")
def profile():
    if request.method == "POST":
        try:
            api_client.call("PUT", "/patients/me", form_body(PROFILE_FIELDS))
            flash("Profile saved", "success")
            return redirect(url_for("patient.profile"))
        except ApiError as exc:
            flash(exc.message, "error")
    data = api_client.call("GET", "/patients/me")
    return render_template("patient/profile.html", profile=data["profile"])


@bp.post("/profile/delete")
@role_required("patient")
def delete_account():
    try:
        result = api_client.call("DELETE", "/patients/me")
    except ApiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.profile"))
    api_client.logout()
    flash(result.get("message", "Account removed"), "success")
    return redirect(url_for("auth.landing"))
