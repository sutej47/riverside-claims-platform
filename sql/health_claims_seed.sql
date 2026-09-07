-- =====================================================================
-- health_claims — Reference seed data
-- Riverside Regional Health
--
-- Run this in DBeaver against the health_claims database (Alt+X).
--
-- Two reference tables, both small and hand-written because they come
-- from the persona rather than from a generator:
--   payers               - 9 rows
--   denial_reason_codes  - 25 rows
--
-- billing_providers is NOT here. There are ~2,200 of them and they get
-- generated in Python.
--
-- Safe to re-run: every insert is ON CONFLICT DO NOTHING.
-- =====================================================================

SET search_path TO billing, public;

-- ---------------------------------------------------------------------
-- payers
--
-- Shares are documented here for the generator to read, but are not a
-- column: a share is a property of the claim mix, not of the payer.
-- ---------------------------------------------------------------------
INSERT INTO payers (
    payer_id, payer_name, payer_type, payer_edi_identifier,
    address_line_1, city, state_code, zip_code,
    effective_from, effective_to, is_active
) VALUES

-- Government --------------------------------------------------------
('MCR01', 'Medicare Part A/B - Ohio',        'MEDICARE',     '00180',
 'PO Box 6704',            'Columbus',    'OH', '43216', '2015-01-01', NULL, true),

('OHMCD', 'Ohio Department of Medicaid',     'MEDICAID',     'OHMCD',
 '50 W Town St Suite 400', 'Columbus',    'OH', '43215', '2015-01-01', NULL, true),

-- Commercial --------------------------------------------------------
('ANTOH', 'Anthem Blue Cross Blue Shield Ohio', 'COMMERCIAL', '00332',
 '4361 Irwin Simpson Rd',  'Mason',       'OH', '45040', '2015-01-01', NULL, true),

('UHC01', 'UnitedHealthcare',                'COMMERCIAL',   '87726',
 'PO Box 30555',           'Salt Lake City','UT','84130', '2015-01-01', NULL, true),

('AETBH', 'Aetna Better Health of Ohio',     'COMMERCIAL',   '50023',
 '7400 W Campus Rd',       'New Albany',  'OH', '43054', '2016-07-01', NULL, true),

('CIG01', 'Cigna Healthcare',                'COMMERCIAL',   '62308',
 'PO Box 188061',          'Chattanooga', 'TN', '37422', '2015-01-01', NULL, true),

-- Managed care ------------------------------------------------------
-- Riverside won this contract mid-2022. Claims before effective_from
-- should not exist for this payer - a useful referential check.
('MAPLN', 'Medicare Advantage - Multiple Plans', 'MANAGED_CARE', 'MAPLN',
 'PO Box 14165',           'Lexington',   'KY', '40512', '2022-06-01', NULL, true),

-- Other -------------------------------------------------------------
('OHWCP', 'Ohio Bureau of Workers Compensation', 'WORKERS_COMP', 'OHBWC',
 '30 W Spring St',         'Columbus',    'OH', '43215', '2015-01-01', NULL, true),

('SELFP', 'Self Pay / Uninsured',            'SELF_PAY',     NULL,
 NULL,                     NULL,          NULL, NULL,    '2015-01-01', NULL, true)

ON CONFLICT (payer_id) DO NOTHING;


-- ---------------------------------------------------------------------
-- denial_reason_codes
--
-- CARC = Claim Adjustment Reason Code (why the amount changed)
-- RARC = Remittance Advice Remark Code (extra explanation)
--
-- valid_from / valid_to make this a temporal reference table. Joins
-- against it must be dated:
--     ON  a.reason_code = d.reason_code
--     AND c.service_date BETWEEN d.valid_from
--                            AND COALESCE(d.valid_to, DATE '9999-12-31')
--
-- A plain equi-join here is a bug. It will silently pick up a code
-- version that did not exist on the date of service.
-- ---------------------------------------------------------------------
INSERT INTO denial_reason_codes (
    reason_code, code_type, valid_from, valid_to,
    description, denial_category, is_appealable, typical_resolution_days
) VALUES

-- --- Authorisation ---------------------------------------------------
('197', 'CARC', '2015-01-01', NULL,
 'Precertification, authorization or notification absent',
 'AUTHORISATION', true, 32),
('198', 'CARC', '2015-01-01', NULL,
 'Precertification, authorization exceeded',
 'AUTHORISATION', true, 28),
