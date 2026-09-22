-- =====================================================================
-- dp_control — platform control plane
--
-- Business state for the pipelines: where each extract got to, what
-- every run did, and which rows were set aside. Kept out of Airflow's
-- own metadata database on purpose; that belongs to Airflow.
--
-- Safe to re-run: every object is created IF NOT EXISTS.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS control;


-- Where each incremental extract got to last time.
-- One row per source table. No row yet means "never extracted":
-- the first run takes the whole table.
CREATE TABLE IF NOT EXISTS control.ingestion_watermark (
    source_db         VARCHAR(40)  NOT NULL,
    source_table      VARCHAR(80)  NOT NULL,
    watermark_column  VARCHAR(40)  NOT NULL DEFAULT 'updated_at',
    last_watermark    TIMESTAMPTZ,
    last_run_id       VARCHAR(250),
    last_row_count    BIGINT,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (source_db, source_table)
);


-- One row per table per run. The first place to look when a number
-- looks wrong: what window was read, how many rows came back, where
-- they were written.
CREATE TABLE IF NOT EXISTS control.pipeline_run_log (
    log_id            BIGSERIAL    PRIMARY KEY,
    dag_id            VARCHAR(250) NOT NULL,
    run_id            VARCHAR(250) NOT NULL,
    source_db         VARCHAR(40),
    source_table      VARCHAR(80),
    watermark_from    TIMESTAMPTZ,
    watermark_to      TIMESTAMPTZ,
    rows_extracted    BIGINT,
    s3_path           TEXT,
    status            VARCHAR(20)  NOT NULL
        CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    error_message     TEXT,
    started_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_run_log_table
    ON control.pipeline_run_log (source_db, source_table, started_at);


-- Daily count of rows set aside instead of loaded, and why.
-- The rows themselves live in S3; this is the summary that alerts
-- are built on.
CREATE TABLE IF NOT EXISTS control.quarantine_register (
    register_id       BIGSERIAL    PRIMARY KEY,
    run_id            VARCHAR(250) NOT NULL,
    source_db         VARCHAR(40)  NOT NULL,
    source_table      VARCHAR(80)  NOT NULL,
    reject_reason     VARCHAR(200) NOT NULL,
    row_count         BIGINT       NOT NULL,
    s3_path           TEXT,
    recorded_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);