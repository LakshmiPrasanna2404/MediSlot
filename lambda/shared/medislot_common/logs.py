"""Structured JSON logging with a correlation ID, plus CloudWatch EMF metrics.

Every log line is one JSON object carrying the correlation ID of the request
that produced it. The ID comes from the X-Correlation-Id header when the web
app sends one, otherwise from API Gateway's request ID, so it is generated at
the edge and threaded through every line and event payload.

Fields set with set_context() (the route and the caller's userId and role) are
added to every line of the request, so a business event such as "slots
synced" always says who did it and through which route. Never logged:
passwords, tokens, request bodies, emails.

Custom metrics use the CloudWatch Embedded Metric Format: a JSON log line that
CloudWatch turns into a metric. VPC-attached functions need no extra endpoint.
"""

import json
import logging
import os
import sys
import time

METRIC_NAMESPACE = "medislot"

_state = {"correlation_id": None, "aws_request_id": None, "api_request_id": None, "context": {}}


def set_correlation_id(value, aws_request_id=None, api_request_id=None):
    """Start a new request: api_request_id is API Gateway's request ID, the key of its access log line."""
    _state["correlation_id"] = value
    _state["aws_request_id"] = aws_request_id
    _state["api_request_id"] = api_request_id
    _state["context"] = {}


def set_context(**fields):
    """Fields added to every following line of this request (None values are skipped)."""
    _state["context"].update({key: value for key, value in fields.items() if value is not None})


def correlation_id():
    return _state["correlation_id"]


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local"),
            "correlationId": _state["correlation_id"],
            "awsRequestId": _state["aws_request_id"],
        }
        if _state["api_request_id"]:
            entry["apiRequestId"] = _state["api_request_id"]
        entry.update(_state["context"])
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def get_logger(name="medislot"):
    logger = logging.getLogger(name)
    if not getattr(logger, "_medislot_configured", False):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.handlers = [handler]
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
        logger.propagate = False
        logger._medislot_configured = True
    return logger


def log(level, message, **fields):
    get_logger().log(level, message, extra={"fields": {k: v for k, v in fields.items() if v is not None}})


def info(message, **fields):
    log(logging.INFO, message, **fields)


def warning(message, **fields):
    log(logging.WARNING, message, **fields)


def error(message, **fields):
    log(logging.ERROR, message, **fields)


def exception(message, **fields):
    get_logger().exception(message, extra={"fields": fields})


def metric(name, value=1, unit="Count", **dimensions):
    """Emit one custom metric in the medislot namespace (CloudWatch EMF)."""
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [sorted(dimensions)] if dimensions else [[]],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        "correlationId": _state["correlation_id"],
    }
    payload.update({key: str(val) for key, val in dimensions.items()})
    print(json.dumps(payload), flush=True)
