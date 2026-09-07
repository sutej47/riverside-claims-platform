"""
Generate clinical encounters and their children for Riverside Regional Health.

Populates six tables in health_ehr.clinical:
    encounters, encounter_diagnoses, encounter_procedures,
    orders, observations, clinical_notes

Volumes follow the persona: 26,000 inpatient discharges, 412,000 outpatient
encounters and 98,000 ED visits a year, scaled to whatever date range is
asked for.

Deliberate messiness built in here, not injected later:
  - ICD-10 codes retired in 2024 still appear on 2023 encounters, and no
    version column says which vocabulary a code came from (problem A7)
  - observation_value is free text: "140/90", "12.4", "negative" and the
    sentinel values 999 and -1 all live in the same column (problem A6)
  - ~1.5% of encounters are still OPEN with no discharge time, so an
    extract taken today sees rows that will change tomorrow
  - a small share of patients are frequent flyers, which puts real skew
    into any join or group-by on mrn (problem B3)
  - clinical notes carry PHI inline: names, dates and phone numbers sit
    inside free text, not in a maskable column (problem C1)

The run is deterministic for a given date range and seed.

Usage:
    python3 generate_encounters.py --dry-run
    python3 generate_encounters.py --start 2025-10-01 --end 2025-12-31
    python3 generate_encounters.py --start 2023-01-01 --end 2025-12-31 \\
        --with-clinical --truncate
"""

import argparse
import os
import random
from datetime import date, datetime, timedelta, timezone

from faker import Faker

from identifiers import make_encounter_id, make_order_id

SEED = 20250831

# Annual volumes from the persona
ANNUAL_INPATIENT = 26_000
ANNUAL_OUTPATIENT = 412_000
ANNUAL_ED = 98_000

OPEN_ENCOUNTER_RATE = 0.015
CANCELLED_RATE = 0.008

BATCH = 5_000


# ---------------------------------------------------------------------
# Reference vocabularies
# ---------------------------------------------------------------------

# (code, description, retired_from)  retired_from = None means still current.
# Codes with a retirement date still appear on encounters before it, which
# is what makes a temporal reference join necessary rather than optional.
ICD10 = [
    ("I10",     "Essential (primary) hypertension", None),
    ("E11.9",   "Type 2 diabetes mellitus without complications", None),
    ("E78.5",   "Hyperlipidemia, unspecified", None),
    ("J44.9",   "Chronic obstructive pulmonary disease, unspecified", None),
    ("N18.3",   "Chronic kidney disease, stage 3 unspecified",
     date(2024, 10, 1)),                       # split into N18.30/31/32
    ("I25.10",  "Atherosclerotic heart disease of native coronary artery", None),
    ("F32.9",   "Major depressive disorder, single episode, unspecified",
     date(2024, 10, 1)),                       # replaced by F32.A
    ("F41.9",   "Anxiety disorder, unspecified", None),
    ("M54.5",   "Low back pain", date(2023, 10, 1)),   # split into M54.50/51/59
    ("M17.11",  "Unilateral primary osteoarthritis, right knee", None),
    ("J06.9",   "Acute upper respiratory infection, unspecified", None),
    ("J18.9",   "Pneumonia, unspecified organism", None),
    ("U07.1",   "COVID-19", None),
    ("R07.9",   "Chest pain, unspecified", None),
    ("R10.9",   "Unspecified abdominal pain", None),
    ("R51.9",   "Headache, unspecified", None),
    ("Z00.00",  "General adult medical examination without abnormal findings", None),
    ("Z12.31",  "Screening mammogram for malignant neoplasm of breast", None),
    ("N39.0",   "Urinary tract infection, site not specified", None),
    ("K21.9",   "Gastro-esophageal reflux disease without esophagitis", None),
    ("E03.9",   "Hypothyroidism, unspecified", None),
    ("D64.9",   "Anemia, unspecified", None),
    ("I48.91",  "Unspecified atrial fibrillation", None),
    ("I50.32",  "Chronic diastolic (congestive) heart failure", None),
    ("G47.33",  "Obstructive sleep apnea (adult) (pediatric)", None),
    ("M79.604", "Pain in right leg", None),
    ("S52.501A","Unspecified fracture of lower end of right radius, initial", None),
    ("T78.40XA","Allergy, unspecified, initial encounter", None),
    ("O80",     "Encounter for full-term uncomplicated delivery", None),
    ("Z34.90",  "Encounter for supervision of normal pregnancy, unspecified", None),
    ("J45.909", "Unspecified asthma, uncomplicated", None),
    ("L03.115", "Cellulitis of right lower limb", None),
    ("H25.13",  "Age-related nuclear cataract, bilateral", None),
    ("C50.911", "Malignant neoplasm of unspecified site of right breast", None),
    ("F17.210", "Nicotine dependence, cigarettes, uncomplicated", None),
    ("Z79.4",   "Long term (current) use of insulin", None),
    ("R73.03",  "Prediabetes", None),
    ("M25.561", "Pain in right knee", None),
    ("B37.3",   "Candidiasis of vulva and vagina", date(2025, 10, 1)),
    ("R53.83",  "Other fatigue", None),
]

