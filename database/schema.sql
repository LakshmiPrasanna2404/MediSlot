-- MediSlot RDS MySQL schema
-- Relational system of record.
-- Slots, seats, waitlists, hold offers, idempotency and dispatch records
-- remain in DynamoDB as defined in the final DynamoDB schema.

CREATE TABLE IF NOT EXISTS users (
    user_id CHAR(36) NOT NULL,
    full_name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('doctor', 'patient') NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT chk_users_email_nonempty
        CHECK (CHAR_LENGTH(TRIM(email)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    hospital_name VARCHAR(180) NOT NULL,
    address_line1 VARCHAR(180) NULL,
    address_line2 VARCHAR(180) NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NULL,
    postal_code VARCHAR(20) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (hospital_id),
    CONSTRAINT uq_hospitals_name_city UNIQUE (hospital_name, city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS specialisations (
    specialisation_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    specialisation_name VARCHAR(120) NOT NULL,
    description VARCHAR(500) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (specialisation_id),
    CONSTRAINT uq_specialisations_name UNIQUE (specialisation_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS doctors (
    doctor_id CHAR(36) NOT NULL,
    hospital_id BIGINT UNSIGNED NULL,
    first_name VARCHAR(80) NOT NULL,
    last_name VARCHAR(80) NOT NULL,
    phone VARCHAR(25) NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    qualification VARCHAR(180) NULL,
    experience_years SMALLINT UNSIGNED NULL,
    bio TEXT NULL,
    profile_photo_s3_key VARCHAR(512) NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    CONSTRAINT fk_doctors_user
        FOREIGN KEY (doctor_id) REFERENCES users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctors_hospital
        FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)
        ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT chk_doctors_experience
        CHECK (experience_years IS NULL OR experience_years <= 80)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS patients (
    patient_id CHAR(36) NOT NULL,
    date_of_birth DATE NULL,
    gender ENUM('female', 'male', 'other', 'prefer_not_to_say') NULL,
    phone VARCHAR(25) NULL,
    city VARCHAR(100) NULL,
    state VARCHAR(100) NULL,
    country VARCHAR(100) NOT NULL DEFAULT 'India',
    emergency_contact_name VARCHAR(120) NULL,
    emergency_contact_phone VARCHAR(25) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    CONSTRAINT fk_patients_user
        FOREIGN KEY (patient_id) REFERENCES users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS doctor_specialisations (
    doctor_id CHAR(36) NOT NULL,
    specialisation_id BIGINT UNSIGNED NOT NULL,
    is_primary TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id, specialisation_id),
    CONSTRAINT fk_doctor_specialisations_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_doctor_specialisations_specialisation
        FOREIGN KEY (specialisation_id) REFERENCES specialisations(specialisation_id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_name ENUM(
        'Quick Consult',
        'Standard Consult',
        'Detailed Consult',
        'Health Camp Session'
    ) NOT NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    fee DECIMAL(10,2) NOT NULL,
    capacity TINYINT UNSIGNED NOT NULL DEFAULT 1,
    description VARCHAR(500) NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_type_id),

    -- Required as the referenced key for doctor/consultation ownership checks.
    CONSTRAINT uq_consultation_types_doctor_type
        UNIQUE (doctor_id, consultation_type_id),

    CONSTRAINT fk_consultation_types_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT chk_consultation_duration
        CHECK (duration_minutes > 0),
    CONSTRAINT chk_consultation_fee
        CHECK (fee >= 0),
    CONSTRAINT chk_consultation_capacity CHECK (
        (consultation_name IN (
            'Quick Consult',
            'Standard Consult',
            'Detailed Consult'
        ) AND duration_minutes IN (15, 30, 60) AND capacity = 1)
        OR
        (consultation_name = 'Health Camp Session'
            AND duration_minutes = 60
            AND capacity BETWEEN 2 AND 10)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS availability_patterns (
    availability_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL
        COMMENT 'ISO weekday: Monday=1 through Sunday=7',
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    effective_from DATE NOT NULL,
    effective_until DATE NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (availability_id),

    -- Enforces that this consultation type belongs to this same doctor.
    CONSTRAINT fk_availability_doctor_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types(doctor_id, consultation_type_id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT chk_availability_day
        CHECK (day_of_week BETWEEN 1 AND 7),
    CONSTRAINT chk_availability_time
        CHECK (end_time > start_time),
    CONSTRAINT chk_availability_dates
        CHECK (effective_until IS NULL OR effective_until >= effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    blackout_date DATE NOT NULL,
    start_time TIME NULL,
    end_time TIME NULL,
    reason VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (blackout_id),
    CONSTRAINT fk_blackout_dates_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors(doctor_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT chk_blackout_time_range CHECK (
        (start_time IS NULL AND end_time IS NULL)
        OR
        (start_time IS NOT NULL AND end_time IS NOT NULL
            AND end_time > start_time)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS appointments (
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    doctor_id CHAR(36) NOT NULL,
    consultation_type_id CHAR(36) NOT NULL,
    slot_id CHAR(36) NOT NULL,
    appointment_start DATETIME(6) NOT NULL,
    appointment_end DATETIME(6) NOT NULL,
    fee_amount DECIMAL(10,2) NOT NULL,
    status ENUM(
        'CONFIRMED',
        'COMPLETED',
        'CANCELLED_BY_PATIENT',
        'CANCELLED_BY_DOCTOR',
        'NO_SHOW'
    ) NOT NULL DEFAULT 'CONFIRMED',
    reschedule_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (appointment_id),

    CONSTRAINT fk_appointments_patient
        FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    -- Enforces that the selected consultation type belongs to this doctor.
    CONSTRAINT fk_appointments_doctor_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types(doctor_id, consultation_type_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    CONSTRAINT chk_appointment_time
        CHECK (appointment_end > appointment_start),
    CONSTRAINT chk_appointment_fee
        CHECK (fee_amount >= 0),
    CONSTRAINT chk_appointment_reschedule_count
        CHECK (reschedule_count <= 2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id CHAR(36) NOT NULL,
    appointment_id CHAR(36) NOT NULL,
    patient_id CHAR(36) NOT NULL,
    original_amount DECIMAL(10,2) NOT NULL,
    refund_percentage DECIMAL(5,2) NOT NULL,
    refund_amount DECIMAL(10,2) NOT NULL,
    reason ENUM(
        'PATIENT_CANCELLATION',
        'DOCTOR_CANCELLATION',
        'NO_SHOW',
        'OTHER'
    ) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    CONSTRAINT fk_refund_ledger_appointment
        FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_refund_ledger_patient
        FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_refund_amounts CHECK (
        original_amount >= 0
        AND refund_amount >= 0
        AND refund_percentage BETWEEN 0 AND 100
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ============================================================
-- Performance indexes
-- These are included in schema.sql so only one SQL file is needed.
-- Run this file once on a new database, in its entirety.
-- ============================================================

-- Doctor directory and filter combinations.
CREATE INDEX idx_doctors_name
    ON doctors (last_name, first_name);

CREATE INDEX idx_doctors_city
    ON doctors (city, state);

CREATE INDEX idx_doctors_hospital
    ON doctors (hospital_id, doctor_id);

CREATE INDEX idx_doctors_active_city
    ON doctors (is_active, city, doctor_id);

-- Specialisation filter and join path.
CREATE INDEX idx_doctor_specialisations_specialisation
    ON doctor_specialisations (specialisation_id, doctor_id);

-- Consultation types returned with public doctor profiles.
CREATE INDEX idx_consultation_types_doctor_active
    ON consultation_types (doctor_id, is_active, consultation_name);

-- Weekly pattern lookup during slot materialisation.
CREATE INDEX idx_availability_doctor_day_active
    ON availability_patterns (doctor_id, day_of_week, is_active, effective_from);

CREATE INDEX idx_availability_consultation_type
    ON availability_patterns (consultation_type_id);

CREATE INDEX idx_availability_doctor_consultation
    ON availability_patterns (doctor_id, consultation_type_id);

-- Blackout lookup by doctor and date.
CREATE INDEX idx_blackout_doctor_date
    ON blackout_dates (doctor_id, blackout_date);

-- Appointment day view and patient history.
CREATE INDEX idx_appointments_doctor_start
    ON appointments (doctor_id, appointment_start, status);

CREATE INDEX idx_appointments_patient_start
    ON appointments (patient_id, appointment_start, status);

CREATE INDEX idx_appointments_slot
    ON appointments (slot_id);

CREATE INDEX idx_appointments_consultation_type
    ON appointments (consultation_type_id);

CREATE INDEX idx_appointments_doctor_consultation
    ON appointments (doctor_id, consultation_type_id);

-- Refund history by patient and appointment.
CREATE INDEX idx_refund_ledger_patient_created
    ON refund_ledger (patient_id, created_at);

CREATE INDEX idx_refund_ledger_appointment
    ON refund_ledger (appointment_id);
