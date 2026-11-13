-- V002: bring a database created from the earlier schema.sql in line with
-- the final schema diagram. Every statement is guarded with an @if line, so
-- on a database created by V001 this whole file does nothing.

-- doctors: the name lives in users.full_name, the city in hospitals.city.
-- Columns that are not on the diagram are removed (with their indexes).
-- @if index_exists doctors idx_doctors_name
ALTER TABLE doctors DROP INDEX idx_doctors_name;
-- @if index_exists doctors idx_doctors_city
ALTER TABLE doctors DROP INDEX idx_doctors_city;
-- @if index_exists doctors idx_doctors_active_city
ALTER TABLE doctors DROP INDEX idx_doctors_active_city;
-- @if column_exists doctors first_name
ALTER TABLE doctors DROP COLUMN first_name;
-- @if column_exists doctors last_name
ALTER TABLE doctors DROP COLUMN last_name;
-- @if column_exists doctors phone
ALTER TABLE doctors DROP COLUMN phone;
-- @if column_exists doctors city
ALTER TABLE doctors DROP COLUMN city;
-- @if column_exists doctors state
ALTER TABLE doctors DROP COLUMN state;
-- @if column_exists doctors country
ALTER TABLE doctors DROP COLUMN country;
-- @if column_exists doctors bio
ALTER TABLE doctors DROP COLUMN bio;
-- @if column_exists doctors is_active
ALTER TABLE doctors DROP COLUMN is_active;
-- @if index_exists doctors idx_doctors_hospital
ALTER TABLE doctors ADD INDEX fk_doctors_hospital (hospital_id), DROP INDEX idx_doctors_hospital;
-- @if column_missing doctors timezone
ALTER TABLE doctors ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata' AFTER profile_photo_s3_key;
-- @if constraint_exists doctors chk_doctors_experience
ALTER TABLE doctors DROP CHECK chk_doctors_experience;
-- @if column_type_is_not doctors years_experience int
ALTER TABLE doctors MODIFY COLUMN years_experience INT NULL;
-- @if constraint_missing doctors chk_doctors_experience
ALTER TABLE doctors ADD CONSTRAINT chk_doctors_experience CHECK (years_experience IS NULL OR years_experience BETWEEN 0 AND 80);

-- consultation_types: INT columns as on the diagram, duration fixed to 15/30/60.
-- @if constraint_exists consultation_types chk_consultation_capacity
ALTER TABLE consultation_types DROP CHECK chk_consultation_capacity;
-- @if constraint_exists consultation_types chk_consultation_duration
ALTER TABLE consultation_types DROP CHECK chk_consultation_duration;
-- @if column_type_is_not consultation_types duration_minutes int
ALTER TABLE consultation_types MODIFY COLUMN duration_minutes INT NOT NULL;
-- @if column_type_is_not consultation_types capacity int
ALTER TABLE consultation_types MODIFY COLUMN capacity INT NOT NULL DEFAULT 1;
-- @if constraint_missing consultation_types chk_consultation_duration
ALTER TABLE consultation_types ADD CONSTRAINT chk_consultation_duration CHECK (duration_minutes IN (15, 30, 60));
-- @if constraint_missing consultation_types chk_consultation_capacity
ALTER TABLE consultation_types ADD CONSTRAINT chk_consultation_capacity CHECK (
    (name = 'Health Camp Session' AND duration_minutes = 60 AND capacity BETWEEN 2 AND 10)
    OR (name <> 'Health Camp Session' AND capacity = 1)
);
-- @if index_exists consultation_types idx_consultation_types_doctor_active
ALTER TABLE consultation_types DROP INDEX idx_consultation_types_doctor_active;

-- availability_patterns: no effective date range on the diagram.
-- @if index_exists availability_patterns idx_availability_doctor_day_active
ALTER TABLE availability_patterns DROP INDEX idx_availability_doctor_day_active;
-- @if index_exists availability_patterns idx_availability_consultation_type
ALTER TABLE availability_patterns DROP INDEX idx_availability_consultation_type;
-- @if constraint_exists availability_patterns chk_availability_dates
ALTER TABLE availability_patterns DROP CHECK chk_availability_dates;
-- @if column_exists availability_patterns effective_from
ALTER TABLE availability_patterns DROP COLUMN effective_from;
-- @if column_exists availability_patterns effective_until
ALTER TABLE availability_patterns DROP COLUMN effective_until;
-- @if column_exists availability_patterns start_time
ALTER TABLE availability_patterns
    MODIFY COLUMN start_time TIME NOT NULL COMMENT 'Doctor local time',
    MODIFY COLUMN end_time TIME NOT NULL COMMENT 'Doctor local time';

-- appointments: availability_id is a plain column on the diagram (no FK),
-- active_flag is not on the diagram, counters are INT. Indexes identical to
-- the V003 ones are kept (InnoDB uses them for the foreign keys).
-- @if constraint_exists appointments fk_appointments_availability
ALTER TABLE appointments DROP FOREIGN KEY fk_appointments_availability;
-- @if index_exists appointments fk_appointments_availability
ALTER TABLE appointments DROP INDEX fk_appointments_availability;
-- @if column_exists appointments active_flag
ALTER TABLE appointments DROP COLUMN active_flag;
-- @if column_is_not_nullable appointments availability_id
ALTER TABLE appointments MODIFY COLUMN availability_id CHAR(36) NULL;
-- @if constraint_exists appointments chk_appointment_reschedule_count
ALTER TABLE appointments DROP CHECK chk_appointment_reschedule_count;
-- @if column_type_is_not appointments reschedule_count int
ALTER TABLE appointments MODIFY COLUMN reschedule_count INT NOT NULL DEFAULT 0;
-- @if column_type_is_not appointments version int
ALTER TABLE appointments MODIFY COLUMN version INT NOT NULL DEFAULT 1;
-- @if constraint_missing appointments chk_appointment_reschedule_count
ALTER TABLE appointments ADD CONSTRAINT chk_appointment_reschedule_count CHECK (reschedule_count BETWEEN 0 AND 2);
-- @if index_exists appointments idx_appointments_slot
ALTER TABLE appointments DROP INDEX idx_appointments_slot;
-- @if index_exists appointments idx_appointments_consultation_type
ALTER TABLE appointments DROP INDEX idx_appointments_consultation_type;

-- refund_ledger: enum order as on the diagram.
-- @if column_type_is_not refund_ledger tier enum('full','partial','none')
ALTER TABLE refund_ledger MODIFY COLUMN tier ENUM('FULL', 'PARTIAL', 'NONE') NOT NULL;
