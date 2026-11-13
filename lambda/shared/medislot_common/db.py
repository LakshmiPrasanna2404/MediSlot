"""MySQL (RDS) access.

One connection per Lambda container, opened lazily and re-used across
invocations. Credentials come from SSM (/medislot/{env}/db/username and the
SecureString /medislot/{env}/db/password). The session time zone is UTC, so
DATETIME and TIMESTAMP values are always UTC.
"""

import os
from contextlib import contextmanager

import pymysql
import pymysql.cursors

from . import config

_connection = None

# Bundled at build time from https://truststore.pki.rds.amazonaws.com
CA_BUNDLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rds-ca-bundle.pem")


def _credentials():
    # Tests (and local runs) may supply credentials directly.
    if os.environ.get("DB_USERNAME") and os.environ.get("DB_PASSWORD"):
        return os.environ["DB_USERNAME"], os.environ["DB_PASSWORD"]
    username = config.get_parameter(os.environ["DB_USERNAME_PARAMETER"], decrypt=True)
    password = config.get_parameter(os.environ["DB_PASSWORD_PARAMETER"], decrypt=True)
    if not username or not password:
        raise RuntimeError("Database credentials are empty")
    return username, password


def _ssl_options():
    mode = os.environ.get("DB_SSL", "verify")
    if mode == "disabled":
        return None
    if os.path.exists(CA_BUNDLE):
        # Encrypted and verified against the RDS certificate authority.
        return {"ca": CA_BUNDLE}
    # Encrypted, certificate not verified (no bundle packaged).
    return {"check_hostname": False}


def connect():
    username, password = _credentials()
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=username,
        password=password,
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        ssl=_ssl_options(),
        init_command="SET time_zone = '+00:00'",
    )


def connection():
    global _connection
    if _connection is None:
        _connection = connect()
    else:
        try:
            _connection.ping(reconnect=False)
        except (pymysql.MySQLError, OSError):
            reset()
            _connection = connect()
    return _connection


def reset():
    global _connection
    if _connection is not None:
        try:
            _connection.close()
        except pymysql.MySQLError:
            pass  # noqa: S110 - closing a dead connection is best effort
    _connection = None


@contextmanager
def transaction():
    """Yield a cursor inside BEGIN ... COMMIT, rolling back on any error."""
    conn = connection()
    conn.begin()
    try:
        with conn.cursor() as cursor:
            yield cursor
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def fetch_one(sql, params=()):
    with connection().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def fetch_all(sql, params=()):
    with connection().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def execute(sql, params=()):
    with connection().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
