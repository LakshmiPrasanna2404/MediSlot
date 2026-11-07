"""Unit-test harness.

* MySQL: a real MySQL server (a service container in CI, a local server
  otherwise). Connection details come from MEDISLOT_TEST_DB_* variables. The
  schema is built with the same migrations the db-migrate Lambda runs.
* AWS: moto. DynamoDB tables are created from infrastructure/data-stack.yaml,
  so the tests use exactly the key design that gets deployed.
* Each Lambda handler is loaded from its own folder under a unique module
  name (every function has a file called handler.py).
"""

import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pymysql
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lambda" / "shared"))
sys.path.insert(0, str(ROOT / "lambda" / "db_migrate"))

ENVIRONMENT = "test"
TEST_DB = os.environ.get("MEDISLOT_TEST_DB_NAME", "medislot_unit")
DB_SETTINGS = {
    "host": os.environ.get("MEDISLOT_TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MEDISLOT_TEST_DB_PORT", "3306")),
    "user": os.environ.get("MEDISLOT_TEST_DB_USER", "medislot"),
    "password": os.environ.get("MEDISLOT_TEST_DB_PASSWORD", "medislot-test-pw"),
}
JWT_SECRET = "unit-test-signing-secret-that-is-long-enough-0123456789"
PHOTO_BUCKET = "medislot-test-doctor-profile-files"

CONFIG = {
    "slots/booking-window-days": "30",
    "slots/max-lookup-days": "31",
    "photos/max-upload-bytes": "5242880",
    "photos/upload-url-seconds": "300",
    "photos/view-url-seconds": "900",
    "search/default-page-size": "20",
    "search/max-page-size": "50",
    "doctors/default-timezone": "Asia/Kolkata",
}
AUTH = {
    "access-token-seconds": "900",
    "refresh-token-seconds": "604800",
    "bcrypt-rounds": "12",
    "password-min-length": "10",
    "jwt-issuer": "medislot-test",
}

os.environ.update(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "ENVIRONMENT": ENVIRONMENT,
        "DB_HOST": DB_SETTINGS["host"],
        "DB_PORT": str(DB_SETTINGS["port"]),
        "DB_NAME": TEST_DB,
        "DB_USERNAME": DB_SETTINGS["user"],
        "DB_PASSWORD": DB_SETTINGS["password"],
        "DB_SSL": "disabled",
        "JWT_SECRET_PARAMETER": f"/medislot/{ENVIRONMENT}/auth/jwt-secret",
        "DOCTOR_FILES_BUCKET": PHOTO_BUCKET,
        "REFRESH_TOKENS_TABLE": f"medislot-{ENVIRONMENT}-refresh-tokens",
        "TOKEN_REVOCATIONS_TABLE": f"medislot-{ENVIRONMENT}-token-revocations",
        "SLOTS_TABLE": f"medislot-{ENVIRONMENT}-slots",
        "LOG_LEVEL": "WARNING",
    }
)

import migrator  # noqa: E402
from medislot_common import config, db  # noqa: E402

TABLES_IN_ORDER = [
    "refund_ledger", "appointments", "blackout_dates", "availability_patterns", "consultation_types",
    "doctors", "patients", "users", "hospitals",
]


def _server_connection(database=None):
    return pymysql.connect(
        **DB_SETTINGS, database=database, autocommit=True, cursorclass=pymysql.cursors.DictCursor
    )


@pytest.fixture(scope="session")
def database():
    server = _server_connection()
    with server.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    server.close()
    conn = _server_connection(TEST_DB)
    migrator.migrate(conn, str(ROOT / "database" / "migrations"), log=lambda _m: None)
    yield conn
    conn.close()


@pytest.fixture()
def clean_db(database):
    with database.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in TABLES_IN_ORDER:
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    db.reset()
    yield database
    db.reset()


def _data_stack_tables():
    from cfn_flip import to_json

    template = json.loads(to_json((ROOT / "infrastructure" / "data-stack.yaml").read_text()))
    definitions = []
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::DynamoDB::Table":
            continue
        props = resource["Properties"]
        name = props["TableName"]["Fn::Sub"].replace("${Environment}", ENVIRONMENT)
        definition = {
            "TableName": name,
            "AttributeDefinitions": props["AttributeDefinitions"],
            "KeySchema": props["KeySchema"],
            "BillingMode": "PAY_PER_REQUEST",
        }
        for key in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"):
            if key in props:
                definition[key] = props[key]
        definitions.append(definition)
    return definitions


@pytest.fixture()
def aws():
    from moto import mock_aws

    with mock_aws():
        config.reset_caches()
        import boto3

        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        for definition in _data_stack_tables():
            dynamodb.create_table(**definition)
        ssm = boto3.client("ssm", region_name="us-east-1")
        for key, value in CONFIG.items():
            ssm.put_parameter(Name=f"/medislot/{ENVIRONMENT}/config/{key}", Value=value, Type="String")
        for key, value in AUTH.items():
            ssm.put_parameter(Name=f"/medislot/{ENVIRONMENT}/auth/{key}", Value=value, Type="String")
        ssm.put_parameter(Name=f"/medislot/{ENVIRONMENT}/auth/jwt-secret", Value=JWT_SECRET, Type="SecureString")
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=PHOTO_BUCKET)
        yield boto3
        config.reset_caches()


_loaded = {}


def load_handler(folder, filename="handler.py"):
    key = (folder, filename)
    if key not in _loaded:
        path = ROOT / "lambda" / folder / filename
        spec = importlib.util.spec_from_file_location(f"medislot_fn_{folder}_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded[key] = module
    return _loaded[key]


class FakeContext:
    def __init__(self):
        self.aws_request_id = str(uuid.uuid4())
        self.function_name = "unit-test"


def api_event(method, resource, *, body=None, user=None, path_params=None, query=None, jti=None):
    """A REST API (Lambda proxy) event as API Gateway would send it."""
    path = resource
    for name, value in (path_params or {}).items():
        path = path.replace("{" + name + "}", str(value))
    authorizer = None
    if user is not None:
        authorizer = {
            "principalId": user["user_id"],
            "userId": user["user_id"],
            "role": user["role"],
            "jti": jti or str(uuid.uuid4()),
            "exp": str(int(time.time()) + 900),
        }
    return {
        "resource": resource,
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "queryStringParameters": query,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
        "requestContext": {"requestId": str(uuid.uuid4()), "authorizer": authorizer},
    }


def call(module, event):
    result = module.lambda_handler(event, FakeContext())
    body = json.loads(result["body"]) if result.get("body") else None
    return result["statusCode"], body


@pytest.fixture()
def auth_fn(aws, clean_db):
    return load_handler("auth", "auth_handler.py")


@pytest.fixture()
def register(auth_fn):
    """Register an account through the real Auth Lambda; returns its user dict."""

    def _register(role, **fields):
        unique = uuid.uuid4().hex[:8]
        body = {
            "full_name": fields.pop("full_name", f"Test {role.title()} {unique}"),
            "email": fields.pop("email", f"{role}-{unique}@example.com"),
            "password": fields.pop("password", "CorrectHorse42"),
        }
        if role == "doctor":
            body["licence_number"] = fields.pop("licence_number", f"LIC-{unique}")
        body.update(fields)
        status, payload = call(auth_fn, api_event("POST", f"/auth/{role}s/register", body=body))
        assert status == 201, payload
        user = payload["user"]
        user["profile_id"] = payload[f"{role}_id"]
        user["password"] = body["password"]
        return user

    return _register
