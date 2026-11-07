-- The schema.sql that was deployed before the final-schema alignment (kept to test V002).
-- MediSlot RDS MySQL schema - aligned with the approved final schema diagram.
-- MySQL 8.0.16+ (CHECK constraints are enforced).
-- DynamoDB owns slots, seats, waitlist, hold-offers, idempotency and dispatch-log.

CREATE TABLE IF NOT EXISTS users (
    user_id CHAR(36) NOT NULL,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('patient', 'doctor') NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT chk_users_email_nonempty CHECK (CHAR_LENGTH(TRIM(email)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id INT NOT NULL AUTO_INCREMENT,
    name VARCHAR(200) NOT NULL,
    address VARCHAR(255) NULL,
    city VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (hospital_id),
    CONSTRAINT uq_hospitals_name_city UNIQUE (name, city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id INT NOT NULL AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (specialisation_id),
    CONSTRAINT uq_specialisations_name UNIQUE (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    hospital_id INT NULL,
    specialisation_id INT NULL,
    licence_number VARCHAR(50) NOT NULL,
    years_experience INT UNSIGNED NULL,
    qualifications VARCHAR(255) NULL,
    languages VARCHAR(100) NULL,
    -- Retained for the existing doctor profile/API fields.
    first_name VARCHAR(80) NULL,
    last_name VARCHAR(80) NULL,
    phone VARCHAR(25) NULL,
    city VARCHAR(100) NULL,
    state VARCHAR(100) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    bio TEXT NULL,
    profile_photo_s3_key VARCHAR(512) NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    CONSTRAINT uq_doctors_user UNIQUE (user_id),
    CONSTRAINT uq_doctors_licence_number UNIQUE (licence_number),
    CONSTRAINT fk_doctors_user FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctors_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT fk_doctors_specialisation FOREIGN KEY (specialisation_id)
        REFERENCES specialisations(specialisation_id)
        ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT chk_doctors_experience CHECK (years_experience IS NULL OR years_experience <= 80)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS patients (
    patient_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    date_of_birth DATE NULL,
    gender VARCHAR(10) NULL,
    phone VARCHAR(20) NULL,
    address VARCHAR(255) NULL,
    city VARCHAR(100) NULL,
    emergency_contact_name VARCHAR(100) NULL,
    emergency_contact_phone VARCHAR(20) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    CONSTRAINT uq_patients_user UNIQUE (user_id),
    CONSTRAINT fk_patients_user FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id INT NOT NULL AUTO_INCREMENT,
    doctor_id CHAR(36) NOT NULL,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(255) NULL,
    fee DECIMAL(10,2) NOT NULL,
    duration_minutes INT UNSIGNED NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_type_id),
    CONSTRAINT uq_consultation_types_doctor_type UNIQUE (doctor_id, consultation_type_id),
    CONSTRAINT fk_consultation_types_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_consultation_fee CHECK (fee >= 0),
    CONSTRAINT chk_consultation_duration CHECK (duration_minutes > 0),
    CONSTRAINT chk_consultation_capacity CHECK (
        (name = 'Health Camp Session' AND capacity BETWEEN 2 AND 10)
        OR (name <> 'Health Camp Session' AND capacity = 1)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS availability_patterns (
    availability_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id INT NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL COMMENT 'ISO weekday: Monday=1 through Sunday=7',
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    -- Kept because the current availability API uses effective date ranges.
    effective_from DATE NOT NULL,
    effective_until DATE NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (availability_id),
    CONSTRAINT fk_availability_doctor_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types(doctor_id, consultation_type_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_availability_day CHECK (day_of_week BETWEEN 1 AND 7),
    CONSTRAINT chk_availability_time CHECK (end_time > start_time),
    CONSTRAINT chk_availability_dates CHECK (effective_until IS NULL OR effective_until >= effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id INT NOT NULL AUTO_INCREMENT,
    doctor_id CHAR(36) NOT NULL,
    `date` DATE NOT NULL,
    reason VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (blackout_id),
    CONSTRAINT uq_blackout_doctor_date UNIQUE (doctor_id, `date`),
    CONSTRAINT fk_blackout_dates_doctor FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    slot_id VARCHAR(100) NOT NULL,
    consultation_type_id INT NOT NULL,
    availability_id CHAR(36) NOT NULL,
    slot_start_utc DATETIME NOT NULL,
    slot_end_utc DATETIME NOT NULL,
    consultation_fee DECIMAL(10,2) NOT NULL,
    status ENUM('CONFIRMED', 'COMPLETED', 'CANCELLED_BY_PATIENT', 'CANCELLED_BY_DOCTOR', 'NO_SHOW')
        NOT NULL DEFAULT 'CONFIRMED',
    reschedule_count INT UNSIGNED NOT NULL DEFAULT 0,
    reschedule_state VARCHAR(30) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    active_flag TINYINT(1) NOT NULL DEFAULT 1,
    idempotency_key VARCHAR(100) NULL,
    active_slot_id VARCHAR(100)
        GENERATED ALWAYS AS (CASE WHEN status = 'CONFIRMED' THEN slot_id ELSE NULL END) STORED,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (appointment_id),
    CONSTRAINT uq_patient_active_slot UNIQUE (patient_id, active_slot_id),
    CONSTRAINT uq_patient_idempotency UNIQUE (patient_id, idempotency_key),
    CONSTRAINT fk_appointments_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_appointments_doctor_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types(doctor_id, consultation_type_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_appointments_availability FOREIGN KEY (availability_id)
        REFERENCES availability_patterns(availability_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_appointment_time CHECK (slot_end_utc > slot_start_utc),
    CONSTRAINT chk_appointment_fee CHECK (consultation_fee >= 0),
    CONSTRAINT chk_appointment_reschedule_count CHECK (reschedule_count <= 2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id CHAR(36) NOT NULL,
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    tier ENUM('PARTIAL', 'FULL', 'NONE') NOT NULL,
    base_fee_amount DECIMAL(10,2) NOT NULL,
    refund_percent DECIMAL(5,2) NOT NULL,
    cancellation_fee_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
    refund_amount DECIMAL(10,2) NOT NULL,
    reason ENUM('PATIENT_CANCEL', 'DOCTOR_CANCEL', 'NO_SHOW') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    CONSTRAINT uq_refund_ledger_appointment UNIQUE (appointment_id),
    CONSTRAINT fk_refund_ledger_appointment FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_refund_ledger_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_refund_amounts CHECK (
        base_fee_amount >= 0 AND cancellation_fee_amount >= 0 AND refund_amount >= 0
        AND refund_percent BETWEEN 0 AND 100
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Lookup/performance indexes (unique constraints above already create their own indexes).
CREATE INDEX idx_doctors_name ON doctors (last_name, first_name);
CREATE INDEX idx_doctors_city ON doctors (city, state);
CREATE INDEX idx_doctors_hospital ON doctors (hospital_id, doctor_id);
CREATE INDEX idx_doctors_active_city ON doctors (is_active, city, doctor_id);
CREATE INDEX idx_consultation_types_doctor_active ON consultation_types (doctor_id, is_active, name);
CREATE INDEX idx_availability_doctor_day_active ON availability_patterns (doctor_id, day_of_week, is_active, effective_from);
CREATE INDEX idx_availability_consultation_type ON availability_patterns (consultation_type_id);

CREATE INDEX idx_appointments_doctor_start ON appointments (doctor_id, slot_start_utc, status);
CREATE INDEX idx_appointments_patient_start ON appointments (patient_id, slot_start_utc, status);
CREATE INDEX idx_appointments_slot ON appointments (slot_id);
CREATE INDEX idx_appointments_consultation_type ON appointments (consultation_type_id);
CREATE INDEX idx_refund_ledger_patient_created ON refund_ledger (patient_id, created_at);
