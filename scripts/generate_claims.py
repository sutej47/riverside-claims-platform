"""
Generate 837 claim submissions for Riverside Regional Health.

Populates five tables in health_claims.billing:
    claims, claim_diagnoses, claim_lines, claim_status_history,
    claim_submission_batches

Claims are built FROM the clinical encounters already in health_ehr, but
they do not carry the encounter id, the MRN, or anything else that would
make the join easy. Billing knows an account number and a member id, and
that is all — which is the identity resolution problem (A3) as it actually
appears in a hospital.

Deliberate messiness built in here, not injected later:
  - a business claim can be three rows: original, replacement, void. No
    column says which one is current (problem A1).
  - ~2% of claims name a payer_id or an NPI that is not in the master
    tables, because the interface let them through (problem A5).
  - claim lines and clinical procedures do not fully agree: procedures
    performed but never charged, and charges with no procedure behind
    them.
  - submission dates bunch at month end, because that is when billing
    staff clear their queues.
  - claim_lines is the atomic grain. Header totals are a rollup and can
    disagree with the sum of the lines (problem A4).

WHAT IS NOT HERE, ON PURPOSE: nothing about denial, payment or
adjudication. That arrives later in the 835 remittance, days or weeks
after submission. Putting an outcome column on the claim would leak the
label straight into the model (problem D1).

Usage:
    python3 generate_claims.py --dry-run
    python3 generate_claims.py --start 2025-10-01 --end 2025-12-31
    python3 generate_claims.py --start 2025-10-01 --end 2025-12-31 --truncate
"""

import argparse
import os
import random
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from identifiers import make_account_number, make_claim_id

SEED = 20250831
BATCH = 5_000

# Claim type mix from the persona
CLAIM_TYPE_BY_ENCOUNTER = {
    "INPATIENT": "837I",
    "EMERGENCY": "837P",
    "OUTPATIENT": "837P",
}

# Place of service, professional claims only
POS_BY_ENCOUNTER = {
    "EMERGENCY": "23",      # emergency room, hospital
    "OUTPATIENT": "22",     # on-campus outpatient hospital
    "INPATIENT": "21",      # inpatient hospital
}

# Institutional bill types
BILL_TYPES = ["111", "112", "117", "131", "137"]

DRG_CODES = ["470", "871", "291", "690", "193", "247", "641", "603",
             "392", "064", "480", "853", "177", "312", "378"]

CLEARINGHOUSES = ["AVAILITY", "CHANGE_HC", "WAYSTAR"]

# Versioning
REPLACEMENT_RATE = 0.06
VOID_RATE = 0.01

# Referential gaps the interface let through
ORPHAN_PAYER_RATE = 0.008
ORPHAN_NPI_RATE = 0.012
UNKNOWN_PAYERS = ["ZZZ99", "XX001", "TEMP1"]

# Clinical / billing disagreement
PROCEDURE_NOT_CHARGED_RATE = 0.05
# Extra lines a coder adds with no clinical procedure behind them:
# supplies, injections, facility fees. Weighted so the finished claim
# averages the ~3.4 lines the persona calls for.
EXTRA_LINE_WEIGHTS = [(0, 0.28), (1, 0.30), (2, 0.22), (3, 0.13), (4, 0.07)]

# Header total disagrees with the sum of lines
HEADER_MISMATCH_RATE = 0.015

STATUS_FLOW = [
    ("CREATED", "INTERNAL", 0),
    ("BATCHED", "INTERNAL", 0),
    ("SUBMITTED", "CLEARINGHOUSE", 0),
    ("ACCEPTED", "CLEARINGHOUSE", 1),
    ("IN_PROCESS", "PAYER", 3),
]

# Fallback CPT codes for charges with no clinical procedure behind them
FALLBACK_CPT = [
    ("99213", 128), ("99214", 186), ("80053", 62), ("85025", 44),
    ("71046", 185), ("36415", 18), ("97110", 64), ("J1885", 34),
    ("A4550", 22), ("99283", 428),
]

CPT_MODIFIERS = ["25", "59", "76", "LT", "RT", "50", "GP", "TC", "26"]

REVENUE_CODES = ["0250", "0300", "0320", "0360", "0450", "0636",
                 "0730", "0910", "0120", "0110"]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def mrn_to_seq(mrn):
    """MRN00418823 -> 418823."""
    return int(mrn[3:])


