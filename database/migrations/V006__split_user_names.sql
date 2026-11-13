-- V006: store a person's name as first_name and last_name.
--
-- users.full_name is replaced by two columns. Existing names are split at the
-- first space: the first word becomes first_name and the rest last_name. A
-- one-word name keeps an empty last_name; new accounts must give both (the API
-- checks it). The search index on (role, full_name) becomes
-- (role, first_name, last_name), which also gives search results their order.
--
-- Every statement that depends on the current state is guarded, so a run that
-- stopped part-way can be repeated.

-- @if column_missing users first_name
ALTER TABLE users
    ADD COLUMN first_name VARCHAR(60) NULL AFTER user_id,
    ADD COLUMN last_name  VARCHAR(60) NULL AFTER first_name;

-- @if column_exists users full_name
UPDATE users
SET first_name = LEFT(SUBSTRING_INDEX(TRIM(full_name), ' ', 1), 60),
    last_name  = LEFT(TRIM(SUBSTRING(TRIM(full_name), CHAR_LENGTH(SUBSTRING_INDEX(TRIM(full_name), ' ', 1)) + 1)), 60)
WHERE first_name IS NULL;

ALTER TABLE users
    MODIFY first_name VARCHAR(60) NOT NULL,
    MODIFY last_name  VARCHAR(60) NOT NULL;

-- @if constraint_missing users chk_users_first_name_nonempty
ALTER TABLE users ADD CONSTRAINT chk_users_first_name_nonempty CHECK (CHAR_LENGTH(TRIM(first_name)) > 0);

-- @if index_exists users idx_users_role_full_name
DROP INDEX idx_users_role_full_name ON users;

-- @if column_exists users full_name
ALTER TABLE users DROP COLUMN full_name;

-- Doctor search: role = 'doctor', ordered by first and last name.
-- @if index_missing users idx_users_role_names
CREATE INDEX idx_users_role_names ON users (role, first_name, last_name);
