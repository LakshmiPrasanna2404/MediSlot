"""Schema migrations: idempotent, and they upgrade the earlier schema to the final one."""

import re
from pathlib import Path

import pymysql
import pytest
from conftest import DB_SETTINGS, ROOT

import migrator

LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "legacy_schema.sql"
TABLES = [
    "users", "hospitals", "specialisations", "doctors", "patients", "consultation_types",
    "availability_patterns", "blackout_dates", "appointments", "refund_ledger",
]


def _fresh_database(name):
    server = pymysql.connect(**DB_SETTINGS, autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    with server.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    server.close()
    return pymysql.connect(**DB_SETTINGS, database=name, autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def _table_definitions(conn):
    result = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SHOW CREATE TABLE {table}")
            text = cur.fetchone()["Create Table"]
            result[table] = re.sub(r" AUTO_INCREMENT=\d+", "", text)
    return result


def _run_sql_file(conn, path):
    with conn.cursor() as cur:
        for _guard, statement in migrator.parse_statements(path.read_text()):
            cur.execute(statement)


def test_parse_statements_reads_guards():
    sql = "-- note\n-- @if column_exists t c\nALTER TABLE t DROP COLUMN c;\nSELECT 1;\n"
    assert migrator.parse_statements(sql) == [
        (("column_exists", ["t", "c"]), "ALTER TABLE t DROP COLUMN c"),
        (None, "SELECT 1"),
    ]


def test_fresh_database_and_rerun_is_a_no_op():
    conn = _fresh_database("medislot_mig_fresh")
    first = migrator.migrate(conn, str(ROOT / "database" / "migrations"), log=lambda _m: None)
    assert first["applied"] == [
        "V001__create_schema.sql",
        "V002__align_with_final_schema.sql",
        "V003__indexes.sql",
        "V004__seed_specialisations.sql",
    ]
    second = migrator.migrate(conn, str(ROOT / "database" / "migrations"), log=lambda _m: None)
    assert second["applied"] == []
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM specialisations")
        assert cur.fetchone()["n"] == 17


@pytest.mark.skipif(not LEGACY_SCHEMA.exists(), reason="legacy schema fixture missing")
def test_legacy_schema_upgrades_to_the_same_tables_as_a_fresh_build():
    fresh = _fresh_database("medislot_mig_fresh2")
    migrator.migrate(fresh, str(ROOT / "database" / "migrations"), log=lambda _m: None)

    legacy = _fresh_database("medislot_mig_legacy")
    _run_sql_file(legacy, LEGACY_SCHEMA)
    with legacy.cursor() as cur:
        cur.execute("INSERT INTO users VALUES ('u1','Dr A','a@x.com','h','doctor',1,NOW(),NOW())")
        cur.execute("INSERT INTO doctors (doctor_id,user_id,licence_number,first_name,city) VALUES ('d1','u1','L1','A','Pune')")
    migrator.migrate(legacy, str(ROOT / "database" / "migrations"), log=lambda _m: None)

    assert _table_definitions(legacy) == _table_definitions(fresh)
    with legacy.cursor() as cur:
        cur.execute("SELECT doctor_id, licence_number, timezone FROM doctors")
        assert cur.fetchone() == {"doctor_id": "d1", "licence_number": "L1", "timezone": "Asia/Kolkata"}


def test_reference_schema_file_matches_migrations():
    fresh = _fresh_database("medislot_mig_fresh3")
    migrator.migrate(fresh, str(ROOT / "database" / "migrations"), log=lambda _m: None)
    reference = _fresh_database("medislot_mig_reference")
    _run_sql_file(reference, ROOT / "database" / "schema.sql")
    assert _table_definitions(reference) == _table_definitions(fresh)


def test_generated_active_slot_blocks_second_confirmed_appointment(database):
    with database.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        insert = """INSERT INTO appointments (appointment_id, patient_id, doctor_id, slot_id, consultation_type_id,
                    slot_start_utc, slot_end_utc, consultation_fee, status)
                    VALUES (%s,'p','d','s1',1,'2030-01-01 09:00','2030-01-01 09:30',100,%s)"""
        cur.execute(insert, ("a1", "CANCELLED_BY_PATIENT"))
        cur.execute(insert, ("a2", "CONFIRMED"))
        with pytest.raises(pymysql.err.IntegrityError):
            cur.execute(insert, ("a3", "CONFIRMED"))
        cur.execute("DELETE FROM appointments")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