# (code, description, category, typical_charge)
CPT = [
    ("99213", "Office visit, established patient, low complexity", "EM", 128),
    ("99214", "Office visit, established patient, moderate", "EM", 186),
    ("99215", "Office visit, established patient, high complexity", "EM", 252),
    ("99203", "Office visit, new patient, low complexity", "EM", 174),
    ("99204", "Office visit, new patient, moderate", "EM", 266),
    ("99232", "Subsequent hospital care, moderate", "EM", 148),
    ("99233", "Subsequent hospital care, high", "EM", 212),
    ("99283", "Emergency department visit, moderate severity", "ED", 428),
    ("99284", "Emergency department visit, high severity", "ED", 764),
    ("99285", "Emergency department visit, highest severity", "ED", 1142),
    ("80053", "Comprehensive metabolic panel", "LAB", 62),
    ("80061", "Lipid panel", "LAB", 58),
    ("85025", "Complete blood count with differential", "LAB", 44),
    ("83036", "Hemoglobin A1C", "LAB", 51),
    ("84443", "Thyroid stimulating hormone", "LAB", 68),
    ("81001", "Urinalysis with microscopy", "LAB", 32),
    ("87804", "Influenza assay", "LAB", 41),
    ("86803", "Hepatitis C antibody", "LAB", 74),
    ("71046", "Chest X-ray, 2 views", "RAD", 185),
    ("72148", "MRI lumbar spine without contrast", "RAD", 1284),
    ("74177", "CT abdomen and pelvis with contrast", "RAD", 1642),
    ("70450", "CT head without contrast", "RAD", 892),
    ("77067", "Screening mammography, bilateral", "RAD", 246),
    ("93000", "Electrocardiogram, complete", "RAD", 78),
    ("76700", "Ultrasound, abdominal, complete", "RAD", 412),
    ("29881", "Arthroscopy, knee, with meniscectomy", "SURG", 4820),
    ("47562", "Laparoscopic cholecystectomy", "SURG", 8940),
    ("66984", "Cataract extraction with lens insertion", "SURG", 3160),
    ("27447", "Total knee arthroplasty", "SURG", 21400),
    ("45378", "Diagnostic colonoscopy", "SURG", 1284),
    ("97110", "Therapeutic exercise, 15 minutes", "PT", 64),
    ("97140", "Manual therapy, 15 minutes", "PT", 58),
    ("97530", "Therapeutic activities, 15 minutes", "PT", 68),
    ("90834", "Psychotherapy, 45 minutes", "BH", 142),
    ("90837", "Psychotherapy, 60 minutes", "BH", 186),
    ("90792", "Psychiatric diagnostic evaluation with medical", "BH", 284),
    ("00790", "Anesthesia for upper abdomen procedures", "ANES", 1120),
    ("00840", "Anesthesia for lower abdomen procedures", "ANES", 980),
    ("E0601", "Continuous positive airway pressure device", "DME", 1240),
    ("K0001", "Standard wheelchair", "DME", 720),
]

CPT_MODIFIERS = ["25", "59", "76", "LT", "RT", "50", "GP", "TC", "26"]

