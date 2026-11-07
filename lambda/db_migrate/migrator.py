"""Versioned schema migrations for the MediSlot MySQL database.

Files are named V<number>__<description>.sql and run in order. Each applied
version is recorded in schema_migrations, so a version runs once.

MySQL commits DDL immediately (no transactional DDL), so migrations are
written to be safe to re-run: any statement that depends on the current state
is preceded by a guard line, for example

    -- @if column_exists doctors first_name
    ALTER TABLE doctors DROP COLUMN first_name;

Supported guards:
    table_exists / table_missing <table>
    column_exists / column_missing <table> <column>
    column_type_is_not <table> <column> <type>      (compares COLUMN_TYPE, lower case)
    column_is_not_nullable <table> <column>
    index_exists / index_missing <table> <index>
    constraint_exists / constraint_missing <table> <constraint>
"""

import hashlib
import os
import re

FILE_PATTERN = re.compile(r"^V(\d+)__([A-Za-z0-9_]+)\.sql$")
GUARD_PATTERN = re.compile(r"^--\s*@if\s+(\w+)\s+(.+?)\s*$")
LOCK_NAME = "medislot_schema_migrations"


def discover(directory):
    migrations = []
    for name in os.listdir(directory):
        match = FILE_PATTERN.match(name)
        if match:
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            migrations.append(
                {
                    "version": int(match.group(1)),
                    "description": match.group(2).replace("_", " "),
                    "file": name,
                    "sql": body,
                    "checksum": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }
            )
    return sorted(migrations, key=lambda m: m["version"])


def parse_statements(sql):
    """Split a migration into (guard, statement) pairs.

    Statements end with a semicolon at the end of a line. A guard applies to
    the statement that follows it. Plain comment lines are dropped.
    """
    statements = []
    guard = None
    buffer = []
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        guard_match = GUARD_PATTERN.match(stripped)
        if guard_match and not buffer:
            guard = (guard_match.group(1), guard_match.group(2).split())
            continue
        if stripped.startswith("--") and not buffer:
            continue
        if not stripped and not buffer:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            statements.append((guard, statement))
            guard = None
            buffer = []
    if buffer:
        raise ValueError("Migration ends without a terminating semicolon")
    return statements


def _guard_holds(cursor, schema, guard):
    kind, args = guard
    if kind in ("table_exists", "table_missing"):
        cursor.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (schema, args[0]),
        )
        exists = cursor.fetchone()["n"] > 0
        return exists if kind == "table_exists" else not exists
    if kind in ("column_exists", "column_missing", "column_type_is_not", "column_is_not_nullable"):
        cursor.execute(
            """SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s""",
            (schema, args[0], args[1]),
        )
        row = cursor.fetchone()
        if kind == "column_exists":
            return row is not None
        if kind == "column_missing":
            return row is None
        if row is None:
            return False
        if kind == "column_type_is_not":
            return row["COLUMN_TYPE"].lower().replace(" ", "") != args[2].lower()
        return row["IS_NULLABLE"] == "NO"
    if kind in ("index_exists", "index_missing"):
        cursor.execute(
            """SELECT COUNT(*) AS n FROM information_schema.STATISTICS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s""",
            (schema, args[0], args[1]),
        )
        exists = cursor.fetchone()["n"] > 0
        return exists if kind == "index_exists" else not exists
    if kind in ("constraint_exists", "constraint_missing"):
        cursor.execute(
            """SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s""",
            (schema, args[0], args[1]),
        )
        exists = cursor.fetchone()["n"] > 0
        return exists if kind == "constraint_exists" else not exists
    raise ValueError(f"Unknown migration guard: {kind}")


def ensure_history_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version      INT          NOT NULL,
            description  VARCHAR(200) NOT NULL,
            checksum     CHAR(64)     NOT NULL,
            applied_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """
    )


def applied_versions(cursor):
    cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    return {row["version"]: row["checksum"] for row in cursor.fetchall()}


def migrate(conn, directory, log=print):
    """Apply every pending migration. Returns a summary dict."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS db")
        schema = cursor.fetchone()["db"]
        cursor.execute("SELECT GET_LOCK(%s, 60) AS got", (LOCK_NAME,))
        if cursor.fetchone()["got"] != 1:
            raise RuntimeError("Another migration run holds the lock")
        try:
            ensure_history_table(cursor)
            done = applied_versions(cursor)
            applied, skipped_statements = [], 0
            for migration in discover(directory):
                version = migration["version"]
                if version in done:
                    if done[version] != migration["checksum"]:
                        log(f"warning: V{version:03d} changed after it was applied (checksum differs)")
                    continue
                for guard, statement in parse_statements(migration["sql"]):
                    if guard is not None and not _guard_holds(cursor, schema, guard):
                        skipped_statements += 1
                        continue
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, description, checksum) VALUES (%s, %s, %s)",
                    (version, migration["description"], migration["checksum"]),
                )
                conn.commit()
                applied.append(migration["file"])
                log(f"applied {migration['file']}")
            return {
                "database": schema,
                "applied": applied,
                "skippedGuardedStatements": skipped_statements,
                "currentVersion": max([*done.keys(), *[int(FILE_PATTERN.match(f).group(1)) for f in applied]], default=0),
            }
        finally:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
