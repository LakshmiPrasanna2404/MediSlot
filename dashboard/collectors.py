"""Read-only data collectors for the MediSlot operations dashboard.

CloudWatchCollector reads metrics, alarms and recent Lambda error lines.
StaticCollector returns deterministic sample data for tests and local preview.
Both return the same JSON-serialisable shape, built by summary(range_key).

The collector only ever calls the read-only APIs listed in READ_ONLY_CALLS;
a test enforces that.
"""

import math
import re
import threading
import time
from datetime import datetime, timezone

# range key -> (window seconds, period seconds)
RANGES = {"1h": (3600, 60), "6h": (21600, 300), "24h": (86400, 900)}

FUNCTION_KEYS = ("auth", "authorizer", "app")
# The authorizer raises "Unauthorized" for every bad token, so its logs and
# Errors metric are mostly normal 401s. Its logs are not searched for errors.
ERROR_LOG_FUNCTIONS = ("auth", "app")
ERROR_FILTER_PATTERN = "?ERROR ?Traceback ?failed ?Exception"
MAX_CUSTOM_METRICS = 12

READ_ONLY_CALLS = frozenset(
    {"get_metric_data", "list_metrics", "describe_alarms", "filter_log_events"}
)

_EMAIL = re.compile(r"[^\s@'\"<>(),;]+@[^\s@'\"<>(),;]+")


