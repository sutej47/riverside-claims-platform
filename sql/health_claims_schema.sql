-- =====================================================================
-- health_claims — Billing / Claims source system
-- Riverside Regional Health
--
-- Models the 837 (claim submission) and 835 (remittance advice) EDI
-- transaction sets as a relational billing system would store them.
--
-- Apply with:
--   docker exec -i dp-postgres-sources psql -U dataeng -d health_claims \
--     < sql/health_claims_schema.sql
--
-- =====================================================================
-- DESIGN DECISIONS  (read these before changing anything)
-- =====================================================================
--
-- 1. NO foreign keys to patients.
--    Patients live in health_ehr, a different database. Postgres cannot
--    enforce cross-database references and neither can a real hospital
--    estate. patient_account_number is stored as plain text with no
--    constraint. This is what forces identity resolution (problem A3)
--    into the pipeline instead of the source.
--
-- 2. NO foreign keys on payer_id or billing_provider_npi from claims.
--    Deliberately absent so orphan records (problem A5) can be injected.
--    Real billing systems reference providers who have since left the
--    network. If the constraint existed, the problem could not exist.
--
-- 3. claim_lines is the atomic grain.
--    A claim is NOT the fact grain. Lines are adjudicated individually:
--    line 1 paid, line 2 denied, line 3 adjusted. Modelling at claim
--    level is the classic dimensional mistake (problem A4).
--
-- 4. Claim versioning via original_claim_id + claim_frequency_code.
--    Every submission gets its own claim_id. Replacements (freq 7) and
--    voids (freq 8) point back at the original. Nothing here marks which
--    version is "current" — that is the pipeline's job (problem A1).
--
-- 5. Remittances are a separate arrival, not a column on claims.
--    An 835 lands 21-68 days after the 837, sometimes never. Storing
--    paid_amount on the claim row would erase the lag that creates the
--    late-arriving-facts problem (A2) and would hand the ML model a
--    leaked label (D1).
--
-- 6. Money is NUMERIC(12,2), never FLOAT.
--    Binary floating point cannot represent 0.10 exactly. In a system
--    that reconciles payments to the cent, this is not a preference.
--
-- 7. Audit fields on every table.
--    created_at / updated_at / source_system / is_deleted. updated_at is
--    what incremental extraction watermarks on (problem B2); is_deleted
--    is the soft-delete flag that stops hard deletes from silently
--    vanishing from the warehouse.
--
-- 8. Charge and amount columns are nullable.
--    They should not be, but sentinel values and missing data (problem
--    A6) need somewhere to land. Enforcing NOT NULL here would push the
--    mess upstream where you cannot practise cleaning it.
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS billing;
SET search_path TO billing, public;


-- =====================================================================
-- REFERENCE / MASTER DATA
-- =====================================================================