# (loinc, name, unit, generator kind)
OBSERVATIONS = [
    ("8480-6",  "Systolic blood pressure", "mmHg", "bp"),
    ("8867-4",  "Heart rate", "bpm", "int:48:118"),
    ("9279-1",  "Respiratory rate", "/min", "int:10:26"),
    ("8310-5",  "Body temperature", "degF", "dec:96.4:101.8"),
    ("2708-6",  "Oxygen saturation", "%", "int:88:100"),
    ("29463-7", "Body weight", "kg", "dec:41.0:158.0"),
    ("8302-2",  "Body height", "cm", "dec:142.0:198.0"),
    ("39156-5", "Body mass index", "kg/m2", "dec:16.0:52.0"),
    ("2345-7",  "Glucose", "mg/dL", "int:58:412"),
    ("718-7",   "Hemoglobin", "g/dL", "dec:7.2:17.4"),
    ("4544-3",  "Hematocrit", "%", "dec:22.0:52.0"),
    ("6690-2",  "Leukocytes", "10*3/uL", "dec:2.1:22.4"),
    ("2160-0",  "Creatinine", "mg/dL", "dec:0.4:6.8"),
    ("3094-0",  "Urea nitrogen", "mg/dL", "int:5:82"),
    ("2951-2",  "Sodium", "mmol/L", "int:128:151"),
    ("2823-3",  "Potassium", "mmol/L", "dec:2.8:6.4"),
    ("4548-4",  "Hemoglobin A1c", "%", "dec:4.6:14.2"),
    ("2093-3",  "Cholesterol total", "mg/dL", "int:112:346"),
    ("14682-9", "Creatinine serum", "mg/dL", "dec:0.4:6.8"),
    ("33914-3", "Estimated GFR", "mL/min", "int:8:120"),
    ("5195-3",  "Hepatitis B surface antigen", None, "qual"),
    ("31208-2", "Bacteria culture", None, "qual"),
]

# Sentinel values a real EHR writes when a value is unknown. These are the
# values that quietly poison an average if nobody catches them.
SENTINELS = ["999", "-1", "9999", "UNKNOWN", "", "N/A"]
SENTINEL_RATE = 0.018

ORDER_TYPES = [
    ("LAB", 0.44), ("IMAGING", 0.17), ("MEDICATION", 0.24),
    ("NURSING", 0.08), ("CONSULT", 0.04), ("PROCEDURE", 0.02),
    ("DIET", 0.01),
]

NOTE_TYPES_BY_ENCOUNTER = {
    "INPATIENT": ["ADMISSION", "PROGRESS", "PROGRESS", "DISCHARGE_SUMMARY"],
    "EMERGENCY": ["PROGRESS", "NURSING"],
    "OUTPATIENT": ["PROGRESS"],
    "OFFICE": ["PROGRESS"],
    "TELEHEALTH": ["PROGRESS"],
    "LAB": [],
    "IMAGING": ["RADIOLOGY"],
}

NOTE_TEMPLATES = [
    "Patient {first} {last} (DOB {dob}) seen today for {complaint}. "
    "Reports symptoms ongoing for {days} days. Contact number on file "
    "is {phone}. Plan: {plan}.",
    "{first} {last} presents with {complaint}. History reviewed with "
    "patient and spouse. Discussed findings; follow-up arranged at the "
    "{clinic} clinic. Callback number {phone}.",
    "Admission note for {first} {last}, DOB {dob}. Chief complaint "
    "{complaint}. Admitted from home. Emergency contact reachable on "
    "{phone}. Plan: {plan}.",
]

COMPLAINTS = [
    "chest discomfort", "shortness of breath", "abdominal pain",
    "worsening cough", "lower back pain", "headache", "dizziness",
    "elevated blood sugar", "routine follow-up", "medication review",
    "knee pain", "fatigue", "rash", "fever",
]

PLANS = [
    "continue current medications and review in 6 weeks",
    "start therapy and reassess in one month",
    "obtain labs and follow up on results",
    "refer to cardiology",
    "physical therapy twice weekly for 6 weeks",
    "discharge home with primary care follow-up",
]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def weighted_pick(rng, options):
    """Pick one option from [(value, weight, ...), ...] by weight."""
    total = sum(o[1] for o in options)
    r = rng.random() * total
    upto = 0.0
    for option in options:
        upto += option[1]
        if r <= upto:
            return option
    return options[-1]


