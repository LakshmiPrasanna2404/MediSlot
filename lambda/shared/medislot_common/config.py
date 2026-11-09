"""AWS clients, settings and SSM Parameter Store access.

SSM holds only the database credentials and the JWT values (secret, issuer,
token lifetimes). They are read once per Lambda container (cold start) and
cached for the life of that container.

Every other rule (slot window, lookup range, photo limits, page sizes, default
doctor time zone, password policy) is a CloudFormation stack parameter, passed
to each function as a CONFIG_* environment variable. Nothing is a constant in
the code: a value changes by changing the stack parameter.
"""

import os

import boto3
from botocore.config import Config

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"total_max_attempts": 3, "mode": "standard"},
)

_clients = {}
_resources = {}
_parameters = {}


def environment():
    return os.environ.get("ENVIRONMENT", "dev")


def region():
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=region(), config=BOTO_CONFIG)
    return _clients[service]


def resource(service):
    if service not in _resources:
        _resources[service] = boto3.resource(service, region_name=region(), config=BOTO_CONFIG)
    return _resources[service]


def table(env_var):
    """DynamoDB Table object for the table name held in an environment variable."""
    return resource("dynamodb").Table(os.environ[env_var])


def get_parameter(name, decrypt=False):
    """Read one SSM parameter, cached per container."""
    if name not in _parameters:
        result = client("ssm").get_parameter(Name=name, WithDecryption=decrypt)
        _parameters[name] = result["Parameter"]["Value"]
    return _parameters[name]


def setting_name(key):
    """Environment variable that holds a setting.

    "slots/booking-window-days" -> "CONFIG_SLOTS_BOOKING_WINDOW_DAYS".
    """
    return "CONFIG_" + key.upper().replace("/", "_").replace("-", "_")


def config_value(key):
    """A business rule, from the environment variable its CloudFormation stack sets."""
    name = setting_name(key)
    value = os.environ.get(name, "")
    if value == "":
        raise RuntimeError(f"Missing setting {name} ({key})")
    return value


def config_int(key):
    return int(config_value(key))


def reset_caches():
    """Used by tests between cases."""
    _clients.clear()
    _resources.clear()
    _parameters.clear()