('15',  'CARC', '2015-01-01', NULL,
 'Authorization number missing, invalid, or does not apply',
 'AUTHORISATION', true, 24),

-- --- Data quality / submission --------------------------------------
('16',  'CARC', '2015-01-01', NULL,
 'Claim/service lacks information or has submission/billing errors',
 'DATA_QUALITY', true, 14),
('125', 'CARC', '2015-01-01', NULL,
 'Submission/billing error(s)',
 'DATA_QUALITY', true, 16),
('4',   'CARC', '2015-01-01', NULL,
 'Procedure code inconsistent with the modifier used',
 'CODING', true, 12),
('11',  'CARC', '2015-01-01', NULL,
 'Diagnosis inconsistent with the procedure',
 'CODING', true, 18),
('181', 'CARC', '2015-01-01', NULL,
 'Procedure code was invalid on the date of service',
 'CODING', true, 10),
('182', 'CARC', '2015-01-01', NULL,
 'Procedure modifier was invalid on the date of service',
 'CODING', true, 10),

-- --- Bundling / pricing ---------------------------------------------
('97',  'CARC', '2015-01-01', NULL,
 'Benefit for this service is included in another service already adjudicated',
 'BUNDLING', false, 21),
('B15', 'CARC', '2015-01-01', NULL,
 'Service requires a qualifying service which has not been received',
 'BUNDLING', true, 26),
('45',  'CARC', '2015-01-01', NULL,
 'Charge exceeds fee schedule/maximum allowable',
 'CONTRACTUAL', false, 0),
('59',  'CARC', '2015-01-01', NULL,
 'Processed based on multiple or concurrent procedure rules',
 'CONTRACTUAL', false, 0),

-- --- Eligibility / coverage -----------------------------------------
('109', 'CARC', '2015-01-01', NULL,
 'Claim not covered by this payer/contractor',
 'ELIGIBILITY', true, 20),
('26',  'CARC', '2015-01-01', NULL,
 'Expenses incurred prior to coverage',
 'ELIGIBILITY', true, 22),
('27',  'CARC', '2015-01-01', NULL,
 'Expenses incurred after coverage terminated',
 'ELIGIBILITY', true, 22),
('96',  'CARC', '2015-01-01', NULL,
 'Non-covered charge(s)',
 'COVERAGE', true, 25),
('204', 'CARC', '2015-01-01', NULL,
 'Service not covered under the patient current benefit plan',
 'COVERAGE', true, 25),
('119', 'CARC', '2015-01-01', NULL,
 'Benefit maximum for this time period has been reached',
 'COVERAGE', false, 0),

-- --- Timing ----------------------------------------------------------
-- The pipeline-caused denial. Claims that sit unsubmitted age out.
-- Worth surfacing as an operational KPI, not just an ML feature.
('29',  'CARC', '2015-01-01', NULL,
 'The time limit for filing has expired',
 'TIMELY_FILING', false, 0),

-- --- Duplicates ------------------------------------------------------
-- Frequently a false positive caused by resubmission handling.
('18',  'CARC', '2015-01-01', NULL,
 'Exact duplicate claim/service',
 'DUPLICATE', true, 8),

-- --- Coordination of benefits ---------------------------------------
('22',  'CARC', '2015-01-01', NULL,
 'This care may be covered by another payer per coordination of benefits',
 'COB', true, 35),
('23',  'CARC', '2015-01-01', NULL,
 'Impact of prior payer(s) adjudication including payments and/or adjustments',
 'COB', false, 0),

-- --- Patient responsibility (not denials, but they reduce payment) ---
('1',   'CARC', '2015-01-01', NULL,
 'Deductible amount',
 'PATIENT_RESPONSIBILITY', false, 0),
('2',   'CARC', '2015-01-01', NULL,
 'Coinsurance amount',
 'PATIENT_RESPONSIBILITY', false, 0),
('3',   'CARC', '2015-01-01', NULL,
 'Co-payment amount',
 'PATIENT_RESPONSIBILITY', false, 0)

ON CONFLICT (reason_code, code_type, valid_from) DO NOTHING;


-- =====================================================================
-- VERIFY
-- =====================================================================
SELECT 'payers' AS table_name, count(*) AS row_count FROM payers
UNION ALL
SELECT 'denial_reason_codes', count(*) FROM denial_reason_codes;

-- Expect: payers 9, denial_reason_codes 26