def seasonal_weight(day, encounter_type):
    """
    Return a multiplier for how busy a given day is.

    Flu season lifts ED and respiratory volume; Q4 brings an elective
    surgery surge; clinics are quiet at weekends and emergency is not.
    """
    weight = 1.0

    if day.month in (12, 1, 2):
        weight *= 1.25 if encounter_type == "EMERGENCY" else 1.10

    if day.month in (10, 11, 12) and encounter_type in ("INPATIENT",
                                                        "OUTPATIENT"):
        weight *= 1.15                      # deductible met, book it now

    if day.weekday() >= 5:                  # Saturday, Sunday
        weight *= 1.0 if encounter_type == "EMERGENCY" else 0.18

    return weight


def code_is_valid_on(retired_from, service_day):
    """True if a code was still in the vocabulary on that date."""
    return retired_from is None or service_day < retired_from


def observation_value(rng, kind):
    """Return a free-text observation value, sentinels included."""
    if rng.random() < SENTINEL_RATE:
        return rng.choice(SENTINELS)

    if kind == "bp":
        return f"{rng.randint(88, 198)}/{rng.randint(48, 118)}"
    if kind == "qual":
        return rng.choice(["negative", "positive", "NEGATIVE",
                           "no growth", "Reactive"])
    prefix, low, high = kind.split(":")
    low, high = float(low), float(high)
    if prefix == "int":
        return str(rng.randint(int(low), int(high)))
    return f"{rng.uniform(low, high):.1f}"


def abnormal_flag(rng):
    r = rng.random()
    if r < 0.72:
        return "N"
    if r < 0.86:
        return "H"
    if r < 0.96:
        return "L"
    return rng.choice(["HH", "LL"])


# ---------------------------------------------------------------------
# Encounter generation
# ---------------------------------------------------------------------

def daily_targets(start, end):
    """Yield (day, encounter_type, count) across the whole range."""
    days = (end - start).days + 1
    per_day = {
        "INPATIENT": ANNUAL_INPATIENT / 365.0,
        "OUTPATIENT": ANNUAL_OUTPATIENT / 365.0,
        "EMERGENCY": ANNUAL_ED / 365.0,
    }
    for offset in range(days):
        day = start + timedelta(days=offset)
        for etype, base in per_day.items():
            yield day, etype, base * seasonal_weight(day, etype)


def pick_patient(rng, patients, frequent):
    """
    Choose a patient for an encounter.

    A fifth of encounters go to frequent flyers, which is what real
    healthcare utilisation looks like and what puts skew into any
    group-by on mrn later.
    """
    if frequent and rng.random() < 0.20:
        return rng.choice(frequent)
    return rng.choice(patients)