def submission_date_for(rng, service_date):
    """
    Return the date the claim was submitted.

    Most claims go out within a fortnight, but the queue is cleared at
    month end, so dates bunch around the 28th to the 31st.
    """
    lag = max(1, int(rng.gauss(9, 6)))
    submitted = service_date + timedelta(days=lag)

    if rng.random() < 0.34:
        # Pushed to the month-end batch
        month_end = (submitted.replace(day=1) + timedelta(days=32)) \
            .replace(day=1) - timedelta(days=1)
        submitted = month_end - timedelta(days=rng.randint(0, 3))

    return submitted


def pick_coverage(rng, coverages, service_date):
    """
    Return the coverage in force on the service date.

    Falls back to whatever is on file if none of the spells cover the
    date, which is what a billing clerk does under pressure and is why
    a naive temporal join finds the wrong payer.
    """
    if not coverages:
        return None
    in_force = [c for c in coverages
                if c["from"] <= service_date and
                (c["to"] is None or service_date < c["to"])]
    if in_force:
        return rng.choice(in_force)
    return coverages[-1] if rng.random() < 0.7 else None


# ---------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------

def build(rng, start, end, encounters, procedures_by_enc, diagnoses_by_enc,
          coverage_by_mrn, npis, payer_ids):
    """Build every claim table. Returns a dict of table name -> rows."""

    claims, claim_dx, claim_lines, status_history = [], [], [], []
    batches = []

    org_npis = [n for n in npis]
    claim_seq = 0
    batch_rows = defaultdict(list)      # (submission_date, payer) -> claims

    for enc in encounters:
        (encounter_id, mrn, etype, facility, admit, discharge,
         los, admission_type, admission_source, discharge_status) = enc

        service_date = admit.date()
        if not (start <= service_date <= end):
            continue

        coverage = pick_coverage(rng, coverage_by_mrn.get(mrn), service_date)
        account_number = make_account_number(mrn_to_seq(mrn))

        if coverage:
            payer_id = coverage["payer"]
            subscriber_id = coverage["subscriber"]
            relationship = coverage["relationship"]
        else:
            payer_id, subscriber_id, relationship = "SELFP", None, "18"

        # The interface occasionally lets an unknown payer through
        if rng.random() < ORPHAN_PAYER_RATE:
            payer_id = rng.choice(UNKNOWN_PAYERS)

        procedures = procedures_by_enc.get(encounter_id, [])
        diagnoses = diagnoses_by_enc.get(encounter_id, [])
        if not diagnoses:
            continue                        # nothing to bill against

        # An inpatient stay bills a facility claim and a professional
        # claim; everything else bills once.
        if etype == "INPATIENT":
            claim_plan = [("837I", procedures[:len(procedures) // 2 + 1]),
                          ("837P", procedures[len(procedures) // 2 + 1:])]
        else:
            claim_plan = [("837P", procedures)]

        for claim_type, own_procedures in claim_plan:
            if claim_type == "837P" and not own_procedures \
                    and rng.random() < 0.5:
                continue

            claim_seq += 1
            claim_id = make_claim_id(claim_seq, service_date.year)
            submitted = submission_date_for(rng, service_date)

            billing_npi = rng.choice(org_npis)
            rendering_npi = rng.choice(org_npis)
            if rng.random() < ORPHAN_NPI_RATE:
                # An NPI that passes a format check and matches nothing
                rendering_npi = f"1{rng.randint(10**8, 10**9 - 1)}"

            # --- lines --------------------------------------------------
            lines = []
            line_no = 0
            for proc_code, proc_mods, proc_qty, charge in own_procedures:
                if rng.random() < PROCEDURE_NOT_CHARGED_RATE:
                    continue                # performed, never billed
                line_no += 1
                mods = (proc_mods.split(",") if proc_mods else [])
                lines.append((line_no, proc_code, mods, proc_qty, charge))

            # Charges with nothing clinical behind them: supplies,
            # injections, facility fees added by the coder.
            n_extra = rng.choices([w[0] for w in EXTRA_LINE_WEIGHTS],
                                  [w[1] for w in EXTRA_LINE_WEIGHTS])[0]
            for _ in range(max(n_extra, 1 if not lines else 0)):
                line_no += 1
                code, base = rng.choice(FALLBACK_CPT)
                lines.append((line_no, code,
                              rng.sample(CPT_MODIFIERS, 1)
                              if rng.random() < 0.15 else [],
                              1, base))
                if line_no > 12:
                    break

            line_total = 0.0
            for line_number, code, mods, qty, base in lines:
                charge = round(base * qty * rng.uniform(0.92, 1.34), 2)
                line_total += charge
                claim_lines.append((
                    f"{claim_id}-{line_number:03d}",
                    claim_id, line_number,
                    service_date,
                    service_date + timedelta(days=los or 0),
                    code,
                    "HCPCS" if code[0].isalpha() else "CPT",
                    mods[0] if len(mods) > 0 else None,
                    mods[1] if len(mods) > 1 else None,
                    None, None,
                    rng.choice(REVENUE_CODES) if claim_type == "837I" else None,
                    POS_BY_ENCOUNTER[etype] if claim_type == "837P" else None,
                    # Diagnosis pointers into claim_diagnoses
                    sorted(rng.sample(range(1, min(len(diagnoses), 4) + 1),
                                      rng.randint(1, min(len(diagnoses), 4)))),
                    qty, "UN", charge,
                    rendering_npi,
                ))

            # The header is a rollup, and it is not always right
            header_total = round(line_total, 2)
            if rng.random() < HEADER_MISMATCH_RATE:
                header_total = round(line_total * rng.uniform(0.88, 1.12), 2)

            # --- diagnoses ----------------------------------------------
            for seq, (code, dx_type, poa) in enumerate(diagnoses[:12], 1):
                claim_dx.append((
                    claim_id, seq, code, "ICD10",
                    "PRINCIPAL" if seq == 1 else
                    ("ADMITTING" if dx_type == "ADMITTING" else "OTHER"),
                    poa if claim_type == "837I" else None,
                ))

            # --- header -------------------------------------------------
            is_institutional = claim_type == "837I"
            claims.append((
                claim_id, "1", None, 1,
                account_number, subscriber_id, relationship,
                claim_type,
                rng.choice(BILL_TYPES) if is_institutional else None,
                POS_BY_ENCOUNTER[etype] if not is_institutional else None,
                facility,
                billing_npi, rendering_npi,
                rng.choice(org_npis) if rng.random() < 0.31 else None,
                payer_id, 1,
                service_date,
                service_date + timedelta(days=los or 0),
                service_date if is_institutional else None,
                (discharge.date() if discharge else None)
                if is_institutional else None,
                discharge_status if is_institutional else None,
                submitted,
                rng.choice(DRG_CODES) if is_institutional else None,
                admission_type if is_institutional else None,
                admission_source if is_institutional else None,
                header_total, 0,
                "SUBMITTED",
                rng.choice(CLEARINGHOUSES),
            ))

            batch_rows[(submitted, payer_id)].append((claim_id, header_total))

            # --- status history -----------------------------------------
            for status, source, offset in STATUS_FLOW:
                status_history.append((
                    claim_id, status,
                    datetime.combine(submitted + timedelta(days=offset),
                                     datetime.min.time(),
                                     tzinfo=timezone.utc)
                    + timedelta(hours=rng.randint(6, 20)),
                    source, None,
                ))

            # --- versioning ---------------------------------------------
            # A corrected claim is a NEW row pointing at the original.
            # Nothing marks the original as superseded.
            if rng.random() < REPLACEMENT_RATE:
                claim_seq += 1
                replacement_id = make_claim_id(claim_seq, service_date.year)
                resubmitted = submitted + timedelta(days=rng.randint(14, 75))

                claims.append((
                    replacement_id, "7", claim_id, 2,
                    account_number, subscriber_id, relationship,
                    claim_type,
                    rng.choice(BILL_TYPES) if is_institutional else None,
                    POS_BY_ENCOUNTER[etype] if not is_institutional else None,
                    facility, billing_npi, rendering_npi, None,
                    payer_id, 1,
                    service_date, service_date + timedelta(days=los or 0),
                    service_date if is_institutional else None,
                    (discharge.date() if discharge else None)
                    if is_institutional else None,
                    discharge_status if is_institutional else None,
                    resubmitted,
                    rng.choice(DRG_CODES) if is_institutional else None,
                    admission_type if is_institutional else None,
                    admission_source if is_institutional else None,
                    # A correction usually changes the money
                    round(header_total * rng.uniform(0.72, 1.18), 2), 0,
                    "SUBMITTED", rng.choice(CLEARINGHOUSES),
                ))

                for seq, (code, dx_type, poa) in enumerate(diagnoses[:12], 1):
                    claim_dx.append((
                        replacement_id, seq, code, "ICD10",
                        "PRINCIPAL" if seq == 1 else "OTHER",
                        poa if is_institutional else None,
                    ))

                for line_number, code, mods, qty, base in lines:
                    claim_lines.append((
                        f"{replacement_id}-{line_number:03d}",
                        replacement_id, line_number,
                        service_date, service_date + timedelta(days=los or 0),
                        code, "HCPCS" if code[0].isalpha() else "CPT",
                        mods[0] if len(mods) > 0 else None,
                        mods[1] if len(mods) > 1 else None,
                        None, None,
                        rng.choice(REVENUE_CODES) if is_institutional else None,
                        POS_BY_ENCOUNTER[etype] if not is_institutional else None,
                        [1], qty, "UN",
                        round(base * qty * rng.uniform(0.92, 1.34), 2),
                        rendering_npi,
                    ))

                for status, source, offset in STATUS_FLOW:
                    status_history.append((
                        replacement_id, status,
                        datetime.combine(resubmitted + timedelta(days=offset),
                                         datetime.min.time(),
                                         tzinfo=timezone.utc)
                        + timedelta(hours=rng.randint(6, 20)),
                        source, None,
                    ))

                batch_rows[(resubmitted, payer_id)].append(
                    (replacement_id, header_total))

            elif rng.random() < VOID_RATE:
                # Voided entirely. The original row stays exactly as it is.
                claim_seq += 1
                void_id = make_claim_id(claim_seq, service_date.year)
                voided = submitted + timedelta(days=rng.randint(10, 60))

                claims.append((
                    void_id, "8", claim_id, 2,
                    account_number, subscriber_id, relationship,
                    claim_type, None, None, facility,
                    billing_npi, rendering_npi, None,
                    payer_id, 1,
                    service_date, service_date + timedelta(days=los or 0),
                    None, None, None, voided,
                    None, None, None,
                    0, 0, "VOIDED", rng.choice(CLEARINGHOUSES),
                ))

    # --- submission batches ---------------------------------------------
    for i, ((submitted, payer_id), rows) in enumerate(
            sorted(batch_rows.items()), 1):
        created = datetime.combine(submitted, datetime.min.time(),
                                   tzinfo=timezone.utc) \
            + timedelta(hours=rng.randint(2, 8))
        batches.append((
            f"BATCH{submitted.strftime('%y%m%d')}{i % 100000:05d}",
            payer_id if len(payer_id) == 5 else None,
            rng.choice(CLEARINGHOUSES),
            created,
            created + timedelta(hours=rng.randint(1, 10)),
            len(rows),
            round(sum(t for _, t in rows), 2),
            "SUBMITTED",
            # The clearinghouse bounces a few on format alone
            sum(1 for _ in rows if rng.random() < 0.014),
        ))

    return {
        "claims": claims,
        "claim_diagnoses": claim_dx,
        "claim_lines": claim_lines,
        "claim_status_history": status_history,
        "claim_submission_batches": batches,
    }


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def connect(dbname):
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5433"),
        dbname=dbname,
        user=os.getenv("PGUSER", "dp_admin"),
        password=os.getenv("PGPASSWORD", "admin"),
    )


def fetch_clinical(start, end):
    """Read the encounters and their children out of the EHR."""
    conn = connect("health_ehr")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT encounter_id, mrn, encounter_type, facility_code,
                       admission_datetime, discharge_datetime,
                       length_of_stay_days, admission_type_code,
                       admission_source_code, discharge_status_code
                FROM clinical.encounters
                WHERE encounter_status <> 'CANCELLED'
                  AND admission_datetime::date BETWEEN %s AND %s
                ORDER BY admission_datetime
            """, (start, end))
            encounters = cur.fetchall()

            enc_ids = tuple(e[0] for e in encounters)
            if not enc_ids:
                return encounters, {}, {}, {}

            cur.execute("""
                SELECT p.encounter_id, p.procedure_code, p.modifiers,
                       p.quantity
                FROM clinical.encounter_procedures p
                JOIN clinical.encounters e USING (encounter_id)
                WHERE e.admission_datetime::date BETWEEN %s AND %s
                ORDER BY p.encounter_id, p.procedure_sequence
            """, (start, end))
            procedures = defaultdict(list)
            for enc_id, code, mods, qty in cur.fetchall():
                procedures[enc_id].append((code, mods, qty or 1, 0))

            cur.execute("""
                SELECT d.encounter_id, d.icd10_code, d.diagnosis_type,
                       d.poa_indicator
                FROM clinical.encounter_diagnoses d
                JOIN clinical.encounters e USING (encounter_id)
                WHERE e.admission_datetime::date BETWEEN %s AND %s
                ORDER BY d.encounter_id, d.diagnosis_sequence
            """, (start, end))
            diagnoses = defaultdict(list)
            for enc_id, code, dx_type, poa in cur.fetchall():
                diagnoses[enc_id].append((code, dx_type, poa))

            cur.execute("""
                SELECT mrn, payer_id, subscriber_id, relationship_code,
                       effective_from, effective_to
                FROM clinical.patient_coverage
                ORDER BY mrn, effective_from
            """)
            coverage = defaultdict(list)
            for mrn, payer, sub, rel, eff_from, eff_to in cur.fetchall():
                coverage[mrn].append({"payer": payer, "subscriber": sub,
                                      "relationship": rel,
                                      "from": eff_from, "to": eff_to})

        return encounters, procedures, diagnoses, coverage
    finally:
        conn.close()


def fetch_billing_reference():
    """Read the NPIs and payer ids the claims should point at."""
    conn = connect("health_claims")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT provider_npi FROM billing.billing_providers "
                        "WHERE is_active")
            npis = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT payer_id FROM billing.payers")
            payers = [r[0] for r in cur.fetchall()]
        return npis, payers
    finally:
        conn.close()


# Charges come from a small internal fee schedule; the clinical system
# does not know what anything costs.
FEE_SCHEDULE_BASE = 96


def apply_charges(rng, procedures):
    """Attach a base charge to every clinical procedure."""
    for enc_id, procs in procedures.items():
        procedures[enc_id] = [
            (code, mods, qty,
             FEE_SCHEDULE_BASE + (sum(ord(c) for c in code) % 40) * 27)
            for code, mods, qty, _ in procs
        ]
    return procedures


INSERTS = {
    "claims": """
        INSERT INTO billing.claims (
            claim_id, claim_frequency_code, original_claim_id, version_seq,
            patient_account_number, subscriber_id, patient_relationship,
            claim_type, bill_type_code, place_of_service_code, facility_code,
            billing_provider_npi, rendering_provider_npi,
            referring_provider_npi, payer_id, payer_sequence,
            statement_from_date, statement_to_date, admission_date,
            discharge_date, discharge_status_code, submission_date,
            drg_code, admission_type_code, admission_source_code,
            total_charge_amount, prior_payment_amount, claim_status,
            clearinghouse_id
        ) VALUES %s ON CONFLICT (claim_id) DO NOTHING
    """,
    "claim_diagnoses": """
        INSERT INTO billing.claim_diagnoses (
            claim_id, diagnosis_sequence, diagnosis_code, code_version,
            diagnosis_type, present_on_admission
        ) VALUES %s ON CONFLICT (claim_id, diagnosis_sequence) DO NOTHING
    """,
    "claim_lines": """
        INSERT INTO billing.claim_lines (
            claim_line_id, claim_id, line_number, service_date_from,
            service_date_to, procedure_code, procedure_code_type,
            modifier_1, modifier_2, modifier_3, modifier_4, revenue_code,
            place_of_service_code, diagnosis_pointers, service_units,
            unit_type, charge_amount, rendering_provider_npi
        ) VALUES %s ON CONFLICT (claim_line_id) DO NOTHING
    """,
    "claim_status_history": """
        INSERT INTO billing.claim_status_history (
            claim_id, status_code, status_date, status_source, status_note
        ) VALUES %s
    """,
    "claim_submission_batches": """
        INSERT INTO billing.claim_submission_batches (
            batch_id, payer_id, clearinghouse_id, batch_created_at,
            batch_submitted_at, claim_count, total_charge_amount,
            batch_status, rejection_count
        ) VALUES %s ON CONFLICT (batch_id) DO NOTHING
    """,
}

LOAD_ORDER = ["claims", "claim_diagnoses", "claim_lines",
              "claim_status_history", "claim_submission_batches"]

TRUNCATE_ORDER = [
    "billing.claim_status_history", "billing.claim_lines",
    "billing.claim_diagnoses", "billing.claim_submission_batches",
    "billing.claims",
]


def load(tables, truncate):
    from psycopg2.extras import execute_values

    conn = connect("health_claims")
    try:
        with conn, conn.cursor() as cur:
            if truncate:
                cur.execute(f"TRUNCATE {', '.join(TRUNCATE_ORDER)} CASCADE")
                print("Existing claim data removed.")

            for name in LOAD_ORDER:
                rows = tables[name]
                if not rows:
                    continue
                for i in range(0, len(rows), BATCH):
                    execute_values(cur, INSERTS[name], rows[i:i + BATCH],
                                   page_size=BATCH)
                print(f"  {name:26} {len(rows):>10,}")
    finally:
        conn.close()


# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate 837 claims.")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--truncate", action="store_true")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rng = random.Random(SEED)

    if args.dry_run:
        encounters, procedures, diagnoses, coverage = synthetic_reference(rng)
        npis = [f"1{i:09d}" for i in range(1, 2000)]
        payers = ["MCR01", "OHMCD", "ANTOH", "UHC01", "SELFP"]
        print("Dry run: using synthetic reference data.")
    else:
        print("Reading clinical data...")
        encounters, procedures, diagnoses, coverage = fetch_clinical(start, end)
        npis, payers = fetch_billing_reference()
        print(f"Read {len(encounters):,} encounters, "
              f"{len(procedures):,} with procedures, {len(npis):,} NPIs.")

    procedures = apply_charges(rng, procedures)

    print(f"Generating claims for {start} to {end}...")
    tables = build(rng, start, end, encounters, procedures, diagnoses,
                   coverage, npis, payers)

    print()
    total = 0
    for name in LOAD_ORDER:
        print(f"{name:26} {len(tables[name]):>10,}")
        total += len(tables[name])
    print(f"{'TOTAL':26} {total:>10,}")

    claims = tables["claims"]
    replacements = sum(1 for c in claims if c[1] == "7")
    voids = sum(1 for c in claims if c[1] == "8")
    orphan_payers = sum(1 for c in claims if c[14] in UNKNOWN_PAYERS)
    print(f"\nreplacements {replacements:,} | voids {voids:,} | "
          f"unknown payer {orphan_payers:,}")
    print(f"lines per claim: "
          f"{len(tables['claim_lines']) / max(1, len(claims)):.1f}")

    if args.dry_run:
        print("\nSample claim:")
        print(" ", claims[0])
        return

    print("\nLoading:")
    load(tables, args.truncate)


def synthetic_reference(rng):
    """Reference data for a dry run, so nothing has to be read."""
    encounters, procedures, diagnoses, coverage = [], {}, {}, {}
    base = datetime(2025, 10, 1, tzinfo=timezone.utc)

    for i in range(1, 20001):
        enc_id = f"ENC25{i:09d}"
        mrn = f"MRN{rng.randint(1, 5000):08d}"
        etype = rng.choice(["OUTPATIENT"] * 7 + ["EMERGENCY"] * 2
                           + ["INPATIENT"])
        admit = base + timedelta(days=rng.randint(0, 91),
                                 hours=rng.randint(0, 23))
        los = rng.randint(1, 8) if etype == "INPATIENT" else 0
        encounters.append((enc_id, mrn, etype, "RRH-MAIN", admit,
                           admit + timedelta(days=los), los,
                           "03", "01", "01"))
        procedures[enc_id] = [
            (rng.choice(["99213", "80053", "71046", "29881"]), None, 1, 0)
            for _ in range(rng.randint(1, 4))
        ]
        diagnoses[enc_id] = [
            (rng.choice(["I10", "E11.9", "J06.9", "R07.9"]),
             "PRINCIPAL" if j == 0 else "SECONDARY", "Y")
            for j in range(rng.randint(1, 5))
        ]
        coverage.setdefault(mrn, [{
            "payer": rng.choice(["MCR01", "OHMCD", "ANTOH", "UHC01"]),
            "subscriber": f"SUB{rng.randint(10**7, 10**8)}",
            "relationship": rng.choice(["18", "18", "01", "19"]),
            "from": date(2022, 1, 1), "to": None,
        }])

    return encounters, procedures, diagnoses, coverage


if __name__ == "__main__":
    main()