-- =====================================================================
-- health_ehr — Clinical / EHR source system
-- Riverside Regional Health
--
-- The clinical side of the estate. Owns the patient. Knows nothing
-- about claims, and does not share a key with billing.
--
-- Apply with:
--   docker exec -i dp-postgres-sources psql -U dp_admin -d health_ehr \
--     < sql/health_ehr_schema.sql
--
-- =====================================================================
-- DESIGN DECISIONS  (read these before changing anything)
-- =====================================================================
--
-- 1. The patient key here is MRN. Billing's key is an account number.
--    Nothing joins them. That is the whole point (problem A3).
--    A real hospital runs an EHR from one vendor and a billing system
--    from another; the "link" is a nightly interface that drifts, and
--    reconciling the two is permanent engineering work.
--
-- 2. The only bridge is patient_coverage.subscriber_id, matching
--    claims.subscriber_id — and it is a bad bridge on purpose:
--      - it identifies the SUBSCRIBER, not the patient. A child's claim
--        carries the parent's member ID (claims.patient_relationship
--        19=child, 01=spouse). Joining on it alone merges families.
--      - member IDs are reissued at plan renewal, so the same ID points
--        at different coverage in different years. Any join must be
--        temporal (effective_from / effective_to).
--      - roughly 8% of coverage rows are missing entirely.
--    The remainder resolves on demographics: name, DOB, gender, ZIP.
--    That is fuzzy matching, and it is meant to be.
--
-- 3. Duplicate patients are allowed and expected.
--    The same human can hold more than one MRN — registered twice at
--    different facilities, name misspelt, DOB typo. patient_identifiers
--    holds the alternates. No unique constraint on (name, dob), for
--    exactly that reason.
--
-- 4. Addresses are history, not a column.
--    A 2023 claim should match the address the patient held in 2023.
--    Flattening it onto patients would quietly break historical matching.
--
-- 5. The EHR keeps its OWN provider table.
--    clinical.providers and billing.billing_providers describe the same
--    humans with different keys, different names and different active
--    flags. They disagree. Reconciling them is its own piece of work.
--
-- 6. Diagnoses and procedures are child tables, not columns.
--    An encounter carries up to 25 diagnoses. Flattening to
--    primary_diagnosis only is the classic loss of grain, and it is what
--    makes claim-to-encounter reconciliation impossible later.
--
-- 7. ICD and CPT codes carry no version column.
--    Codes retired between 2023 and 2025 sit alongside current ones with
--    nothing marking which vocabulary they came from (problem A7).
--
-- 8. This schema carries real PHI shapes: names, dates of birth,
--    addresses, phone numbers, free-text notes. Nothing is
--    de-identified. HIPAA Safe Harbor de-identification (problem C1)
--    happens in the pipeline, which is where it happens in real life.
--
-- 9. Audit fields on every table, same as billing. updated_at is what
--    incremental extraction watermarks on (problem B2).
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS clinical;
SET search_path TO clinical, public;


-- =====================================================================
-- REFERENCE / MASTER DATA
-- =====================================================================

