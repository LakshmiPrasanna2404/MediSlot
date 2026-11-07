"""MediSlot web application: one Flask app, two dashboards.

    /                 landing page: Login as Doctor / Login as Patient
    /doctor/*         doctor dashboard (doctor sessions only)
    /patient/*        patient dashboard (patient sessions only)

The app is a pure API client. It has no database credentials and makes no
database connection: every read and write is a call to the MediSlot REST API
with the JWT kept in a server-side session (only a random session id goes to
the browser).
"""

import uuid

from flask import Flask, g, render_template, request
from flask_session import Session

from . import settings
from .api_client import ApiError
from .security import init_csrf


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(settings.load(test_config))
    Session(app)
    init_csrf(app)

    from .views_auth import bp as auth_bp
    from .views_doctor import bp as doctor_bp
    from .views_patient import bp as patient_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(doctor_bp)
    app.register_blueprint(patient_bp)

    from . import formatting

    formatting.register(app)

    @app.before_request
    def correlation_id():
        # Generated at the edge for every page view and sent to the API with
        # each call, so web, API Gateway and Lambda logs share one ID.
        g.correlation_id = request.headers.get("X-Correlation-Id") or f"web-{uuid.uuid4()}"

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers["X-Correlation-Id"] = g.get("correlation_id", "")
        return response

    @app.errorhandler(ApiError)
    def api_error(exc):
        return render_template("error.html", status=exc.status, message=exc.message), 502 if exc.status >= 500 else exc.status

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", status=404, message="Page not found"), 404

    return app