def build(rng, fake, start, end, patients, providers, departments,
          with_clinical):
    """Build every encounter table. Returns a dict of table name -> rows."""

    # The top 4% of patients account for a fifth of all encounters
    frequent = rng.sample(patients, max(1, len(patients) * 4 // 100))

    provider_ids = [p for p in providers]
    dept_by_type = {}
    for dept_id, dept_type, facility in departments:
        dept_by_type.setdefault(dept_type, []).append((dept_id, facility))

    def department_for(etype):
        preferred = {
            "INPATIENT": ["INPATIENT_UNIT", "SURGERY"],
            "EMERGENCY": ["EMERGENCY"],
            "OUTPATIENT": ["CLINIC", "LAB", "IMAGING"],
        }[etype]
        for want in rng.sample(preferred, len(preferred)):
            if want in dept_by_type:
                return rng.choice(dept_by_type[want])
        return rng.choice(next(iter(dept_by_type.values())))

    encounters, diagnoses, procedures = [], [], []
    orders, observations, notes = [], [], []

    enc_seq = 0
    order_seq = 0

    for day, etype, target in daily_targets(start, end):
        # Poisson-ish jitter so no two days are identical
        count = max(0, int(rng.gauss(target, target ** 0.5)))

        for _ in range(count):
            enc_seq += 1
            mrn = pick_patient(rng, patients, frequent)
            dept_id, facility = department_for(etype)
            encounter_id = make_encounter_id(enc_seq, day.year)

            # --- timing -----------------------------------------------
            admit = datetime(day.year, day.month, day.day,
                             rng.randint(0, 23), rng.choice([0, 15, 30, 45]),
                             tzinfo=timezone.utc)

            if etype == "INPATIENT":
                los = max(1, int(rng.gauss(4.2, 3.1)))
            elif etype == "EMERGENCY":
                los = 0
            else:
                los = 0

            status = "CLOSED"
            discharge = admit + timedelta(
                days=los, hours=rng.randint(1, 9) if los == 0 else 0)

            if rng.random() < CANCELLED_RATE:
                status, discharge, los = "CANCELLED", None, None
            elif rng.random() < OPEN_ENCOUNTER_RATE:
                # Still in-flight. Will change after extraction.
                status, discharge, los = "OPEN", None, None

            encounters.append((
                encounter_id, mrn, etype, facility, dept_id,
                rng.choice(provider_ids),
                rng.choice(provider_ids) if rng.random() < 0.42 else None,
                admit, discharge, los,
                {"EMERGENCY": "01", "INPATIENT": rng.choice(["01", "02", "03"]),
                 "OUTPATIENT": "03"}[etype],
                rng.choice(["01", "02", "07", "09"]),
                rng.choice(["01", "01", "01", "03", "06", "20"])
                if status == "CLOSED" else None,
                status,
            ))

            if status == "CANCELLED":
                continue

            # Child records need a time window even when the encounter is
            # still open and length_of_stay_days is NULL.
            span_hours = max(1, (los or 1) * 24)

            # --- diagnoses --------------------------------------------
            n_dx = {"INPATIENT": rng.randint(4, 14),
                    "EMERGENCY": rng.randint(2, 6),
                    "OUTPATIENT": rng.randint(1, 4)}[etype]

            valid_codes = [c for c in ICD10 if code_is_valid_on(c[2], day)]
            # 3% of the time a retired code is used anyway, because the
            # clinician's favourites list was never updated.
            pool = ICD10 if rng.random() < 0.03 else valid_codes
            chosen = rng.sample(pool, min(n_dx, len(pool)))

            for i, (code, description, _retired) in enumerate(chosen, 1):
                diagnoses.append((
                    encounter_id, i, code, description,
                    rng.choice(["Y", "Y", "N", "U", "W"])
                    if etype == "INPATIENT" else None,
                    "PRINCIPAL" if i == 1 else
                    ("ADMITTING" if i == 2 and etype == "INPATIENT"
                     else "SECONDARY"),
                ))

            # --- procedures -------------------------------------------
            n_proc = {"INPATIENT": rng.randint(1, 7),
                      "EMERGENCY": rng.randint(1, 4),
                      "OUTPATIENT": rng.randint(1, 3)}[etype]

            for i in range(1, n_proc + 1):
                code, description, _cat, _charge = rng.choice(CPT)
                mods = ",".join(rng.sample(CPT_MODIFIERS,
                                           rng.randint(1, 2))) \
                    if rng.random() < 0.22 else None
                procedures.append((
                    encounter_id, i, code, description, mods,
                    admit + timedelta(hours=rng.randint(0, span_hours)),
                    rng.choice(provider_ids),
                    rng.randint(1, 3) if rng.random() < 0.12 else 1,
                ))

            # --- orders -----------------------------------------------
            n_orders = {"INPATIENT": rng.randint(6, 22),
                        "EMERGENCY": rng.randint(3, 11),
                        "OUTPATIENT": rng.randint(1, 6)}[etype]

            for _ in range(n_orders):
                order_seq += 1
                otype, _w = weighted_pick(rng, ORDER_TYPES)
                ordered = admit + timedelta(
                    hours=rng.randint(0, span_hours))
                # An order placed but never completed stays open forever
                completed = None if rng.random() < 0.06 else \
                    ordered + timedelta(minutes=rng.randint(20, 900))
                orders.append((
                    make_order_id(order_seq, day.year), encounter_id, mrn,
                    otype,
                    rng.choice(CPT)[0] if otype in ("LAB", "IMAGING")
                    else f"{otype[:3]}{rng.randint(100, 999)}",
                    fake.sentence(nb_words=5)[:200],
                    rng.choice(provider_ids), ordered, completed,
                    "COMPLETED" if completed else
                    rng.choice(["ORDERED", "IN_PROGRESS", "CANCELLED"]),
                    rng.choice(["ROUTINE", "ROUTINE", "ROUTINE",
                                "URGENT", "STAT"]),
                ))

            if not with_clinical:
                continue

            # --- observations -----------------------------------------
            n_obs = {"INPATIENT": rng.randint(18, 60),
                     "EMERGENCY": rng.randint(6, 20),
                     "OUTPATIENT": rng.randint(2, 9)}[etype]

            for _ in range(n_obs):
                loinc, name, unit, kind = rng.choice(OBSERVATIONS)
                observations.append((
                    encounter_id, mrn, loinc, name,
                    observation_value(rng, kind), unit,
                    None if unit is None else
                    rng.choice(["70-100", "12-16", "3.5-5.0", "90-120"]),
                    abnormal_flag(rng),
                    admit + timedelta(hours=rng.randint(0, span_hours)),
                    rng.choice(provider_ids),
                ))

            # --- notes ------------------------------------------------
            for note_type in NOTE_TYPES_BY_ENCOUNTER[etype]:
                template = rng.choice(NOTE_TEMPLATES)
                notes.append((
                    encounter_id, mrn, note_type,
                    template.format(
                        first=fake.first_name(), last=fake.last_name(),
                        dob=fake.date_of_birth(minimum_age=18,
                                               maximum_age=90)
                            .strftime("%m/%d/%Y"),
                        complaint=rng.choice(COMPLAINTS),
                        days=rng.randint(2, 21),
                        phone=fake.phone_number()[:20],
                        clinic=rng.choice(["Cardiology", "Family Medicine",
                                           "Orthopaedics", "Neurology"]),
                        plan=rng.choice(PLANS),
                    ),
                    rng.choice(provider_ids),
                    admit + timedelta(hours=rng.randint(1, 48)),
                    rng.random() > 0.04,        # 4% unsigned, still editable
                ))

    return {
        "encounters": encounters,
        "encounter_diagnoses": diagnoses,
        "encounter_procedures": procedures,
        "orders": orders,
        "observations": observations,
        "clinical_notes": notes,
    }


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def connect(dbname="health_ehr"):
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5433"),
        dbname=dbname,
        user=os.getenv("PGUSER", "dp_admin"),
        password=os.getenv("PGPASSWORD", "admin"),
    )


def fetch_reference():
    """Read the master data the encounters have to point at."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mrn FROM clinical.patients "
                        "WHERE merged_into_mrn IS NULL")
            patients = [r[0] for r in cur.fetchall()]

            cur.execute("SELECT provider_id FROM clinical.providers")
            providers = [r[0] for r in cur.fetchall()]

            cur.execute("SELECT department_id, department_type, facility_code "
                        "FROM clinical.departments")
            departments = cur.fetchall()
        return patients, providers, departments
    finally:
        conn.close()


INSERTS = {
    "encounters": """
        INSERT INTO clinical.encounters (
            encounter_id, mrn, encounter_type, facility_code, department_id,
            attending_provider_id, referring_provider_id, admission_datetime,
            discharge_datetime, length_of_stay_days, admission_type_code,
            admission_source_code, discharge_status_code, encounter_status
        ) VALUES %s ON CONFLICT (encounter_id) DO NOTHING
    """,
    "encounter_diagnoses": """
        INSERT INTO clinical.encounter_diagnoses (
            encounter_id, diagnosis_sequence, icd10_code, icd10_description,
            poa_indicator, diagnosis_type
        ) VALUES %s
    """,
    "encounter_procedures": """
        INSERT INTO clinical.encounter_procedures (
            encounter_id, procedure_sequence, procedure_code,
            procedure_description, modifiers, performed_datetime,
            performing_provider_id, quantity
        ) VALUES %s
    """,
    "orders": """
        INSERT INTO clinical.orders (
            order_id, encounter_id, mrn, order_type, order_code,
            order_description, ordering_provider_id, ordered_datetime,
            completed_datetime, order_status, priority_code
        ) VALUES %s ON CONFLICT (order_id) DO NOTHING
    """,
    "observations": """
        INSERT INTO clinical.observations (
            encounter_id, mrn, observation_code, observation_name,
            observation_value, unit_of_measure, reference_range,
            abnormal_flag, observed_datetime, recorded_by_provider_id
        ) VALUES %s
    """,
    "clinical_notes": """
        INSERT INTO clinical.clinical_notes (
            encounter_id, mrn, note_type, note_text,
            authored_by_provider_id, authored_datetime, is_signed
        ) VALUES %s
    """,
}

LOAD_ORDER = ["encounters", "encounter_diagnoses", "encounter_procedures",
              "orders", "observations", "clinical_notes"]

TRUNCATE_ORDER = [
    "clinical.clinical_notes", "clinical.observations", "clinical.orders",
    "clinical.encounter_procedures", "clinical.encounter_diagnoses",
    "clinical.encounters",
]


def load(tables, truncate):
    from psycopg2.extras import execute_values

    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            if truncate:
                cur.execute(f"TRUNCATE {', '.join(TRUNCATE_ORDER)} CASCADE")
                print("Existing encounter data removed.")

            for name in LOAD_ORDER:
                rows = tables[name]
                if not rows:
                    continue
                for start in range(0, len(rows), BATCH):
                    execute_values(cur, INSERTS[name],
                                   rows[start:start + BATCH], page_size=BATCH)
                print(f"  {name:24} {len(rows):>10,}")
    finally:
        conn.close()


# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate EHR encounters.")
    parser.add_argument("--start", default="2025-10-01",
                        help="first service date, YYYY-MM-DD")
    parser.add_argument("--end", default="2025-12-31",
                        help="last service date, YYYY-MM-DD")
    parser.add_argument("--with-clinical", action="store_true",
                        help="also generate observations and clinical notes "
                             "(several times more rows, much slower)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print counts and samples, write nothing")
    parser.add_argument("--truncate", action="store_true",
                        help="empty the encounter tables before inserting")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        parser.error("--end must not be before --start")

    rng = random.Random(SEED)
    fake = Faker("en_US")
    Faker.seed(SEED)

    if args.dry_run:
        # Enough fake reference data to exercise the generator
        patients = [f"MRN{i:08d}" for i in range(1, 20001)]
        providers = [f"PRV{i:07d}" for i in range(1, 501)]
        departments = [(f"DEP{i:05d}",
                        ["INPATIENT_UNIT", "CLINIC", "EMERGENCY",
                         "SURGERY", "LAB", "IMAGING"][i % 6],
                        f"RRH-{i % 4}") for i in range(1, 33)]
        print("Dry run: using synthetic reference data.")
    else:
        patients, providers, departments = fetch_reference()
        print(f"Read {len(patients):,} patients, {len(providers):,} providers, "
              f"{len(departments)} departments.")

    print(f"Generating encounters for {start} to {end} "
          f"({(end - start).days + 1} days)...")

    tables = build(rng, fake, start, end, patients, providers, departments,
                   args.with_clinical)

    print()
    total = 0
    for name in LOAD_ORDER:
        print(f"{name:24} {len(tables[name]):>10,}")
        total += len(tables[name])
    print(f"{'TOTAL':24} {total:>10,}")

    if not args.with_clinical:
        print("\nobservations and clinical_notes skipped "
              "(pass --with-clinical to generate them)")

    encounters = tables["encounters"]
    open_count = sum(1 for e in encounters if e[13] == "OPEN")
    print(f"\nopen encounters: {open_count:,} "
          f"({open_count / max(1, len(encounters)):.1%})")

    if args.dry_run:
        print("\nSample encounter:")
        print(" ", encounters[0])
        return

    print("\nLoading:")
    load(tables, args.truncate)


if __name__ == "__main__":
    main()