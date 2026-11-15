"""Patient dashboard (/patient/*): doctor search, doctor profile with slot browser, booking,
own appointments with cancellation, refund history, own profile."""

import uuid
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import api_client
from .api_client import ApiError
from .security import role_required
from .views_auth import form_body

bp = Blueprint("patient", __name__, url_prefix="/patient")
PROFILE_FIELDS = ["first_name", "last_name", "date_of_birth", "gender", "phone", "address", "city",
                  "emergency_contact_name", "emergency_contact_phone"]
SEARCH_FILTERS = ("name", "hospital", "specialisation", "city")
APPOINTMENT_VIEWS = ("upcoming", "past")


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


def _find_slot(slot_id):
    """The open slot behind a slot id (<doctorId>#<UTC start>), from the doctor's slot lookup."""
    doctor_id, _, start = slot_id.partition("#")
    try:
        start_day = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").date()
    except ValueError:
        return None, None
    # Slot lookup takes dates in the doctor's time zone; one day either side covers every offset.
    params = {"from": (start_day - timedelta(days=1)).isoformat(), "to": (start_day + timedelta(days=1)).isoformat()}
    result = api_client.call("GET", f"/doctors/{doctor_id}/slots", params=params)
    for slot in result.get("slots", []):
        if slot["slot_id"] == slot_id:
            return doctor_id, slot
    return doctor_id, None


@bp.get("/book")
@role_required("patient")
def book():
    slot_id = request.args.get("slot_id", "")
    doctor_id, slot = _find_slot(slot_id)
    if not doctor_id:
        flash("That slot could not be found.", "error")
        return redirect(url_for("patient.search"))
    if not slot or slot.get("free_seats", 0) <= 0:
        flash("That time is no longer available. Please choose another one.", "warning")
        return redirect(url_for("patient.doctor", doctor_id=doctor_id))
    profile = api_client.call("GET", f"/doctors/{doctor_id}")["doctor"]
    # One key per confirmation page: pressing the button twice books once.
    return render_template("patient/book.html", slot=slot, doctor=profile, idempotency_key=str(uuid.uuid4()))


@bp.post("/book")
@role_required("patient")
def book_confirm():
    slot_id = request.form.get("slot_id", "")
    doctor_id = slot_id.partition("#")[0]
    try:
        result = api_client.call("POST", "/appointments", {"slot_id": slot_id},
                                 headers={"Idempotency-Key": request.form.get("idempotency_key", "")})
    except ApiError as exc:
        flash(exc.message, "error")
        if doctor_id:
            return redirect(url_for("patient.doctor", doctor_id=doctor_id))
        return redirect(url_for("patient.search"))
    appointment = result["appointment"]
    flash(f"Booked: {appointment['consultation_type_name']} with Dr. {appointment['doctor']['first_name']} "
          f"{appointment['doctor']['last_name']}. A confirmation email is on its way.", "success")
    return redirect(url_for("patient.appointments", highlight=appointment["appointment_id"]))


@bp.get("/appointments")
@role_required("patient")
def appointments():
    show = request.args.get("show", "upcoming")
    show = show if show in APPOINTMENT_VIEWS else "upcoming"
    items = []
    try:
        items = api_client.call("GET", "/appointments", params={"status": show})["appointments"]
    except ApiError as exc:
        flash(exc.message, "error")
    return render_template("patient/appointments.html", appointments=items, show=show,
                           highlight=request.args.get("highlight"))


@bp.post("/appointments/<appointment_id>/cancel")
@role_required("patient")
def cancel(appointment_id):
    try:
        result = api_client.call("DELETE", f"/appointments/{appointment_id}")
    except ApiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.appointments"))
    refund = result.get("refund") or {}
    amount = refund.get("refund_amount", 0)
    if amount:
        message = f"Appointment cancelled. {refund.get('refund_percent')}% of the fee is refunded: ₹{float(amount):,.2f}."
    else:
        message = "Appointment cancelled. It was too close to the start for a refund."
    flash(message, "success")
    return redirect(url_for("patient.appointments"))


@bp.get("/refunds")
@role_required("patient")
def refunds():
    page = request.args.get("page", "1")
    page = page if page.isdigit() and int(page) > 0 else "1"
    data = {"refunds": [], "page": 1, "has_more": False, "totals": {}}
    try:
        data = api_client.call("GET", "/patients/me/refunds", params={"page": page})
    except ApiError as exc:
        flash(exc.message, "error")
    return render_template("patient/refunds.html", data=data)


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
