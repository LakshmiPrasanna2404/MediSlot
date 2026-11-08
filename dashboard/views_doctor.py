"""Doctor dashboard (/doctor/*): schedule, profile, consultation types, availability, blackouts."""

from datetime import date

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import api_client
from .api_client import ApiError
from .security import role_required
from .views_auth import form_body

bp = Blueprint("doctor", __name__, url_prefix="/doctor")
PROFILE_FIELDS = ["full_name", "specialisation", "hospital_name", "hospital_address", "hospital_city",
                  "years_experience", "qualifications", "languages", "timezone"]


def _sync_message(summary):
    if not summary:
        return ""
    parts = [f"{summary.get('created', 0)} slot(s) added", f"{summary.get('removed', 0)} removed"]
    kept = summary.get("keptBecauseBooked") or []
    if kept:
        parts.append(f"{len(kept)} kept because patients are booked (cancel those slots separately)")
    return " (" + ", ".join(parts) + ")"


@bp.get("/")
@role_required("doctor")
def home():
    return redirect(url_for("doctor.schedule"))


@bp.get("/schedule")
@role_required("doctor")
def schedule():
    view = request.args.get("view", "day")
    params = {"view": view if view in ("day", "week") else "day"}
    if request.args.get("date"):
        params["date"] = request.args["date"]
    try:
        data = api_client.call("GET", "/doctors/me/schedule", params=params)
    except ApiError as exc:
        flash(exc.message, "error")
        data = {"days": [], "view": params["view"], "timezone": "", "from": date.today().isoformat()}
    return render_template("doctor/schedule.html", data=data, view=params["view"])


@bp.route("/profile", methods=["GET", "POST"])
@role_required("doctor")
def profile():
    if request.method == "POST":
        body = form_body(PROFILE_FIELDS)
        try:
            result = api_client.call("PUT", "/doctors/me", body)
            flash("Profile saved" + _sync_message(result.get("slot_sync")), "success")
            return redirect(url_for("doctor.profile"))
        except ApiError as exc:
            flash(exc.message, "error")
    data = api_client.call("GET", "/doctors/me")
    return render_template("doctor/profile.html", profile=data["profile"])


@bp.post("/profile/photo-upload")
@role_required("doctor")
def photo_upload():
    """Returns the pre-signed POST; the browser then sends the image straight to S3."""
    try:
        result = api_client.call("POST", "/doctors/me/photo", {"content_type": request.form.get("content_type", "")})
    except ApiError as exc:
        return jsonify({"error": exc.message}), exc.status
    return jsonify(result)


@bp.route("/consultation-types", methods=["GET", "POST"])
@role_required("doctor")
def consultation_types():
    if request.method == "POST":
        body = {k: v for k, v in {
            "name": request.form.get("name"),
            "fee": request.form.get("fee"),
            "description": request.form.get("description", "").strip() or None,
        }.items() if v}
        if request.form.get("capacity"):
            body["capacity"] = int(request.form["capacity"]) if request.form["capacity"].isdigit() else request.form["capacity"]
        try:
            api_client.call("POST", "/doctors/me/consultation-types", body)
            flash("Consultation type created", "success")
            return redirect(url_for("doctor.consultation_types"))
        except ApiError as exc:
            flash(exc.message, "error")
    data = api_client.call("GET", "/doctors/me/consultation-types")
    return render_template("doctor/consultation_types.html", types=data["consultation_types"])


@bp.post("/consultation-types/<int:type_id>/update")
@role_required("doctor")
def update_consultation_type(type_id):
    body = {"fee": request.form.get("fee"), "description": request.form.get("description", "")}
    if request.form.get("capacity"):
        body["capacity"] = int(request.form["capacity"]) if request.form["capacity"].isdigit() else request.form["capacity"]
    try:
        api_client.call("PUT", f"/doctors/me/consultation-types/{type_id}", body)
        flash("Consultation type updated", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("doctor.consultation_types"))


@bp.post("/consultation-types/<int:type_id>/delete")
@role_required("doctor")
def delete_consultation_type(type_id):
    try:
        api_client.call("DELETE", f"/doctors/me/consultation-types/{type_id}")
        flash("Consultation type removed", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("doctor.consultation_types"))


def _patterns_from_form():
    patterns = []
    rows = zip(
        request.form.getlist("consultation_type_id"),
        request.form.getlist("day_of_week"),
        request.form.getlist("start_time"),
        request.form.getlist("end_time"),
        strict=False,
    )
    for type_id, day, start, end in rows:
        if not (type_id and day and start and end):
            continue  # blank row
        patterns.append({
            "consultation_type_id": int(type_id) if type_id.isdigit() else type_id,
            "day_of_week": int(day) if day.isdigit() else day,
            "start_time": start[:5],
            "end_time": end[:5],
        })
    return patterns


@bp.route("/availability", methods=["GET", "POST"])
@role_required("doctor")
def availability():
    if request.method == "POST":
        try:
            result = api_client.call("PUT", "/doctors/me/availability", {"patterns": _patterns_from_form()})
            flash(f"Weekly availability saved: {result['patterns_saved']} pattern(s)" + _sync_message(result.get("slot_sync")),
                  "success")
            return redirect(url_for("doctor.availability"))
        except ApiError as exc:
            flash(exc.message, "error")
    data = api_client.call("GET", "/doctors/me/availability")
    types = [t for t in api_client.call("GET", "/doctors/me/consultation-types")["consultation_types"] if t["is_active"]]
    return render_template("doctor/availability.html", data=data, types=types)


@bp.route("/blackouts", methods=["GET", "POST"])
@role_required("doctor")
def blackouts():
    if request.method == "POST":
        body = {"date": request.form.get("date", "")}
        if request.form.get("reason", "").strip():
            body["reason"] = request.form["reason"].strip()
        try:
            result = api_client.call("POST", "/doctors/me/blackouts", body)
            flash("Blackout date added" + _sync_message(result.get("slot_sync")), "success")
            return redirect(url_for("doctor.blackouts"))
        except ApiError as exc:
            flash(exc.message, "error")
    data = api_client.call("GET", "/doctors/me/blackouts")
    return render_template("doctor/blackouts.html", blackouts=data["blackouts"], today=date.today().isoformat())


@bp.post("/blackouts/<int:blackout_id>/delete")
@role_required("doctor")
def delete_blackout(blackout_id):
    try:
        result = api_client.call("DELETE", f"/doctors/me/blackouts/{blackout_id}")
        flash("Blackout date removed" + _sync_message(result.get("slot_sync")), "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("doctor.blackouts"))
