-- V004: the specialisation catalogue doctors choose from.
-- INSERT IGNORE + UNIQUE(name) keeps this idempotent.

INSERT IGNORE INTO specialisations (name, description) VALUES
    ('General Medicine', 'Primary care and common illnesses'),
    ('Cardiology', 'Heart and blood vessels'),
    ('Dermatology', 'Skin, hair and nails'),
    ('Paediatrics', 'Children and adolescents'),
    ('Orthopaedics', 'Bones, joints and muscles'),
    ('Gynaecology', 'Women''s reproductive health'),
    ('ENT', 'Ear, nose and throat'),
    ('Ophthalmology', 'Eyes and vision'),
    ('Neurology', 'Brain and nervous system'),
    ('Psychiatry', 'Mental health'),
    ('Dentistry', 'Teeth and oral health'),
    ('Pulmonology', 'Lungs and breathing'),
    ('Gastroenterology', 'Digestive system'),
    ('Endocrinology', 'Hormones and diabetes'),
    ('Nephrology', 'Kidneys'),
    ('Urology', 'Urinary tract'),
    ('Oncology', 'Cancer care');
