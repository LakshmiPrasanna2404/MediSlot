-- V005: normalise consultation types.
--
-- consultation_types held one row per doctor and type, so every doctor's row
-- repeated the type's name and duration. It becomes a catalogue of the four
-- fixed types (name, duration, allowed seats), and what each doctor sets for
-- a type moves to the new doctor_consultation_types table: fee, seats,
-- description, active flag. Its key is (doctor_id, consultation_type_id),
-- exactly the pair availability_patterns and appointments already reference.
--
-- Every statement that depends on the current state is guarded, so a run
-- that stopped part-way can be repeated.

-- @if column_exists consultation_types doctor_id
RENAME TABLE consultation_types TO consultation_types_v4;

-- @if table_missing consultation_types
CREATE TABLE consultation_types (
    consultation_type_id  INT          NOT NULL AUTO_INCREMENT,
    name                  VARCHAR(100) NOT NULL,
    duration_minutes      INT          NOT NULL,
    min_capacity          INT          NOT NULL DEFAULT 1,
    max_capacity          INT          NOT NULL DEFAULT 1,
    created_at            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_types_name (name),
    CONSTRAINT chk_consultation_type_duration CHECK (duration_minutes IN (15, 30, 60)),
    CONSTRAINT chk_consultation_type_capacity CHECK (min_capacity >= 1 AND max_capacity >= min_capacity AND max_capacity <= 10)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO consultation_types (consultation_type_id, name, duration_minutes, min_capacity, max_capacity) VALUES
    (1, 'Quick Consult', 15, 1, 1),
    (2, 'Standard Consult', 30, 1, 1),
    (3, 'Detailed Consult', 60, 1, 1),
    (4, 'Health Camp Session', 60, 2, 10);

-- @if table_missing doctor_consultation_types
CREATE TABLE doctor_consultation_types (
    doctor_id             CHAR(36)      NOT NULL,
    consultation_type_id  INT           NOT NULL,
    fee                   DECIMAL(10,2) NOT NULL,
    capacity              INT           NOT NULL DEFAULT 1,
    description           VARCHAR(255)  NULL,
    is_active             TINYINT(1)    NOT NULL DEFAULT 1,
    created_at            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id, consultation_type_id),
    KEY fk_doctor_consultation_types_type (consultation_type_id),
    CONSTRAINT fk_doctor_consultation_types_doctor FOREIGN KEY (doctor_id) REFERENCES doctors (doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_consultation_types_type FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types (consultation_type_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_doctor_consultation_fee CHECK (fee >= 0),
    CONSTRAINT chk_doctor_consultation_capacity CHECK (capacity BETWEEN 1 AND 10)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- One row per doctor and type. If a doctor had the same type twice, the
-- active and newest one wins.
-- @if table_exists consultation_types_v4
INSERT INTO doctor_consultation_types
    (doctor_id, consultation_type_id, fee, capacity, description, is_active, created_at, updated_at)
SELECT x.doctor_id, x.catalogue_id, x.fee, x.capacity, x.description, x.is_active, x.created_at, x.updated_at
FROM (
    SELECT o.doctor_id, c.consultation_type_id AS catalogue_id, o.fee, o.capacity, o.description,
           o.is_active, o.created_at, o.updated_at,
           ROW_NUMBER() OVER (PARTITION BY o.doctor_id, c.consultation_type_id
                              ORDER BY o.is_active DESC, o.consultation_type_id DESC) AS pick
    FROM consultation_types_v4 o
    JOIN consultation_types c ON c.name = o.name
) x
WHERE x.pick = 1
  AND NOT EXISTS (
    SELECT 1 FROM doctor_consultation_types d
    WHERE d.doctor_id = x.doctor_id AND d.consultation_type_id = x.catalogue_id
  );

-- @if constraint_exists availability_patterns fk_availability_doctor_consultation
ALTER TABLE availability_patterns DROP FOREIGN KEY fk_availability_doctor_consultation;

-- @if constraint_exists appointments fk_appointments_doctor_consultation
ALTER TABLE appointments DROP FOREIGN KEY fk_appointments_doctor_consultation;

-- Point weekly patterns and appointments at the catalogue ids.
-- @if table_exists consultation_types_v4
UPDATE availability_patterns ap
JOIN consultation_types_v4 o ON o.consultation_type_id = ap.consultation_type_id AND o.doctor_id = ap.doctor_id
JOIN consultation_types c ON c.name = o.name
SET ap.consultation_type_id = c.consultation_type_id;

-- @if table_exists consultation_types_v4
UPDATE appointments a
JOIN consultation_types_v4 o ON o.consultation_type_id = a.consultation_type_id AND o.doctor_id = a.doctor_id
JOIN consultation_types c ON c.name = o.name
SET a.consultation_type_id = c.consultation_type_id;

-- @if table_exists consultation_types_v4
DROP TABLE consultation_types_v4;

-- @if index_exists availability_patterns fk_availability_doctor_consultation
ALTER TABLE availability_patterns RENAME INDEX fk_availability_doctor_consultation TO fk_availability_doctor_type;

-- @if constraint_missing availability_patterns fk_availability_doctor_type
ALTER TABLE availability_patterns ADD CONSTRAINT fk_availability_doctor_type
    FOREIGN KEY (doctor_id, consultation_type_id)
    REFERENCES doctor_consultation_types (doctor_id, consultation_type_id) ON DELETE CASCADE ON UPDATE CASCADE;

-- @if index_exists appointments fk_appointments_doctor_consultation
ALTER TABLE appointments RENAME INDEX fk_appointments_doctor_consultation TO fk_appointments_doctor_type;

-- @if constraint_missing appointments fk_appointments_doctor_type
ALTER TABLE appointments ADD CONSTRAINT fk_appointments_doctor_type
    FOREIGN KEY (doctor_id, consultation_type_id)
    REFERENCES doctor_consultation_types (doctor_id, consultation_type_id) ON DELETE RESTRICT ON UPDATE CASCADE;
