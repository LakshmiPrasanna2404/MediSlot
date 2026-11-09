"""Configuration, all from environment variables.

On EC2 the dashboard stack writes them: the API address comes from the API
stack's output (passed in by the pipeline), and the session signing key is a
random value generated once on the instance, in a file only the app can read.
"""

import os

from cachelib import FileSystemCache


def api_base_url():
    """The REST API address."""
    return os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")


def _session_secret():
    if os.environ.get("MEDISLOT_SESSION_SECRET"):
        return os.environ["MEDISLOT_SESSION_SECRET"]
    with open(os.environ["MEDISLOT_SESSION_SECRET_FILE"], encoding="utf-8") as handle:
        secret = handle.read().strip()
    if len(secret) < 32:
        raise RuntimeError("Session secret must be at least 32 characters")
    return secret


def load(test_config=None):
    values = dict(test_config or {})
    if "SECRET_KEY" not in values:
        values["SECRET_KEY"] = _session_secret()
    session_dir = values.pop("SESSION_DIR", os.environ.get("MEDISLOT_SESSION_DIR", "/tmp/medislot-sessions"))  # noqa: S108
    values.setdefault("SESSION_TYPE", "cachelib")
    values.setdefault("SESSION_CACHELIB", FileSystemCache(cache_dir=session_dir, threshold=2000, mode=0o600))
    values.setdefault("SESSION_PERMANENT", False)
    values.setdefault("SESSION_KEY_PREFIX", "medislot:")
    values.setdefault("SESSION_COOKIE_NAME", "medislot_session")
    values.setdefault("SESSION_COOKIE_HTTPONLY", True)
    values.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    values.setdefault("MAX_CONTENT_LENGTH", 64 * 1024)
    return values
