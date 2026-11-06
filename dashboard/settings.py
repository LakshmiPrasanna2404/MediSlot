"""Runtime settings for the MediSlot operations dashboard.

On EC2 these come from /etc/medislot-dashboard.env, which the instance's
UserData writes from CloudFormation exports. None of them is a secret.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    environment: str = "dev"
    region: str = "us-east-1"
    api_id: str = ""
    slots_table: str = ""
    seats_table: str = ""
    db_instance_id: str = ""
    # Every GetMetricData query is billed, so results are cached this long.
    cache_seconds: int = 120
    # The login page is served over plain HTTP, so the cookie cannot be Secure.
    # Set DASHBOARD_SECURE_COOKIE=1 only if HTTPS is ever put in front of it.
    secure_cookie: bool = False

    @property
    def ssm_prefix(self):
        return f"/medislot/{self.environment}/dashboard"

    @property
    def function_names(self):
        """Lambda function name by short key."""
        return {
            key: f"medislot-{self.environment}-{key}"
            for key in ("auth", "authorizer", "app")
        }

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        environment = env.get("ENVIRONMENT", "dev")
        if environment not in ("dev", "prod"):
            raise RuntimeError("ENVIRONMENT must be dev or prod")
        return cls(
            environment=environment,
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1",
            api_id=env.get("API_ID", ""),
            slots_table=env.get("SLOTS_TABLE", f"medislot-{environment}-slots"),
            seats_table=env.get("SEATS_TABLE", f"medislot-{environment}-seats"),
            db_instance_id=env.get("DB_INSTANCE_ID", f"medislot-{environment}-mysql"),
            cache_seconds=int(env.get("CACHE_SECONDS", "120")),
            secure_cookie=env.get("DASHBOARD_SECURE_COOKIE", "") == "1",
        )