-- ---------------------------------------------------------------------
-- payers — insurance companies Riverside bills
-- ---------------------------------------------------------------------
CREATE TABLE payers (
    payer_id                VARCHAR(5)   PRIMARY KEY,
    payer_name              VARCHAR(120) NOT NULL,
    payer_type              VARCHAR(20)  NOT NULL
        CHECK (payer_type IN ('MEDICARE','MEDICAID','COMMERCIAL',
                              'MANAGED_CARE','WORKERS_COMP','SELF_PAY','OTHER')),
    -- The identifier that actually appears in the 837 loop 2010BB
    payer_edi_identifier    VARCHAR(30),
    address_line_1          VARCHAR(100),
    city                    VARCHAR(60),
    state_code              CHAR(2),
    zip_code                VARCHAR(10),
    -- Contract effective dating. A payer can be inactive for new claims
    -- while remittances for old claims are still arriving.
    effective_from          DATE         NOT NULL,
    effective_to            DATE,
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

COMMENT ON TABLE payers IS
  'Insurance payer master. Effective dating matters: remittances arrive '
  'for payers no longer accepting new claims.';


-- ---------------------------------------------------------------------
-- billing_providers — providers as they appear on claims
--
-- Note this is the BILLING view of a provider, not the clinical one.
-- health_ehr has its own provider table with different keys. The two
-- disagree, on purpose.
-- ---------------------------------------------------------------------
CREATE TABLE billing_providers (
    provider_npi            CHAR(10)     PRIMARY KEY,
    provider_last_name      VARCHAR(60),
    provider_first_name     VARCHAR(60),
    organisation_name       VARCHAR(120),   -- populated for org providers
    provider_type           VARCHAR(20)  NOT NULL
        CHECK (provider_type IN ('INDIVIDUAL','ORGANISATION')),
    taxonomy_code           VARCHAR(10),    -- NUCC provider taxonomy
    specialty_description   VARCHAR(80),
    tax_id                  VARCHAR(15),    -- EIN or SSN, PHI-adjacent
    facility_code           VARCHAR(10),    -- which Riverside facility
    is_employed             BOOLEAN      NOT NULL DEFAULT true,
    enrolment_date          DATE,
    termination_date        DATE,           -- set when a provider leaves
    is_active               BOOLEAN      NOT NULL DEFAULT true,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

COMMENT ON COLUMN billing_providers.termination_date IS
  'Set when a provider leaves. Claims referencing terminated providers '
  'keep arriving for months — a legitimate source of orphan records.';


-- ---------------------------------------------------------------------
-- denial_reason_codes — CARC / RARC reference
--
-- Temporally versioned. Codes are retired and added; a denial from 2023
-- must be interpreted against the 2023 code set (problem A7).
-- ---------------------------------------------------------------------
CREATE TABLE denial_reason_codes (
    reason_code             VARCHAR(10)  NOT NULL,
    code_type               VARCHAR(6)   NOT NULL
        CHECK (code_type IN ('CARC','RARC')),
    valid_from              DATE         NOT NULL,
    valid_to                DATE,
    description             TEXT         NOT NULL,
    -- Your own grouping, not part of the standard. Marts aggregate on
    -- this rather than on 200+ raw codes.
    denial_category         VARCHAR(40),
    is_appealable           BOOLEAN,
    typical_resolution_days INT,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    PRIMARY KEY (reason_code, code_type, valid_from)
);


-- =====================================================================
-- 837 — CLAIM SUBMISSION
-- =====================================================================

-- ---------------------------------------------------------------------
-- claims — 837 header, one row per SUBMISSION (not per business claim)
--
-- A single business claim may have three rows here: the original, a
-- replacement, and a void. Working out which one counts is the
-- pipeline's job.
-- ---------------------------------------------------------------------
CREATE TABLE claims (
    claim_id                VARCHAR(20)  PRIMARY KEY,

    -- Versioning ------------------------------------------------------
    -- 1 = original, 7 = replacement, 8 = void. On 7 and 8,
    -- original_claim_id points at the claim being corrected.
    claim_frequency_code    CHAR(1)      NOT NULL DEFAULT '1',
    original_claim_id       VARCHAR(20),
    version_seq             INT          NOT NULL DEFAULT 1,

    -- Patient linkage -------------------------------------------------
    -- Deliberately unconstrained. health_ehr owns the patient; billing
    -- only knows an account number, and the two do not reconcile
    -- cleanly. This is the identity resolution problem, by design.
    patient_account_number  VARCHAR(20)  NOT NULL,
    subscriber_id           VARCHAR(30),     -- member ID on the card
    patient_relationship    VARCHAR(2),      -- 18=self, 01=spouse, 19=child

    -- Claim classification --------------------------------------------
    claim_type              VARCHAR(5)   NOT NULL
        CHECK (claim_type IN ('837P','837I','837D')),
    bill_type_code          VARCHAR(4),      -- institutional only
    place_of_service_code   VARCHAR(2),      -- professional only
    facility_code           VARCHAR(10),

    -- Parties ---------------------------------------------------------
    billing_provider_npi    CHAR(10)     NOT NULL,
    rendering_provider_npi  CHAR(10),
    referring_provider_npi  CHAR(10),
    payer_id                VARCHAR(5)   NOT NULL,
    payer_sequence          SMALLINT     NOT NULL DEFAULT 1,  -- 1=primary, 2=secondary

    -- Dates -----------------------------------------------------------
    statement_from_date     DATE,
    statement_to_date       DATE,
    admission_date          DATE,            -- institutional
    discharge_date          DATE,            -- institutional
    discharge_status_code   VARCHAR(2),
    submission_date         DATE         NOT NULL,

    -- Institutional grouping ------------------------------------------
    drg_code                VARCHAR(10),
    admission_type_code     VARCHAR(2),
    admission_source_code   VARCHAR(2),

    -- Money -----------------------------------------------------------
    total_charge_amount     NUMERIC(12,2),
    prior_payment_amount    NUMERIC(12,2) DEFAULT 0,

    -- Operational -----------------------------------------------------
    claim_status            VARCHAR(20)  NOT NULL DEFAULT 'SUBMITTED',
    clearinghouse_id        VARCHAR(20),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

-- Watermark index: incremental extraction reads on updated_at.
CREATE INDEX idx_claims_updated_at   ON claims (updated_at);
CREATE INDEX idx_claims_original     ON claims (original_claim_id)
    WHERE original_claim_id IS NOT NULL;
CREATE INDEX idx_claims_payer_date   ON claims (payer_id, submission_date);
CREATE INDEX idx_claims_account      ON claims (patient_account_number);

COMMENT ON COLUMN claims.claim_frequency_code IS
  '837 frequency: 1 original, 7 replacement, 8 void. Nothing in this '
  'table says which version is current — resolve it downstream.';


-- ---------------------------------------------------------------------
-- claim_diagnoses — ICD-10 codes, ordered
--
-- diagnosis_sequence is what claim_lines point at. Sequence 1 is the
-- principal diagnosis.
-- ---------------------------------------------------------------------
CREATE TABLE claim_diagnoses (
    claim_diagnosis_id      BIGSERIAL    PRIMARY KEY,
    claim_id                VARCHAR(20)  NOT NULL
        REFERENCES claims(claim_id),
    diagnosis_sequence      SMALLINT     NOT NULL,
    diagnosis_code          VARCHAR(10)  NOT NULL,
    -- 'ICD10' for everything after Oct 2015, but old claims and badly
    -- mapped data still carry ICD9. Keep the version explicit.
    code_version            VARCHAR(6)   NOT NULL DEFAULT 'ICD10',
    diagnosis_type          VARCHAR(20)  NOT NULL DEFAULT 'OTHER'
        CHECK (diagnosis_type IN ('PRINCIPAL','ADMITTING','OTHER',
                                  'EXTERNAL_CAUSE','REASON_FOR_VISIT')),
    present_on_admission    CHAR(1),         -- Y/N/U/W, institutional

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    UNIQUE (claim_id, diagnosis_sequence)
);

CREATE INDEX idx_claim_dx_code       ON claim_diagnoses (diagnosis_code);
CREATE INDEX idx_claim_dx_updated_at ON claim_diagnoses (updated_at);


-- ---------------------------------------------------------------------
-- claim_lines — service lines. THIS IS THE ATOMIC GRAIN.
--
-- Everything financial reconciles here. Claim-level totals are a
-- rollup, and if they disagree with the sum of lines, the lines win.
-- ---------------------------------------------------------------------
CREATE TABLE claim_lines (
    claim_line_id           VARCHAR(26)  PRIMARY KEY,   -- claim_id + line no
    claim_id                VARCHAR(20)  NOT NULL
        REFERENCES claims(claim_id),
    line_number             SMALLINT     NOT NULL,

    -- Service ---------------------------------------------------------
    service_date_from       DATE,
    service_date_to         DATE,
    procedure_code          VARCHAR(10),
    procedure_code_type     VARCHAR(10)  DEFAULT 'CPT'
        CHECK (procedure_code_type IN ('CPT','HCPCS','ICD10PCS','CDT')),
    modifier_1              VARCHAR(2),
    modifier_2              VARCHAR(2),
    modifier_3              VARCHAR(2),
    modifier_4              VARCHAR(2),
    revenue_code            VARCHAR(4),      -- institutional
    place_of_service_code   VARCHAR(2),

    -- Which diagnoses justify this line. Stored as an array of
    -- claim_diagnoses.diagnosis_sequence values, mirroring the 837's
    -- diagnosis pointer structure.
    diagnosis_pointers      SMALLINT[],

    -- Quantity and money ----------------------------------------------
    service_units           NUMERIC(9,3),
    unit_type               VARCHAR(10)  DEFAULT 'UN',   -- UN, MJ (minutes)
    charge_amount           NUMERIC(12,2),

    rendering_provider_npi  CHAR(10),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    UNIQUE (claim_id, line_number)
);

CREATE INDEX idx_claim_lines_claim      ON claim_lines (claim_id);
CREATE INDEX idx_claim_lines_proc       ON claim_lines (procedure_code);
CREATE INDEX idx_claim_lines_svc_date   ON claim_lines (service_date_from);
CREATE INDEX idx_claim_lines_updated_at ON claim_lines (updated_at);

COMMENT ON TABLE claim_lines IS
  'Atomic grain of the billing system. Lines adjudicate independently: '
  'one claim can have a paid line, a denied line and an adjusted line.';


-- ---------------------------------------------------------------------
-- claim_status_history — status transitions
--
-- Append-only. Gives you a real CDC source and lets you measure how long
-- claims sit in each state.
-- ---------------------------------------------------------------------
CREATE TABLE claim_status_history (
    status_history_id       BIGSERIAL    PRIMARY KEY,
    claim_id                VARCHAR(20)  NOT NULL
        REFERENCES claims(claim_id),
    status_code             VARCHAR(20)  NOT NULL,
    status_date             TIMESTAMPTZ  NOT NULL,
    status_source           VARCHAR(20),     -- CLEARINGHOUSE, PAYER, INTERNAL
    status_note             TEXT,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX idx_status_hist_claim ON claim_status_history (claim_id, status_date);


-- =====================================================================
-- 835 — REMITTANCE ADVICE
--
-- Three levels, mirroring the EDI structure:
--   remittances        -> the payment (one cheque / EFT)
--   remittance_claims  -> per-claim adjudication within that payment
--   remittance_lines   -> per-service-line adjudication
--
-- The claim-to-remittance relationship is many-to-many. One payment
-- covers many claims; one claim can be paid across several payments.
-- =====================================================================

-- ---------------------------------------------------------------------
-- remittances — 835 header, one row per payment received
-- ---------------------------------------------------------------------
CREATE TABLE remittances (
    remittance_id           VARCHAR(20)  PRIMARY KEY,
    payer_id                VARCHAR(5)   NOT NULL,

    payment_method_code     VARCHAR(3)   NOT NULL DEFAULT 'ACH'
        CHECK (payment_method_code IN ('ACH','CHK','FWT','NON')),
    check_eft_number        VARCHAR(20),
    total_payment_amount    NUMERIC(14,2),

    -- production_date is when the payer generated the file;
    -- received_date is when Riverside actually got it. The gap is real
    -- and is part of the late-arrival lag.
    production_date         DATE,
    received_date           DATE         NOT NULL,
    posted_date             DATE,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX idx_remit_payer_date  ON remittances (payer_id, received_date);
CREATE INDEX idx_remit_updated_at  ON remittances (updated_at);


-- ---------------------------------------------------------------------
-- remittance_claims — claim-level adjudication
--
-- No unique constraint on (remittance_id, claim_id) and no unique
-- constraint on claim_id: split payments (problem A4) require both to
-- stay open.
-- ---------------------------------------------------------------------
CREATE TABLE remittance_claims (
    remittance_claim_id     BIGSERIAL    PRIMARY KEY,
    remittance_id           VARCHAR(20)  NOT NULL
        REFERENCES remittances(remittance_id),
    -- Not a FK. A remittance can reference a claim_id the billing
    -- system does not have — payer-side corrections, or claims
    -- submitted by an acquired practice.
    claim_id                VARCHAR(20)  NOT NULL,

    -- The payer's own control number. Needed to match corrections.
    payer_claim_control_number VARCHAR(40),

    -- 1 processed as primary, 2 as secondary, 4 denied,
    -- 19/20/21 forwarded, 22 reversal of previous payment
    claim_status_code       VARCHAR(2)   NOT NULL,

    total_charge_amount     NUMERIC(12,2),
    total_paid_amount       NUMERIC(12,2),
    patient_responsibility_amount NUMERIC(12,2),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX idx_remit_claims_claim  ON remittance_claims (claim_id);
CREATE INDEX idx_remit_claims_remit  ON remittance_claims (remittance_id);
CREATE INDEX idx_remit_claims_status ON remittance_claims (claim_status_code);

COMMENT ON TABLE remittance_claims IS
  'Many-to-many bridge between claims and payments. One claim may appear '
  'in several remittances (split payment, then a reversal, then a '
  'corrected payment).';


-- ---------------------------------------------------------------------
-- remittance_lines — service-line adjudication
--
-- Where mixed outcomes live: line 1 paid, line 2 denied, line 3
-- bundled. The reason a claim-grain fact table gives wrong answers.
-- ---------------------------------------------------------------------
CREATE TABLE remittance_lines (
    remittance_line_id      BIGSERIAL    PRIMARY KEY,
    remittance_claim_id     BIGINT       NOT NULL
        REFERENCES remittance_claims(remittance_claim_id),
    -- Not a FK: the payer may report a line the billing system cannot
    -- match (bundling, re-sequencing).
    claim_line_id           VARCHAR(26),

    line_number             SMALLINT,
    -- What the payer says was performed. May differ from what was
    -- submitted when the payer re-codes the service.
    adjudicated_procedure_code VARCHAR(10),
    modifier_1              VARCHAR(2),
    modifier_2              VARCHAR(2),

    charge_amount           NUMERIC(12,2),
    paid_amount             NUMERIC(12,2),
    allowed_amount          NUMERIC(12,2),
    units_paid              NUMERIC(9,3),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX idx_remit_lines_rc   ON remittance_lines (remittance_claim_id);
CREATE INDEX idx_remit_lines_line ON remittance_lines (claim_line_id);


-- ---------------------------------------------------------------------
-- claim_adjustments — CARC adjustment detail
--
-- Attaches at claim level OR line level, never both. The CHECK enforces
-- exactly one parent — this is the table that tells you WHY money was
-- not paid, and it is the source of your ML label.
-- ---------------------------------------------------------------------
CREATE TABLE claim_adjustments (
    adjustment_id           BIGSERIAL    PRIMARY KEY,
    remittance_claim_id     BIGINT
        REFERENCES remittance_claims(remittance_claim_id),
    remittance_line_id      BIGINT
        REFERENCES remittance_lines(remittance_line_id),

    -- CO contractual obligation, PR patient responsibility,
    -- OA other adjustment, PI payer initiated
    adjustment_group_code   VARCHAR(2)   NOT NULL
        CHECK (adjustment_group_code IN ('CO','PR','OA','PI','CR')),
    adjustment_reason_code  VARCHAR(10)  NOT NULL,   -- CARC
    remark_code             VARCHAR(10),             -- RARC
    adjustment_amount       NUMERIC(12,2) NOT NULL,
    adjustment_quantity     NUMERIC(9,3),

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false,

    CONSTRAINT chk_adjustment_single_parent CHECK (
        (remittance_claim_id IS NOT NULL AND remittance_line_id IS NULL)
     OR (remittance_claim_id IS NULL     AND remittance_line_id IS NOT NULL)
    )
);

CREATE INDEX idx_adj_rc     ON claim_adjustments (remittance_claim_id);
CREATE INDEX idx_adj_rl     ON claim_adjustments (remittance_line_id);
CREATE INDEX idx_adj_reason ON claim_adjustments (adjustment_reason_code);

COMMENT ON TABLE claim_adjustments IS
  'The denial reason. This is where the ML label comes from — and every '
  'column here is post-adjudication, so none of it may be used as a '
  'feature (label leakage, problem D1).';


-- ---------------------------------------------------------------------
-- claim_submission_batches — how claims were sent
--
-- Gives you a natural extraction unit and lets you model the month-end
-- batching pattern, plus timely-filing denials caused by batch delay.
-- ---------------------------------------------------------------------
CREATE TABLE claim_submission_batches (
    batch_id                VARCHAR(20)  PRIMARY KEY,
    payer_id                VARCHAR(5),
    clearinghouse_id        VARCHAR(20),
    batch_created_at        TIMESTAMPTZ  NOT NULL,
    batch_submitted_at      TIMESTAMPTZ,
    claim_count             INT,
    total_charge_amount     NUMERIC(14,2),
    batch_status            VARCHAR(20)  NOT NULL DEFAULT 'CREATED',
    rejection_count         INT          DEFAULT 0,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    source_system           VARCHAR(30)  NOT NULL DEFAULT 'BILLING',
    is_deleted              BOOLEAN      NOT NULL DEFAULT false
);

ALTER TABLE claims ADD COLUMN batch_id VARCHAR(20);
CREATE INDEX idx_claims_batch ON claims (batch_id);


-- =====================================================================
-- EXTRACTION SUPPORT
-- =====================================================================

-- Trigger to maintain updated_at. Without this, incremental extraction
-- silently misses updates — which is exactly problem B2, and you want
-- to solve it deliberately rather than suffer it by accident.
CREATE OR REPLACE FUNCTION billing.set_updated_at()
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
        WHERE table_schema = 'billing' AND table_type = 'BASE TABLE'
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON billing.%I
             FOR EACH ROW EXECUTE FUNCTION billing.set_updated_at()', t, t);
    END LOOP;
END $$;


-- Read-only role for the extraction pipeline. Airflow has no business
-- being able to write to a source system.
CREATE ROLE billing_reader NOLOGIN;
GRANT USAGE ON SCHEMA billing TO billing_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO billing_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA billing
    GRANT SELECT ON TABLES TO billing_reader;

COMMIT;


-- =====================================================================
-- VERIFY
-- =====================================================================
--   \dt billing.*
--   SELECT table_name, count(*) AS columns
--   FROM information_schema.columns
--   WHERE table_schema = 'billing'
--   GROUP BY table_name ORDER BY table_name;
--
-- Expect 11 tables.
-- =====================================================================
