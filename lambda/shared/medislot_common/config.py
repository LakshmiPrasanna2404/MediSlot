"""AWS clients and SSM Parameter Store access.

Every threshold, window and percentage lives in SSM under
/medislot/{env}/config/. Values are read once per Lambda container
(cold start) and cached for the life of that container.
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
_config = None


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


def config_prefix():
    return f"/medislot/{environment()}/config/"


def get_config():
    """All business rules under /medislot/{env}/config/, keyed by relative name.

    Example key: "refund/full-refund-min-hours".
    """
    global _config
    if _config is None:
        prefix = config_prefix()
        values = {}
        paginator = client("ssm").get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=False):
            for parameter in page["Parameters"]:
                values[parameter["Name"][len(prefix):]] = parameter["Value"]
        _config = values
    return _config


def config_value(key):
    values = get_config()
    if key not in values:
        raise RuntimeError(f"Missing SSM parameter {config_prefix()}{key}")
    return values[key]


def config_int(key):
    return int(config_value(key))


def reset_caches():
    """Used by tests between cases."""
    global _config
    _clients.clear()
    _resources.clear()
    _parameters.clear()
    _config = None
