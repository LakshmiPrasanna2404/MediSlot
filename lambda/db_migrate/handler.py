"""Database migration Lambda (runs inside the VPC, next to RDS).

The pipeline invokes it after each deployment:
    {"action": "migrate"}  apply pending migrations (default)
    {"action": "status"}   list applied versions
    {"action": "check"}    connectivity check: proves a Lambda in the private
                           subnets reaches RDS (Milestone 2 verification)

It has no API Gateway route; only the deployment role can invoke it.
"""

import os

import migrator
from medislot_common import db, logs

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def lambda_handler(event, context):
    logs.set_correlation_id(getattr(context, "aws_request_id", "local"), getattr(context, "aws_request_id", None))
    action = (event or {}).get("action", "migrate")
    conn = db.connection()
    if action == "check":
        row = db.fetch_one("SELECT VERSION() AS version, DATABASE() AS db, UTC_TIMESTAMP() AS now")
        result = {"ok": True, "mysqlVersion": row["version"], "database": row["db"], "utcNow": str(row["now"])}
    elif action == "status":
        with conn.cursor() as cursor:
            migrator.ensure_history_table(cursor)
            result = {"applied": sorted(migrator.applied_versions(cursor))}
    elif action == "migrate":
        result = migrator.migrate(conn, MIGRATIONS_DIR, log=lambda message: logs.info(message))
    else:
        raise ValueError(f"Unknown action {action!r}")
    logs.info("db-migrate finished", action=action, result=result)
    return result