-- ---------------------------------------------------------------------
-- departments — facility and department master
-- ---------------------------------------------------------------------
CREATE TABLE departments (
    department_id           VARCHAR(12)  PRIMARY KEY,
    department_name         VARCHAR(80)  NOT NULL,
    facility_code           VARCHAR(10)  NOT NULL,
    facility_name           VARCHAR(80),
    department_type         VARCHAR(30)
        CHECK (department_type IN ('INPATIENT_UNIT','CLINIC','EMERGENCY',
                                   'SURGERY','LAB','IMAGING','PHARMACY',
                                   'ADMIN','OTHER')),
    -- Cost centre, as finance knows it. Does not match billing's
    -- facility_code cleanly.
    cost_centre_code        VARCHAR(15),
    bed_count               INT,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

COMMENT ON TABLE departments IS
  'Department master. cost_centre_code is finance''s key and does not '
  'reconcile with billing.facility_code without a mapping.';


-- ---------------------------------------------------------------------
-- providers — the EHR's own view of a clinician
--
-- Same humans as billing.billing_providers, different keys, different
-- spellings, different active flags. The two systems disagree because
-- they are maintained by different teams on different schedules.
-- ---------------------------------------------------------------------
CREATE TABLE providers (
    provider_id             VARCHAR(12)  PRIMARY KEY,   -- EHR's own key
    -- NPI is present but nullable: residents and locums often have no
    -- NPI recorded in the EHR even though billing has one.
    npi                     CHAR(10),

    last_name               VARCHAR(60),
    first_name              VARCHAR(60),
    credentials             VARCHAR(20),    -- MD, DO, NP, PA, RN
    primary_department_id   VARCHAR(12),
    specialty               VARCHAR(80),    -- free text, not taxonomy coded

    hire_date               DATE,
    departure_date          DATE,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (primary_department_id) REFERENCES departments (department_id)
);

COMMENT ON TABLE providers IS
  'Clinical provider master. specialty is free text here and taxonomy '
  'coded in billing; npi is nullable here and mandatory there.';

CREATE INDEX idx_providers_npi ON providers (npi);


-- =====================================================================
-- PATIENT MASTER
-- =====================================================================

-- ---------------------------------------------------------------------
-- patients — the clinical patient record
--
-- MRN is the EHR's key and appears nowhere in billing. Demographics
-- here are the ground truth that identity resolution has to match on.
-- ---------------------------------------------------------------------
CREATE TABLE patients (
    mrn                     VARCHAR(15)  PRIMARY KEY,   -- MRN + 8 digits

    last_name               VARCHAR(60)  NOT NULL,
    first_name              VARCHAR(60)  NOT NULL,
    middle_initial          CHAR(1),
    -- Suffixes are entered inconsistently (JR, Jr., Jr) on purpose.
    name_suffix             VARCHAR(10),
    -- Maiden or previous name. A married patient may appear under
    -- either, which is a real source of duplicate MRNs.
    previous_last_name      VARCHAR(60),

    date_of_birth           DATE,
    -- M / F / U. Coded, not spelled out, as EHRs do.
    gender_code             CHAR(1)
        CHECK (gender_code IN ('M','F','U')),

    phone_home              VARCHAR(20),
    phone_mobile            VARCHAR(20),
    email                   VARCHAR(120),

    -- Last four only. The full SSN is not stored, which is both good
    -- practice and a weaker matching key than you would like.
    ssn_last_four           CHAR(4),

    preferred_language      VARCHAR(30),
    marital_status_code     CHAR(1),

    -- Set when a duplicate is found and merged. The losing MRN stays in
    -- the table pointing at the winner, because downstream systems still
    -- hold references to it. Chains are possible.
    merged_into_mrn         VARCHAR(15),

    registration_date       DATE,
    deceased_date           DATE,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

COMMENT ON TABLE patients IS
  'Clinical patient master, keyed by MRN. Duplicates are permitted: the '
  'same person may hold several MRNs. merged_into_mrn records a merge '
  'already resolved in the source; unmerged duplicates remain.';

CREATE INDEX idx_patients_name_dob ON patients (last_name, first_name, date_of_birth);
CREATE INDEX idx_patients_dob      ON patients (date_of_birth);
CREATE INDEX idx_patients_merged   ON patients (merged_into_mrn);
CREATE INDEX idx_patients_updated  ON patients (updated_at);


-- ---------------------------------------------------------------------
-- patient_identifiers — every other ID the patient is known by
--
-- The single most useful table for identity resolution, and the one a
-- real estate keeps worst. Holds retired MRNs, IDs issued by other
-- facilities before a merger, and — sometimes — the billing account
-- number, populated by an interface that fails silently.
-- ---------------------------------------------------------------------
CREATE TABLE patient_identifiers (
    identifier_id           BIGSERIAL    PRIMARY KEY,
    mrn                     VARCHAR(15)  NOT NULL,

    identifier_type         VARCHAR(20)  NOT NULL
        CHECK (identifier_type IN ('MRN_RETIRED','MRN_LEGACY',
                                   'ACCOUNT_NUMBER','MEMBER_ID',
                                   'DRIVERS_LICENCE','OTHER')),
    identifier_value        VARCHAR(30)  NOT NULL,
    -- Which system issued it. Legacy facility codes appear here.
    issuing_system          VARCHAR(30),

    assigned_date           DATE,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

COMMENT ON TABLE patient_identifiers IS
  'Alternate identifiers. ACCOUNT_NUMBER rows are the only direct link '
  'to billing and cover roughly 60% of patients: the interface that '
  'writes them drops rows without erroring. The rest must be resolved '
  'on demographics.';

CREATE INDEX idx_pat_ident_mrn   ON patient_identifiers (mrn);
CREATE INDEX idx_pat_ident_value ON patient_identifiers (identifier_type, identifier_value);


-- ---------------------------------------------------------------------
-- patient_addresses — address history, not a current-value column
--
-- A 2023 claim should match the address the patient held in 2023.
-- Flattening this onto patients would quietly break historical matching.
-- ---------------------------------------------------------------------
CREATE TABLE patient_addresses (
    address_id              BIGSERIAL    PRIMARY KEY,
    mrn                     VARCHAR(15)  NOT NULL,

    address_type            VARCHAR(15)  NOT NULL DEFAULT 'HOME'
        CHECK (address_type IN ('HOME','MAILING','TEMPORARY','WORK')),
    address_line_1          VARCHAR(100),
    address_line_2          VARCHAR(100),
    city                    VARCHAR(60),
    state_code              CHAR(2),
    zip_code                VARCHAR(10),    -- 5 or 9 digit, inconsistently
    county                  VARCHAR(40),

    effective_from          DATE         NOT NULL,
    effective_to            DATE,
    is_current              BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

COMMENT ON TABLE patient_addresses IS
  'Type 2 address history. is_current is maintained by the EHR and is '
  'sometimes wrong — more than one current row per patient exists.';

CREATE INDEX idx_pat_addr_mrn     ON patient_addresses (mrn);
CREATE INDEX idx_pat_addr_zip     ON patient_addresses (zip_code);
CREATE INDEX idx_pat_addr_current ON patient_addresses (mrn, is_current);


-- =====================================================================
-- COVERAGE
-- =====================================================================

-- ---------------------------------------------------------------------
-- patient_coverage — insurance the patient is registered under
--
-- The nearest thing to a bridge into billing. subscriber_id matches
-- claims.subscriber_id, but identifies the policy holder rather than
-- the patient, and is reissued at renewal. Any join through it must be
-- temporal and must handle dependants.
-- ---------------------------------------------------------------------
CREATE TABLE patient_coverage (
    coverage_id             BIGSERIAL    PRIMARY KEY,
    mrn                     VARCHAR(15)  NOT NULL,

    -- Matches billing.payers.payer_id in the other database. No FK:
    -- cross-database references cannot be enforced, and the EHR's payer
    -- list drifts from billing's.
    payer_id                VARCHAR(5),
    payer_name_raw          VARCHAR(120),   -- as typed at registration

    -- The member ID printed on the insurance card.
    subscriber_id           VARCHAR(30),
    -- 18=self, 01=spouse, 19=child, 21=unknown
    relationship_code       VARCHAR(2),
    -- Populated only when the patient is not the subscriber.
    subscriber_last_name    VARCHAR(60),
    subscriber_first_name   VARCHAR(60),
    subscriber_dob          DATE,

    group_number            VARCHAR(30),
    plan_name               VARCHAR(80),

    -- Coverage priority when a patient holds more than one policy.
    coverage_rank           SMALLINT     NOT NULL DEFAULT 1
        CHECK (coverage_rank BETWEEN 1 AND 3),

    effective_from          DATE         NOT NULL,
    effective_to            DATE,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

COMMENT ON TABLE patient_coverage IS
  'Insurance coverage as registered in the EHR. Effective dated: the '
  'same subscriber_id can belong to different coverage in different '
  'years, so joins to claims must be dated, not just keyed.';

CREATE INDEX idx_coverage_mrn        ON patient_coverage (mrn);
CREATE INDEX idx_coverage_subscriber ON patient_coverage (subscriber_id);
CREATE INDEX idx_coverage_dates      ON patient_coverage (effective_from, effective_to);
CREATE INDEX idx_coverage_updated    ON patient_coverage (updated_at);


-- =====================================================================
-- ENCOUNTERS AND CLINICAL ACTIVITY
-- =====================================================================

-- ---------------------------------------------------------------------
-- encounters — a visit, admission or procedure
--
-- Claims are billed against encounters, but the claim does not carry
-- the encounter_id. Linking a claim to its encounter needs patient
-- identity plus service date plus facility — another join the pipeline
-- has to construct rather than look up.
-- ---------------------------------------------------------------------
CREATE TABLE encounters (
    encounter_id            VARCHAR(20)  PRIMARY KEY,   -- ENC + YY + 9 digits
    mrn                     VARCHAR(15)  NOT NULL,

    encounter_type          VARCHAR(20)  NOT NULL
        CHECK (encounter_type IN ('INPATIENT','OUTPATIENT','EMERGENCY',
                                  'OFFICE','TELEHEALTH','LAB','IMAGING')),
    facility_code           VARCHAR(10),
    department_id           VARCHAR(12),

    -- The EHR's own provider key, not the NPI. Resolving this to a
    -- billing provider is part of the reconciliation work.
    attending_provider_id   VARCHAR(12),
    referring_provider_id   VARCHAR(12),

    admission_datetime      TIMESTAMPTZ,
    -- Nullable: an open encounter has not been discharged yet, which is
    -- how in-flight rows leak into an extract taken mid-stay.
    discharge_datetime      TIMESTAMPTZ,
    length_of_stay_days     INT,

    -- 01=emergency, 02=urgent, 03=elective, 09=information not available
    admission_type_code     VARCHAR(2),
    admission_source_code   VARCHAR(2),
    discharge_status_code   VARCHAR(2),

    encounter_status        VARCHAR(20)  NOT NULL DEFAULT 'CLOSED'
        CHECK (encounter_status IN ('OPEN','CLOSED','CANCELLED')),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (mrn) REFERENCES patients (mrn),
    FOREIGN KEY (department_id) REFERENCES departments (department_id)
);

COMMENT ON TABLE encounters IS
  'Clinical visits. OPEN encounters have no discharge_datetime and will '
  'change after extraction — the classic in-flight row problem.';

CREATE INDEX idx_encounters_mrn       ON encounters (mrn);
CREATE INDEX idx_encounters_admission ON encounters (admission_datetime);
CREATE INDEX idx_encounters_facility  ON encounters (facility_code, admission_datetime);
CREATE INDEX idx_encounters_updated   ON encounters (updated_at);


-- ---------------------------------------------------------------------
-- encounter_diagnoses — ICD-10-CM codes, one row per code
--
-- An encounter carries up to 25 diagnoses. Flattening to a single
-- primary_diagnosis column loses the grain and makes claim-to-encounter
-- reconciliation impossible.
-- ---------------------------------------------------------------------
CREATE TABLE encounter_diagnoses (
    encounter_diagnosis_id  BIGSERIAL    PRIMARY KEY,
    encounter_id            VARCHAR(20)  NOT NULL,

    diagnosis_sequence      SMALLINT     NOT NULL,   -- 1 = principal
    -- No version column, on purpose. Codes retired between 2023 and
    -- 2025 sit here beside current ones (problem A7).
    icd10_code              VARCHAR(10)  NOT NULL,
    icd10_description       VARCHAR(255),
    -- Present on admission: Y, N, U, W, 1
    poa_indicator           CHAR(1),
    diagnosis_type          VARCHAR(20)
        CHECK (diagnosis_type IN ('PRINCIPAL','SECONDARY','ADMITTING',
                                  'REASON_FOR_VISIT','EXTERNAL_CAUSE')),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (encounter_id) REFERENCES encounters (encounter_id)
);

CREATE INDEX idx_enc_dx_encounter ON encounter_diagnoses (encounter_id);
CREATE INDEX idx_enc_dx_code      ON encounter_diagnoses (icd10_code);


-- ---------------------------------------------------------------------
-- encounter_procedures — what was actually done
--
-- The clinical record of a procedure. Billing charges for it separately
-- in claim_lines, and the two do not always agree: procedures performed
-- but never charged, and charges with no procedure behind them.
-- ---------------------------------------------------------------------
CREATE TABLE encounter_procedures (
    encounter_procedure_id  BIGSERIAL    PRIMARY KEY,
    encounter_id            VARCHAR(20)  NOT NULL,

    procedure_sequence      SMALLINT     NOT NULL,
    -- CPT for outpatient, ICD-10-PCS for inpatient. Which one is in
    -- here is not marked; you have to infer it from the encounter type.
    procedure_code          VARCHAR(10)  NOT NULL,
    procedure_description   VARCHAR(255),
    -- Up to four CPT modifiers, stored as one delimited string because
    -- that is how the interface delivers it.
    modifiers               VARCHAR(20),

    performed_datetime      TIMESTAMPTZ,
    performing_provider_id  VARCHAR(12),
    quantity                INT          DEFAULT 1,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (encounter_id) REFERENCES encounters (encounter_id)
);

COMMENT ON COLUMN encounter_procedures.modifiers IS
  'Delimited list, not normalised. Splitting it is the pipeline''s job.';

CREATE INDEX idx_enc_proc_encounter ON encounter_procedures (encounter_id);
CREATE INDEX idx_enc_proc_code      ON encounter_procedures (procedure_code);


-- ---------------------------------------------------------------------
-- orders — clinical orders placed during an encounter
--
-- Lab, imaging and medication orders. Volume table: several per
-- encounter, so this is where partitioning and skew start to matter.
-- ---------------------------------------------------------------------
CREATE TABLE orders (
    order_id                VARCHAR(20)  PRIMARY KEY,   -- ORD + YY + 9 digits
    encounter_id            VARCHAR(20)  NOT NULL,
    mrn                     VARCHAR(15)  NOT NULL,

    order_type              VARCHAR(20)  NOT NULL
        CHECK (order_type IN ('LAB','IMAGING','MEDICATION','PROCEDURE',
                              'CONSULT','NURSING','DIET')),
    order_code              VARCHAR(20),
    order_description       VARCHAR(200),

    ordering_provider_id    VARCHAR(12),
    ordered_datetime        TIMESTAMPTZ  NOT NULL,
    -- Nullable: an order placed but never completed stays open forever.
    completed_datetime      TIMESTAMPTZ,

    order_status            VARCHAR(20)  NOT NULL DEFAULT 'COMPLETED'
        CHECK (order_status IN ('ORDERED','IN_PROGRESS','COMPLETED',
                                'CANCELLED','EXPIRED')),
    priority_code           VARCHAR(10),   -- ROUTINE, STAT, URGENT

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (encounter_id) REFERENCES encounters (encounter_id),
    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

CREATE INDEX idx_orders_encounter ON orders (encounter_id);
CREATE INDEX idx_orders_mrn       ON orders (mrn);
CREATE INDEX idx_orders_ordered   ON orders (ordered_datetime);
CREATE INDEX idx_orders_updated   ON orders (updated_at);


-- ---------------------------------------------------------------------
-- observations — vitals and measurements
--
-- The largest table in the schema by row count. Values are stored as
-- text because an EHR stores "140/90", "negative" and "12.4" in the
-- same column. Typing it is the pipeline's problem, and sentinel
-- values (999, -1, "UNKNOWN") live here too (problem A6).
-- ---------------------------------------------------------------------
CREATE TABLE observations (
    observation_id          BIGSERIAL    PRIMARY KEY,
    encounter_id            VARCHAR(20)  NOT NULL,
    mrn                     VARCHAR(15)  NOT NULL,

    observation_code        VARCHAR(20)  NOT NULL,   -- LOINC where known
    observation_name        VARCHAR(120),
    -- Free text. "140/90", "12.4", "negative", "999" all appear here.
    observation_value       VARCHAR(100),
    unit_of_measure         VARCHAR(20),
    reference_range         VARCHAR(40),
    abnormal_flag           VARCHAR(5),    -- H, L, HH, LL, N

    observed_datetime       TIMESTAMPTZ  NOT NULL,
    recorded_by_provider_id VARCHAR(12),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (encounter_id) REFERENCES encounters (encounter_id),
    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

COMMENT ON TABLE observations IS
  'Vitals and results. observation_value is untyped text on purpose: '
  'this is where sentinel values and unit inconsistency live.';

CREATE INDEX idx_obs_encounter ON observations (encounter_id);
CREATE INDEX idx_obs_mrn       ON observations (mrn);
CREATE INDEX idx_obs_code      ON observations (observation_code);
CREATE INDEX idx_obs_datetime  ON observations (observed_datetime);


-- ---------------------------------------------------------------------
-- clinical_notes — free-text documentation
--
-- Dense PHI: names, dates, addresses and phone numbers appear inside
-- note_text. This is the table that makes HIPAA Safe Harbor
-- de-identification (problem C1) a real piece of work rather than a
-- column mask.
-- ---------------------------------------------------------------------
CREATE TABLE clinical_notes (
    note_id                 BIGSERIAL    PRIMARY KEY,
    encounter_id            VARCHAR(20)  NOT NULL,
    mrn                     VARCHAR(15)  NOT NULL,

    note_type               VARCHAR(30)
        CHECK (note_type IN ('PROGRESS','ADMISSION','DISCHARGE_SUMMARY',
                             'OPERATIVE','CONSULT','NURSING','RADIOLOGY')),
    note_text               TEXT,
    authored_by_provider_id VARCHAR(12),
    authored_datetime       TIMESTAMPTZ,
    -- An unsigned note can still be edited, so its content is not final.
    is_signed               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'EHR',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    FOREIGN KEY (encounter_id) REFERENCES encounters (encounter_id),
    FOREIGN KEY (mrn) REFERENCES patients (mrn)
);

COMMENT ON TABLE clinical_notes IS
  'Free-text notes containing embedded PHI. Unsigned notes change after '
  'extraction, so watermarking on updated_at is not enough on its own.';

CREATE INDEX idx_notes_encounter ON clinical_notes (encounter_id);
CREATE INDEX idx_notes_mrn       ON clinical_notes (mrn);
CREATE INDEX idx_notes_updated   ON clinical_notes (updated_at);


-- =====================================================================
-- AUDIT TRIGGERS
-- =====================================================================

-- updated_at must move on every UPDATE or incremental extraction
-- silently misses rows — problem B2, to be solved deliberately rather
-- than suffered by accident.
CREATE OR REPLACE FUNCTION clinical.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    FOR t IN
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'clinical' AND table_type = 'BASE TABLE'
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON clinical.%I
             FOR EACH ROW EXECUTE FUNCTION clinical.set_updated_at()', t, t);
    END LOOP;
END $$;


-- Read-only role for the extraction pipeline. Airflow has no business
-- being able to write to a source system.
CREATE ROLE ehr_reader NOLOGIN;
GRANT USAGE ON SCHEMA clinical TO ehr_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA clinical TO ehr_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinical
    GRANT SELECT ON TABLES TO ehr_reader;

COMMIT;


-- Expect 12 tables.
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'clinical'
ORDER BY table_name;
