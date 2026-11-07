-- V003: secondary indexes, each chosen for a specific query.
-- docs/data-model.md explains every choice against the query it serves.

-- Doctor search by name: WHERE u.role = 'doctor' AND u.full_name LIKE 'prefix%'
-- ORDER BY u.full_name. Equality on role, then a range on full_name, and the
-- index order also serves the ORDER BY.
-- @if index_missing users idx_users_role_full_name
CREATE INDEX idx_users_role_full_name ON users (role, full_name);

-- Doctor search by city (exact) and hospital name (prefix). City alone and
-- city + hospital both use this index. Hospital name alone uses the leading
-- column of uq_hospitals_name_city (name, city).
-- @if index_missing hospitals idx_hospitals_city_name
CREATE INDEX idx_hospitals_city_name ON hospitals (city, name);

-- Doctor search by specialisation, optionally narrowed by hospital.
-- (hospital_id alone is covered by the index InnoDB creates for its FK.)
-- @if index_missing doctors idx_doctors_specialisation_hospital
CREATE INDEX idx_doctors_specialisation_hospital ON doctors (specialisation_id, hospital_id);

-- Public profile and booking: a doctor's active consultation types.
-- @if index_missing consultation_types idx_consultation_types_doctor_active
CREATE INDEX idx_consultation_types_doctor_active ON consultation_types (doctor_id, is_active);

-- Materialiser and availability editor: a doctor's active weekly patterns.
-- @if index_missing availability_patterns idx_availability_doctor_active_day
CREATE INDEX idx_availability_doctor_active_day ON availability_patterns (doctor_id, is_active, day_of_week);

-- Doctor schedule and appointment list: one doctor, a time range, by status.
-- @if index_missing appointments idx_appointments_doctor_start
CREATE INDEX idx_appointments_doctor_start ON appointments (doctor_id, slot_start_utc, status);

-- Patient appointment list (upcoming / past) and the 3-per-doctor limit.
-- @if index_missing appointments idx_appointments_patient_start
CREATE INDEX idx_appointments_patient_start ON appointments (patient_id, slot_start_utc, status);

-- Doctor slot cancellation and the schedule's patient list per slot.
-- @if index_missing appointments idx_appointments_slot
CREATE INDEX idx_appointments_slot ON appointments (slot_id, status);

-- Patient refund history, newest first.
-- @if index_missing refund_ledger idx_refund_ledger_patient_created
CREATE INDEX idx_refund_ledger_patient_created ON refund_ledger (patient_id, created_at);
