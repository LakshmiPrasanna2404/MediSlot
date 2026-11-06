"""Tests for the MediSlot operations dashboard (Flask app + collectors)."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.app import MAX_FAILURES, Credentials, create_app  # noqa: E402
from dashboard.collectors import (  # noqa: E402
    READ_ONLY_CALLS,
    CloudWatchCollector,
    StaticCollector,
    redact,
)
from dashboard.settings import Settings  # noqa: E402

USER = "admin"
PASSWORD = "correct horse battery 7"
SETTINGS = Settings(environment="dev", region="us-east-1", api_id="abc123")


@pytest.fixture(scope="module")
def password_hash():
    return bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()


def make_client(password_hash, loader=None):
    creds = Credentials(USER, password_hash, "s" * 40)
    app = create_app(
        SETTINGS,
        collector=StaticCollector(SETTINGS),
        credentials_loader=loader or (lambda: creds),
    )
    return app.test_client()


@pytest.fixture
def client(password_hash):
    return make_client(password_hash)


def csrf_from(client, path="/login"):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def sign_in(client, user=USER, password=PASSWORD):
    return client.post(
        "/login",
        data={"username": user, "password": password, "csrf": csrf_from(client)},
    )


# ---- access control ------------------------------------------------------
def test_dashboard_requires_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_api_requires_login(client):
    assert client.get("/api/summary").status_code == 401


def test_login_success_then_dashboard_and_api(client):
    response = sign_in(client)
    assert response.status_code == 302
    page = client.get("/")
    assert page.status_code == 200
    assert b"MediSlot" in page.data
    assert client.get("/api/summary?range=1h").status_code == 200


@pytest.mark.parametrize(
    "user,password", [(USER, "wrong"), ("someone", PASSWORD), ("", ""), (USER, "")]
)
def test_login_failure_is_generic(client, user, password):
    response = sign_in(client, user, password)
    assert response.status_code == 401
    assert b"Invalid username or password" in response.data
    assert client.get("/api/summary").status_code == 401


def test_login_rejects_missing_csrf(client):
    client.get("/login")
    response = client.post("/login", data={"username": USER, "password": PASSWORD})
    assert response.status_code == 400
    assert client.get("/api/summary").status_code == 401


def test_lockout_after_repeated_failures(client):
    for _ in range(MAX_FAILURES):
        assert sign_in(client, password="nope").status_code == 401
    # Even the right password is refused while locked out.
    assert sign_in(client).status_code == 429


def test_logout_needs_csrf_then_clears_session(client):
    sign_in(client)
    client.post("/logout", data={})
    assert client.get("/api/summary").status_code == 200  # still signed in
    token = csrf_from(client, "/")
    client.post("/logout", data={"csrf": token})
    assert client.get("/api/summary").status_code == 401


def test_logout_rejects_get(client):
    assert client.get("/logout").status_code == 405


def test_not_configured_reports_503(password_hash):
    def broken():
        raise RuntimeError("no parameters")

    c = make_client(password_hash, loader=broken)
    assert c.get("/healthz").status_code == 503
    response = c.get("/login")
    assert b"not configured" in response.data
    assert b'name="password"' not in response.data


def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.data.strip() == b"ok"


def test_security_headers(client):
    response = client.get("/login")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "HttpOnly" in sign_in(client).headers["Set-Cookie"]


def test_pages_use_no_inline_script_or_style():
    for name in ("login.html", "dashboard.html"):
        text = (Path(__file__).resolve().parents[1] / "dashboard" / "templates" / name).read_text(
            encoding="utf-8"
        )
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), name
        assert "<style" not in text and " style=" not in text, name
        assert " onclick=" not in text, name


def test_front_end_never_uses_innerhtml():
    js = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    assert "innerHTML" not in js
    assert "outerHTML" not in js
    assert "insertAdjacentHTML" not in js


# ---- summary shape --------------------------------------------------------
@pytest.mark.parametrize("range_key,points", [("1h", 60), ("6h", 72), ("24h", 96)])
def test_summary_shape(client, range_key, points):
    sign_in(client)
    body = client.get(f"/api/summary?range={range_key}").get_json()
    assert len(body["timestamps"]) == points
    for series in body["api"]["series"].values():
        assert len(series) == points
    for key in ("auth", "authorizer", "app"):
        assert len(body["lambdas"][key]["series"]["errors"]) == points
    for key in ("slots", "seats"):
        assert len(body["dynamodb"][key]["series"]["conditional_check_failures"]) == points
    assert len(body["rds"]["series"]["cpu"]) == points
    for panel in ("alarms", "errors", "custom"):
        assert {"available", "items"} <= set(body[panel])


def test_summary_rejects_bad_range(client):
    sign_in(client)
    assert client.get("/api/summary?range=7d").status_code == 400
    assert client.get("/api/summary?range=").status_code == 400


def test_summary_failure_hides_details(password_hash):
    class Boom:
        def summary(self, _):
            raise RuntimeError("secret arn:aws:iam::123456789012:role/x")

    creds = Credentials(USER, password_hash, "s" * 40)
    app = create_app(SETTINGS, collector=Boom(), credentials_loader=lambda: creds)
    c = app.test_client()
    sign_in(c)
    response = c.get("/api/summary")
    assert response.status_code == 502
    assert b"123456789012" not in response.data


# ---- CloudWatch collector -------------------------------------------------
class FakeCloudWatch:
    def __init__(self, metric_values=None, alarms=None, fail_alarms=False):
        self.calls = []
        self.metric_values = metric_values or {}
        self.alarms = alarms or []
        self.fail_alarms = fail_alarms

    def get_metric_data(self, **kwargs):
        self.calls.append("get_metric_data")
        assert len(kwargs["MetricDataQueries"]) <= 500
        results = []
        for query in kwargs["MetricDataQueries"]:
            qid = query["Id"]
            if qid in self.metric_values:
                ts, value = self.metric_values[qid]
                results.append({"Id": qid, "Timestamps": [ts], "Values": [value]})
            else:
                results.append({"Id": qid, "Timestamps": [], "Values": []})
        return {"MetricDataResults": results}

    def list_metrics(self, **kwargs):
        self.calls.append("list_metrics")
        return {"Metrics": []}

    def describe_alarms(self, **kwargs):
        self.calls.append("describe_alarms")
        if self.fail_alarms:
            raise RuntimeError("AccessDenied")
        assert kwargs["AlarmNamePrefix"] == "medislot-dev-"
        return {"MetricAlarms": self.alarms}


class FakeLogs:
    def __init__(self):
        self.calls = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs["logGroupName"])
        return {"events": []}


NOW = 1_760_000_000.0


def collector(cw, logs=None, cache=120, clock=lambda: NOW):
    settings = Settings(environment="dev", api_id="abc123", cache_seconds=cache)
    return CloudWatchCollector(settings, cloudwatch=cw, logs=logs or FakeLogs(), clock=clock)


def test_collector_uses_only_read_only_calls():
    cw, logs = FakeCloudWatch(), FakeLogs()
    collector(cw, logs).summary("1h")
    assert cw.calls and set(cw.calls) <= READ_ONLY_CALLS
    # The authorizer log group is deliberately not searched for errors.
    assert logs.calls == ["/aws/lambda/medislot-dev-auth", "/aws/lambda/medislot-dev-app"]


def test_missing_counts_are_zero_but_missing_gauges_stay_unknown():
    cw = FakeCloudWatch()
    data = collector(cw).summary("1h")
    assert all(v == 0 for v in data["api"]["series"]["requests"])
    assert all(v is None for v in data["rds"]["series"]["cpu"])
    assert data["rds"]["latest"]["cpu"] is None
    assert data["api"]["totals"]["latency_p99"] is None


def test_known_datapoints_are_placed_on_the_grid():
    period = 60
    end = (int(NOW) // period + 1) * period
    ts_last = end - period
    cw = FakeCloudWatch(
        metric_values={
            "api_req": (datetime.fromtimestamp(ts_last, tz=timezone.utc), 17),
            "rds_cpu": (datetime.fromtimestamp(ts_last, tz=timezone.utc), 41.5),
        }
    )
    data = collector(cw).summary("1h")
    assert data["timestamps"][-1] == ts_last
    assert data["api"]["series"]["requests"][-1] == 17
    assert data["api"]["totals"]["requests"] == 17
    assert data["rds"]["latest"]["cpu"] == 41.5


def test_results_are_cached():
    cw = FakeCloudWatch()
    c = collector(cw)
    c.summary("1h")
    first = len(cw.calls)
    c.summary("1h")
    assert len(cw.calls) == first
    c.summary("6h")
    assert len(cw.calls) > first


def test_alarm_failure_does_not_blank_the_dashboard():
    data = collector(FakeCloudWatch(fail_alarms=True)).summary("1h")
    assert data["alarms"] == {"available": False, "items": []}
    assert len(data["timestamps"]) == 60


def test_alarms_are_sorted_with_firing_first():
    def alarm(name, state):
        return {
            "AlarmName": f"medislot-dev-{name}",
            "StateValue": state,
            "StateReason": "reason",
            "StateUpdatedTimestamp": datetime.now(timezone.utc),
        }

    cw = FakeCloudWatch(alarms=[alarm("b", "OK"), alarm("a", "ALARM"), alarm("c", "INSUFFICIENT_DATA")])
    items = collector(cw).summary("1h")["alarms"]["items"]
    assert [a["name"] for a in items] == ["a", "c", "b"]


def test_redact_removes_email_addresses_and_truncates():
    assert "[email]" in redact("duplicate entry jane.doe@example.com for key")
    assert "jane" not in redact("duplicate entry jane.doe@example.com for key")
    assert len(redact("x" * 1000)) <= 300


def test_unknown_range_is_rejected():
    with pytest.raises(ValueError):
        collector(FakeCloudWatch()).summary("7d")
