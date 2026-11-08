"""Configuration. On EC2 the values come from SSM; locally and in tests, from env vars."""

import os

from cachelib import FileSystemCache

_ssm_cache = {}


def _ssm(name, decrypt):
    if name not in _ssm_cache:
        import boto3

        client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        _ssm_cache[name] = client.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    return _ssm_cache[name]


def api_base_url():
    """The REST API address. Read lazily: the API stack may deploy after this instance boots."""
    if os.environ.get("MEDISLOT_API_BASE_URL"):
        return os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")
    return _ssm(os.environ["MEDISLOT_API_BASE_URL_PARAMETER"], decrypt=False).rstrip("/")


def _session_secret():
    if os.environ.get("MEDISLOT_SESSION_SECRET"):
        return os.environ["MEDISLOT_SESSION_SECRET"]
    return _ssm(os.environ["MEDISLOT_SESSION_SECRET_PARAMETER"], decrypt=True)


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
