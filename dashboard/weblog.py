"""Structured logs for the web app: one JSON line per page request and event.

Every line carries the correlation id that is also sent to the API, so one
search in CloudWatch Logs finds the web line, the API Gateway access log line
and the Lambda lines of the same click. On EC2 the lines go to
/var/log/medislot/web.log, which the CloudWatch agent ships to the log group
/medislot/<env>/web.

Never logged: passwords, tokens, the session id, form contents, emails.
"""

import json
import logging
import os
import sys
import time

from flask import g, has_request_context, request, session

LOGGER_NAME = "medislot.web"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if has_request_context():
            entry["correlationId"] = g.get("correlation_id")
            entry["method"] = request.method
            entry["path"] = request.path
            if session.get("user_id"):
                entry["userId"] = session.get("user_id")
                entry["role"] = session.get("role")
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is now (systemd sends it to web.log)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


def _handler():
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter())
    return handler


def configure(app):
    """JSON logs for our own logger and Flask's; one request line replaces werkzeug's."""
    level = os.environ.get("LOG_LEVEL", "INFO")
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers = [_handler()]
    logger.setLevel(level)
    logger.propagate = False
    app.logger.handlers = [_handler()]  # unhandled exceptions, with stack trace
    app.logger.setLevel(level)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.before_request
    def start_timer():
        g.started = time.perf_counter()

    @app.after_request
    def request_line(response):
        if request.path.startswith("/static/"):
            return response
        started = g.get("started")
        status = response.status_code
        log(
            logging.WARNING if status >= 400 else logging.INFO,
            "page request",
            endpoint=request.endpoint,
            status=status,
            durationMs=round((time.perf_counter() - started) * 1000, 1) if started else None,
            remoteIp=request.headers.get("X-Forwarded-For", request.remote_addr),
            userAgent=(request.user_agent.string or "")[:120],
        )
        return response


def log(level, message, **fields):
    logging.getLogger(LOGGER_NAME).log(level, message, extra={"fields": fields})


def info(message, **fields):
    log(logging.INFO, message, **fields)


def warning(message, **fields):
    log(logging.WARNING, message, **fields)


def error(message, **fields):
    log(logging.ERROR, message, **fields)