def redact(text, limit=300):
    """Log lines can contain email addresses (for example duplicate-key errors)."""
    text = _EMAIL.sub("[email]", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class CloudWatchCollector:
    def __init__(self, settings, cloudwatch=None, logs=None, clock=time.time):
        self.settings = settings
        self.clock = clock
        if cloudwatch is None or logs is None:
            import boto3
            from botocore.config import Config

            config = Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"total_max_attempts": 3, "mode": "standard"},
            )
            cloudwatch = cloudwatch or boto3.client(
                "cloudwatch", region_name=settings.region, config=config
            )
            logs = logs or boto3.client("logs", region_name=settings.region, config=config)
        self.cw = cloudwatch
        self.logs = logs
        self._cache = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def summary(self, range_key):
        if range_key not in RANGES:
            raise ValueError("unknown range")
        now = self.clock()
        with self._lock:
            hit = self._cache.get(range_key)
            if hit and now - hit[0] < self.settings.cache_seconds:
                return hit[1]
        data = self._collect(range_key, now)
        with self._lock:
            self._cache[range_key] = (now, data)
        return data

    # ------------------------------------------------------------------
    @staticmethod
    def _query(qid, namespace, metric, dims, stat, period):
        return {
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                },
                "Period": period,
                "Stat": stat,
            },
            "ReturnData": True,
        }

    def _fetch(self, queries, start, end):
        """Return {query id: {epoch timestamp: value}}."""
        points = {q["Id"]: {} for q in queries}
        for i in range(0, len(queries), 400):
            token = None
            while True:
                kwargs = {
                    "MetricDataQueries": queries[i : i + 400],
                    "StartTime": datetime.fromtimestamp(start, tz=timezone.utc),
                    "EndTime": datetime.fromtimestamp(end, tz=timezone.utc),
                    "ScanBy": "TimestampAscending",
                }
                if token:
                    kwargs["NextToken"] = token
                resp = self.cw.get_metric_data(**kwargs)
                for result in resp.get("MetricDataResults", []):
                    bucket = points.setdefault(result["Id"], {})
                    for ts, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                        bucket[int(ts.timestamp())] = value
                token = resp.get("NextToken")
                if not token:
                    break
        return points

    def _collect(self, range_key, now):
        window, period = RANGES[range_key]
        s = self.settings
        end = (int(now) // period + 1) * period
        start = end - window
        grid = list(range(start, end, period))

        queries = []
        # kind "sum": no datapoint means zero events. kind "val": no datapoint means unknown.
        kinds = {}

        def add(qid, namespace, metric, dims, stat, kind, per=period):
            queries.append(self._query(qid, namespace, metric, dims, stat, per))
            kinds[qid] = kind

        api = {"ApiId": s.api_id}
        add("api_req", "AWS/ApiGateway", "Count", api, "Sum", "sum")
        add("api_4xx", "AWS/ApiGateway", "4xx", api, "Sum", "sum")
        add("api_5xx", "AWS/ApiGateway", "5xx", api, "Sum", "sum")
        add("api_p50", "AWS/ApiGateway", "Latency", api, "p50", "val")
        add("api_p99", "AWS/ApiGateway", "Latency", api, "p99", "val")
        add("w_api_p50", "AWS/ApiGateway", "Latency", api, "p50", "val", window)
        add("w_api_p99", "AWS/ApiGateway", "Latency", api, "p99", "val", window)

        functions = s.function_names
        for key in FUNCTION_KEYS:
            dims = {"FunctionName": functions[key]}
            add(f"fn_{key}_inv", "AWS/Lambda", "Invocations", dims, "Sum", "sum")
            add(f"fn_{key}_err", "AWS/Lambda", "Errors", dims, "Sum", "sum")
            add(f"fn_{key}_thr", "AWS/Lambda", "Throttles", dims, "Sum", "sum")
            add(f"fn_{key}_p99", "AWS/Lambda", "Duration", dims, "p99", "val")
            add(f"w_fn_{key}_p99", "AWS/Lambda", "Duration", dims, "p99", "val", window)

        tables = {"slots": s.slots_table, "seats": s.seats_table}
        for key, table in tables.items():
            dims = {"TableName": table}
            add(f"ddb_{key}_rthr", "AWS/DynamoDB", "ReadThrottleEvents", dims, "Sum", "sum")
            add(f"ddb_{key}_wthr", "AWS/DynamoDB", "WriteThrottleEvents", dims, "Sum", "sum")
            add(f"ddb_{key}_ccf", "AWS/DynamoDB", "ConditionalCheckFailedRequests", dims, "Sum", "sum")
            add(f"ddb_{key}_rcu", "AWS/DynamoDB", "ConsumedReadCapacityUnits", dims, "Sum", "sum")
            add(f"ddb_{key}_wcu", "AWS/DynamoDB", "ConsumedWriteCapacityUnits", dims, "Sum", "sum")

        db = {"DBInstanceIdentifier": s.db_instance_id}
        add("rds_cpu", "AWS/RDS", "CPUUtilization", db, "Average", "val")
        add("rds_conn", "AWS/RDS", "DatabaseConnections", db, "Average", "val")
        add("rds_free", "AWS/RDS", "FreeStorageSpace", db, "Average", "val")

        raw = self._fetch(queries, start, end)

        def series(qid, scale=1.0):
            fill = 0 if kinds[qid] == "sum" else None
            out = []
            for t in grid:
                v = raw.get(qid, {}).get(t, fill)
                out.append(None if v is None else v * scale)
            return out

        def window_value(qid):
            values = list(raw.get(qid, {}).values())
            return values[-1] if values else None

        def total(values):
            return sum(v for v in values if v is not None)

        def latest(values):
            for v in reversed(values):
                if v is not None:
                    return v
            return None

        api_series = {
            "requests": series("api_req"),
            "errors_4xx": series("api_4xx"),
            "errors_5xx": series("api_5xx"),
            "latency_p50": series("api_p50"),
            "latency_p99": series("api_p99"),
        }
        result = {
            "generated_at": _iso(now),
            "range": range_key,
            "period_seconds": period,
            "environment": s.environment,
            "timestamps": grid,
            "api": {
                "series": api_series,
                "totals": {
                    "requests": total(api_series["requests"]),
                    "errors_4xx": total(api_series["errors_4xx"]),
                    "errors_5xx": total(api_series["errors_5xx"]),
                    "latency_p50": window_value("w_api_p50"),
                    "latency_p99": window_value("w_api_p99"),
                },
            },
            "lambdas": {},
            "dynamodb": {},
        }

        for key in FUNCTION_KEYS:
            fn_series = {
                "invocations": series(f"fn_{key}_inv"),
                "errors": series(f"fn_{key}_err"),
                "throttles": series(f"fn_{key}_thr"),
                "duration_p99": series(f"fn_{key}_p99"),
            }
            result["lambdas"][key] = {
                "name": functions[key],
                "series": fn_series,
                "totals": {
                    "invocations": total(fn_series["invocations"]),
                    "errors": total(fn_series["errors"]),
                    "throttles": total(fn_series["throttles"]),
                    "duration_p99": window_value(f"w_fn_{key}_p99"),
                },
            }

        for key, table in tables.items():
            ddb_series = {
                "throttled": [
                    (r or 0) + (w or 0)
                    for r, w in zip(series(f"ddb_{key}_rthr"), series(f"ddb_{key}_wthr"))
                ],
                "conditional_check_failures": series(f"ddb_{key}_ccf"),
                "read_units": series(f"ddb_{key}_rcu"),
                "write_units": series(f"ddb_{key}_wcu"),
            }
            result["dynamodb"][key] = {
                "name": table,
                "series": ddb_series,
                "totals": {k: total(v) for k, v in ddb_series.items()},
            }

        rds_series = {
            "cpu": series("rds_cpu"),
            "connections": series("rds_conn"),
            "free_storage_gb": series("rds_free", 1.0 / (1024 ** 3)),
        }
        result["rds"] = {
            "name": s.db_instance_id,
            "series": rds_series,
            "latest": {k: latest(v) for k, v in rds_series.items()},
        }

        result["alarms"] = self._guard(self._alarms)
        result["errors"] = self._guard(lambda: self._recent_errors(window, now))
        result["custom"] = self._guard(lambda: self._custom_metrics(start, end, window))
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _guard(fn):
        """A failure in one optional panel must not blank the whole dashboard."""
        try:
            return {"available": True, "items": fn()}
        except Exception:  # noqa: BLE001 - reported as unavailable, details stay in the server log
            import logging

            logging.getLogger(__name__).exception("Dashboard panel failed")
            return {"available": False, "items": []}

    def _alarms(self):
        prefix = f"medislot-{self.settings.environment}-"
        items = []
        token = None
        while True:
            kwargs = {"AlarmNamePrefix": prefix, "AlarmTypes": ["MetricAlarm"], "MaxRecords": 100}
            if token:
                kwargs["NextToken"] = token
            resp = self.cw.describe_alarms(**kwargs)
            for alarm in resp.get("MetricAlarms", []):
                updated = alarm.get("StateUpdatedTimestamp")
                items.append(
                    {
                        "name": alarm["AlarmName"][len(prefix):],
                        "state": alarm.get("StateValue", "INSUFFICIENT_DATA"),
                        "reason": redact(alarm.get("StateReason", "")),
                        "updated": updated.isoformat() if updated else None,
                    }
                )
            token = resp.get("NextToken")
            if not token:
                break
        order = {"ALARM": 0, "INSUFFICIENT_DATA": 1, "OK": 2}
        return sorted(items, key=lambda a: (order.get(a["state"], 3), a["name"]))

    def _recent_errors(self, window, now):
        from botocore.exceptions import ClientError

        events = []
        for key in ERROR_LOG_FUNCTIONS:
            name = self.settings.function_names[key]
            try:
                resp = self.logs.filter_log_events(
                    logGroupName=f"/aws/lambda/{name}",
                    startTime=int((now - window) * 1000),
                    filterPattern=ERROR_FILTER_PATTERN,
                    limit=20,
                )
            except ClientError as exc:
                # The log group only exists after the function has run once.
                if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    continue
                raise
            for event in resp.get("events", []):
                events.append(
                    {
                        "time": _iso(event["timestamp"] / 1000.0),
                        "function": key,
                        "message": redact(event.get("message", "")),
                    }
                )
        events.sort(key=lambda e: e["time"], reverse=True)
        return events[:20]

    def _custom_metrics(self, start, end, window):
        """Booking, saga and waitlist metrics appear here once the code emits them."""
        seen = {}
        token = None
        while True:
            kwargs = {"Namespace": "medislot"}
            if token:
                kwargs["NextToken"] = token
            resp = self.cw.list_metrics(**kwargs)
            for metric in resp.get("Metrics", []):
                seen.setdefault(metric["MetricName"], metric.get("Dimensions", []))
            token = resp.get("NextToken")
            if not token or len(seen) >= MAX_CUSTOM_METRICS:
                break
        names = sorted(seen)[:MAX_CUSTOM_METRICS]
        if not names:
            return []
        queries = []
        for i, name in enumerate(names):
            queries.append(
                {
                    "Id": f"c{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "medislot",
                            "MetricName": name,
                            "Dimensions": seen[name],
                        },
                        "Period": window,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
            )
        raw = self._fetch(queries, start, end)
        return [
            {"name": name, "total": sum(raw.get(f"c{i}", {}).values())}
            for i, name in enumerate(names)
        ]


# ----------------------------------------------------------------------
class StaticCollector:
    """Deterministic sample data. Used by tests and the local preview only."""

    def __init__(self, settings, clock=time.time):
        self.settings = settings
        self.clock = clock

    @staticmethod
    def _wave(n, base, amp, phase=0.0):
        return [max(0.0, round(base + amp * math.sin(i / 6.0 + phase), 2)) for i in range(n)]

    def summary(self, range_key):
        if range_key not in RANGES:
            raise ValueError("unknown range")
        window, period = RANGES[range_key]
        now = self.clock()
        end = (int(now) // period + 1) * period
        grid = list(range(end - window, end, period))
        n = len(grid)
        w = self._wave
        requests = [round(x) for x in w(n, 40, 25)]
        e4 = [round(x) for x in w(n, 3, 2, 1)]
        e5 = [1 if i % 29 == 0 else 0 for i in range(n)]
        lat50 = w(n, 90, 25, 2)
        lat99 = w(n, 420, 120, 3)
        names = self.settings.function_names

        def lam(key, scale, base):
            inv = [round(x * scale) for x in requests]
            err = [1 if (i % 41 == 0 and key != "authorizer") else 0 for i in range(n)]
            if key == "authorizer":
                err = [round(x * 0.1) for x in e4]
            return {
                "name": names[key],
                "series": {
                    "invocations": inv,
                    "errors": err,
                    "throttles": [0] * n,
                    "duration_p99": w(n, base, base * 0.3, 1.5),
                },
                "totals": {
                    "invocations": sum(inv),
                    "errors": sum(err),
                    "throttles": 0,
                    "duration_p99": base * 1.4,
                },
            }

        def ddb(name, ccf_scale):
            ccf = [round(x * ccf_scale) for x in w(n, 2, 2)]
            series = {
                "throttled": [0] * n,
                "conditional_check_failures": ccf,
                "read_units": w(n, 30, 10),
                "write_units": w(n, 12, 6),
            }
            return {
                "name": name,
                "series": series,
                "totals": {k: sum(v) for k, v in series.items()},
            }

        cpu = w(n, 22, 8, 0.5)
        series = {
            "requests": requests,
            "errors_4xx": e4,
            "errors_5xx": e5,
            "latency_p50": lat50,
            "latency_p99": lat99,
        }
        return {
            "generated_at": _iso(now),
            "range": range_key,
            "period_seconds": period,
            "environment": self.settings.environment,
            "timestamps": grid,
            "api": {
                "series": series,
                "totals": {
                    "requests": sum(requests),
                    "errors_4xx": sum(e4),
                    "errors_5xx": sum(e5),
                    "latency_p50": 95.0,
                    "latency_p99": 480.0,
                },
            },
            "lambdas": {
                "auth": lam("auth", 0.3, 350),
                "authorizer": lam("authorizer", 1.0, 40),
                "app": lam("app", 0.7, 180),
            },
            "dynamodb": {
                "slots": ddb(self.settings.slots_table, 1),
                "seats": ddb(self.settings.seats_table, 0.5),
            },
            "rds": {
                "name": self.settings.db_instance_id,
                "series": {
                    "cpu": cpu,
                    "connections": w(n, 6, 3, 2),
                    "free_storage_gb": [17.4] * n,
                },
                "latest": {"cpu": cpu[-1], "connections": 6, "free_storage_gb": 17.4},
            },
            "alarms": {
                "available": True,
                "items": [
                    {"name": "api-5xx", "state": "OK", "reason": "Threshold not crossed.", "updated": _iso(now - 7200)},
                    {"name": "rds-cpu", "state": "OK", "reason": "Threshold not crossed.", "updated": _iso(now - 9000)},
                    {"name": "lambda-app-errors", "state": "OK", "reason": "Threshold not crossed.", "updated": _iso(now - 3000)},
                ],
            },
            "errors": {
                "available": True,
                "items": [
                    {"time": _iso(now - 600), "function": "auth", "message": "Login failed: OperationalError: (2003, \"Can't connect to MySQL server\")"},
                ],
            },
            "custom": {"available": True, "items": []},
        }
