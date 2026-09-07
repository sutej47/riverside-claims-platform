"""
Generate the EHR master data for Riverside Regional Health.

Populates six tables in health_ehr.clinical:
    departments, providers, patients, patient_addresses,
    patient_identifiers, patient_coverage

Deliberate gaps built in here, not injected later:
  - only ~60% of patients get an ACCOUNT_NUMBER identifier, so the direct
    link to billing covers most patients but not all
  - ~3% of patients are duplicates of another patient, with a misspelt
    name or a transposed date of birth; most are unmerged
  - ~8% of patients have no coverage row at all
  - ~2% of patients have more than one address flagged is_current
  - ~5% of billing providers have no matching EHR provider

The run is deterministic: the same seed produces the same data, so the
database can be rebuilt identically after a reset.

Usage:
    python3 generate_ehr_master.py --dry-run
    python3 generate_ehr_master.py --patients 20000 --truncate
    python3 generate_ehr_master.py --truncate
"""

import argparse
import os
import random
from datetime import date, timedelta

from faker import Faker

from identifiers import make_account_number, make_mrn

SEED = 20250831
DEFAULT_PATIENTS = 285_000

FACILITIES = [
    ("RRH-MAIN", "Riverside Regional Medical Center"),
    ("RRH-NORTH", "Riverside North Hospital"),
    ("RRH-EAST", "Riverside East Hospital"),
    ("RRH-WEST", "Riverside West Hospital"),
]

# (department_name, department_type, bed_count or None)
DEPARTMENT_TEMPLATES = [
    ("Emergency Department", "EMERGENCY", 40),
    ("Medical/Surgical Unit", "INPATIENT_UNIT", 60),
    ("Intensive Care Unit", "INPATIENT_UNIT", 20),
    ("Operating Theatres", "SURGERY", None),
    ("Clinical Laboratory", "LAB", None),
    ("Diagnostic Imaging", "IMAGING", None),
    ("Cardiology Clinic", "CLINIC", None),
    ("Family Medicine Clinic", "CLINIC", None),
]

# Ohio counties by population share, from the persona
COUNTIES = [
    ("Franklin", 0.42, "Columbus", ["43201", "43202", "43204", "43206",
                                    "43209", "43214", "43215", "43219",
                                    "43220", "43229", "43231", "43235"]),
    ("Licking", 0.13, "Newark", ["43055", "43056", "43023", "43062"]),
    ("Fairfield", 0.11, "Lancaster", ["43130", "43136", "43102", "43112"]),
    ("Delaware", 0.09, "Delaware", ["43015", "43035", "43065", "43074"]),
    ("Pickaway", 0.06, "Circleville", ["43113", "43146", "43116"]),
    ("Perry", 0.04, "New Lexington", ["43764", "43730", "43782"]),
    ("Muskingum", 0.04, "Zanesville", ["43701", "43702", "43777"]),
    ("Hocking", 0.03, "Logan", ["43138", "43152", "43149"]),
    ("Madison", 0.03, "London", ["43140", "43162", "43151"]),
    ("Union", 0.03, "Marysville", ["43040", "43064", "43045"]),
    ("Morrow", 0.02, "Mount Gilead", ["43338", "43334", "43315"]),
]

# (payer_id, share, plan_name)
PAYER_MIX = [
    ("MCR01", 0.31, "Medicare Part A/B"),
    ("OHMCD", 0.17, "Ohio Medicaid Managed Care"),
    ("ANTOH", 0.15, "Anthem PPO Blue"),
    ("UHC01", 0.11, "UnitedHealthcare Choice Plus"),
    ("MAPLN", 0.09, "Medicare Advantage HMO"),
    ("AETBH", 0.07, "Aetna Better Health"),
    ("CIG01", 0.04, "Cigna Open Access Plus"),
    ("SELFP", 0.04, None),
    ("OHWCP", 0.02, "Ohio BWC"),
]

# How the payer name gets typed at registration. The EHR stores what the
# clerk entered, not a coded value.
PAYER_NAME_VARIANTS = {
    "MCR01": ["Medicare", "MEDICARE PART B", "Medicare A/B", "medicare"],
    "OHMCD": ["Ohio Medicaid", "OH MEDICAID", "Medicaid", "ODM"],
    "ANTOH": ["Anthem BCBS", "Anthem Blue Cross", "ANTHEM", "BCBS Ohio"],
    "UHC01": ["UnitedHealthcare", "UHC", "United Health Care", "UNITED"],
    "MAPLN": ["Medicare Advantage", "MA Plan", "Medicare Adv"],
    "AETBH": ["Aetna", "Aetna Better Health", "AETNA BH"],
    "CIG01": ["Cigna", "CIGNA HEALTHCARE", "Cigna Health"],
    "SELFP": ["Self Pay", "SELF-PAY", "Uninsured", "NONE"],
    "OHWCP": ["BWC", "Workers Comp", "Ohio BWC", "WORKERS COMPENSATION"],
}

CREDENTIALS = ["MD", "MD", "MD", "DO", "NP", "PA", "RN"]
LANGUAGES = ["English"] * 88 + ["Spanish"] * 6 + ["Somali"] * 3 + \
            ["Nepali"] * 2 + ["Arabic"]
MARITAL_CODES = ["S", "M", "M", "D", "W", "U"]

DUPLICATE_RATE = 0.03
ACCOUNT_LINK_RATE = 0.60
NO_COVERAGE_RATE = 0.08
DOUBLE_CURRENT_ADDRESS_RATE = 0.02
PROVIDER_MISSING_RATE = 0.05

BATCH = 5_000


# ---------------------------------------------------------------------
# Weighted choice
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


# ---------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------

def build_departments():
    """One row per facility/department combination."""
    rows = []
    for f_index, (facility_code, facility_name) in enumerate(FACILITIES, 1):
        for d_index, (name, dtype, beds) in enumerate(DEPARTMENT_TEMPLATES, 1):
            rows.append((
                f"DEP{f_index:02d}{d_index:03d}",
                name,
                facility_code,
                facility_name,
                dtype,
                f"CC{f_index}{d_index:03d}",     # finance's own key
                beds,
                True,
            ))
    return rows


# ---------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------

def misspell(rng, name):
    """Return a plausibly mistyped version of a name."""
    if len(name) < 4:
        return name
    mode = rng.randint(0, 2)
    i = rng.randint(1, len(name) - 2)
    if mode == 0:                                   # transpose
        return name[:i] + name[i + 1] + name[i] + name[i + 2:]
    if mode == 1:                                   # drop a letter
        return name[:i] + name[i + 1:]
    return name[:i] + name[i] + name[i:]            # double a letter


def build_providers(rng, billing_providers, departments):
    """
    Build the EHR provider list from the billing provider list.

    The two systems describe the same clinicians and disagree: some
    providers are missing here, some names are spelt differently, and
    the active flags drift.
    """
    dept_ids = [d[0] for d in departments]
    rows = []

    for seq, (npi, last, first, org_name, ptype, _tax, specialty,
              _tid, _fac, _emp, enrolled, terminated, active) in \
            enumerate(billing_providers, 1):

        if rng.random() < PROVIDER_MISSING_RATE:
            continue                                # never made it into the EHR

        # 4% of EHR rows have no NPI recorded even though billing has one
        ehr_npi = None if rng.random() < 0.04 else npi

        if ptype == "ORGANISATION":
            ehr_last, ehr_first, creds = org_name, None, None
        else:
            ehr_last = misspell(rng, last) if rng.random() < 0.07 else last
            ehr_first = first
            creds = rng.choice(CREDENTIALS)

        # The EHR is slower to record departures than billing is
        ehr_active = active
        if not active and rng.random() < 0.30:
            ehr_active = True

        rows.append((
            f"PRV{seq:07d}",
            ehr_npi,
            ehr_last,
            ehr_first,
            creds,
            rng.choice(dept_ids),
            specialty,                              # free text here
            enrolled,
            terminated if not ehr_active else None,
            ehr_active,
        ))

    return rows


# ---------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------

def transpose_dob(rng, dob):
    """Return a date of birth with two digits swapped, if that is valid."""
    if dob is None:
        return None
    try:
        if dob.day <= 12 and dob.month != dob.day:
            return date(dob.year, dob.day, dob.month)   # month/day swapped
        return date(dob.year, dob.month, max(1, dob.day - 10))
    except ValueError:
        return dob


def build_patients(rng, fake, n_patients):
    """
    Build patients plus their addresses, identifiers and coverage.

    Returns (patients, addresses, identifiers, coverage).
    """
    patients = []
    addresses = []
    identifiers = []
    coverage = []

    # Seq numbers already used, so duplicates can point at a real patient
    created = []

    seq = 0
    while len(patients) < n_patients:
        seq += 1
        mrn = make_mrn(seq)

        is_duplicate = created and rng.random() < DUPLICATE_RATE

        if is_duplicate:
            # Same human, registered again: name misspelt or DOB mistyped
            source = rng.choice(created[-5000:])
            last = misspell(rng, source["last"]) if rng.random() < 0.6 \
                else source["last"]
            first = source["first"]
            dob = source["dob"] if rng.random() < 0.6 \
                else transpose_dob(rng, source["dob"])
            gender = source["gender"]
            county, city, zip_code = source["county"], source["city"], \
                source["zip"]
            # Most duplicates are never found; a third get merged
            merged_into = source["mrn"] if rng.random() < 0.33 else None
        else:
            last = fake.last_name()
            first = fake.first_name()
            dob = fake.date_of_birth(minimum_age=0, maximum_age=97)
            gender = rng.choice("MF") if rng.random() > 0.005 else "U"
            county, _share, city, zips = weighted_pick(rng, COUNTIES)
            zip_code = rng.choice(zips)
            merged_into = None

        registration = fake.date_between(start_date=date(2010, 1, 1),
                                         end_date=date(2025, 6, 30))

        patients.append((
            mrn,
            last,
            first,
            rng.choice("ABCDEFGHJKLMNPRSTW") if rng.random() < 0.45 else None,
            rng.choice(["JR", "Jr.", "Jr", "III", "SR"])
            if rng.random() < 0.03 else None,
            fake.last_name() if gender == "F" and rng.random() < 0.18 else None,
            dob,
            gender,
            fake.phone_number()[:20] if rng.random() < 0.82 else None,
            fake.phone_number()[:20] if rng.random() < 0.71 else None,
            fake.email() if rng.random() < 0.64 else None,
            f"{rng.randint(0, 9999):04d}" if rng.random() < 0.88 else None,
            rng.choice(LANGUAGES),
            rng.choice(MARITAL_CODES),
            merged_into,
            registration,
            # 1.2% deceased, weighted to older patients
            fake.date_between(start_date=date(2023, 1, 1),
                              end_date=date(2025, 12, 31))
            if rng.random() < 0.012 else None,
            merged_into is None,                    # merged rows go inactive
        ))

        created.append({"mrn": mrn, "last": last, "first": first,
                        "dob": dob, "gender": gender, "county": county,
                        "city": city, "zip": zip_code})

        # --- addresses ------------------------------------------------
        n_addresses = 1 if rng.random() < 0.72 else rng.randint(2, 3)
        eff = registration
        for a in range(n_addresses):
            is_last = (a == n_addresses - 1)
            eff_to = None if is_last else eff + timedelta(
                days=rng.randint(200, 1400))
            addresses.append((
                mrn,
                "HOME",
                fake.street_address()[:100],
                f"Apt {rng.randint(1, 400)}" if rng.random() < 0.22 else None,
                city,
                "OH",
                # ZIP+4 sometimes, plain 5 usually
                f"{zip_code}-{rng.randint(1000, 9999)}"
                if rng.random() < 0.14 else zip_code,
                county,
                eff,
                eff_to,
                # The is_current flag is maintained by the EHR and is
                # sometimes wrong: an old row left flagged current.
                is_last or rng.random() < DOUBLE_CURRENT_ADDRESS_RATE,
            ))
            if eff_to:
                eff = eff_to

        # --- identifiers ----------------------------------------------
        # The billing account number exists for every patient, but the
        # interface that copies it into the EHR drops rows silently.
        if rng.random() < ACCOUNT_LINK_RATE:
            identifiers.append((
                mrn, "ACCOUNT_NUMBER", make_account_number(seq),
                "BILLING_INTERFACE", registration, True,
            ))
        if merged_into:
            identifiers.append((
                mrn, "MRN_RETIRED", mrn, "EHR", registration, False,
            ))
        if rng.random() < 0.09:
            identifiers.append((
                mrn, "MRN_LEGACY", f"L{rng.randint(100000, 999999)}",
                rng.choice(["STMARY_LEGACY", "COUNTY_CLINIC", "RRH_EAST_OLD"]),
                registration, False,
            ))

        # --- coverage -------------------------------------------------
        if rng.random() < NO_COVERAGE_RATE:
            continue                                # registered, never insured

        payer_id, _share, plan = weighted_pick(rng, PAYER_MIX)

        # Who holds the policy. Children and spouses carry someone
        # else's member id, which is what breaks a naive join.
        age = (date(2025, 12, 31) - dob).days // 365 if dob else 40
        if age < 19:
            relationship = "19"
        elif rng.random() < 0.14:
            relationship = "01"
        else:
            relationship = "18"

        if relationship == "18":
            sub_last = sub_first = None
            sub_dob = None
        else:
            sub_last = last
            sub_first = fake.first_name()
            sub_dob = fake.date_of_birth(minimum_age=25, maximum_age=70)

        # Coverage is effective dated and renews. A member id issued in
        # 2023 may point at different coverage in 2025.
        n_spells = 1 if rng.random() < 0.68 else rng.randint(2, 3)
        cov_from = max(registration, date(2022, 1, 1))
        for s in range(n_spells):
            is_last = (s == n_spells - 1)
            cov_to = None if is_last else cov_from + timedelta(
                days=rng.randint(365, 900))
            coverage.append((
                mrn,
                payer_id,
                rng.choice(PAYER_NAME_VARIANTS[payer_id]),
                # Member id is reissued at each renewal
                None if payer_id == "SELFP"
                else f"{payer_id[:3]}{rng.randint(10**8, 10**9 - 1)}",
                relationship,
                sub_last,
                sub_first,
                sub_dob,
                f"GRP{rng.randint(1000, 9999)}" if payer_id != "SELFP" else None,
                plan,
                1,
                cov_from,
                cov_to,
                is_last,
            ))
            if cov_to:
                cov_from = cov_to

    return patients, addresses, identifiers, coverage


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


def fetch_billing_providers():
    """Read the provider list out of the billing database."""
    conn = connect("health_claims")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT provider_npi, provider_last_name, provider_first_name,
                       organisation_name, provider_type, taxonomy_code,
                       specialty_description, tax_id, facility_code,
                       is_employed, enrolment_date, termination_date, is_active
                FROM billing.billing_providers
                ORDER BY provider_npi
            """)
            return cur.fetchall()
    finally:
        conn.close()


INSERTS = {
    "departments": """
        INSERT INTO clinical.departments (
            department_id, department_name, facility_code, facility_name,
            department_type, cost_centre_code, bed_count, is_active
        ) VALUES %s ON CONFLICT (department_id) DO NOTHING
    """,
    "providers": """
        INSERT INTO clinical.providers (
            provider_id, npi, last_name, first_name, credentials,
            primary_department_id, specialty, hire_date, departure_date,
            is_active
        ) VALUES %s ON CONFLICT (provider_id) DO NOTHING
    """,
    "patients": """
        INSERT INTO clinical.patients (
            mrn, last_name, first_name, middle_initial, name_suffix,
            previous_last_name, date_of_birth, gender_code, phone_home,
            phone_mobile, email, ssn_last_four, preferred_language,
            marital_status_code, merged_into_mrn, registration_date,
            deceased_date, is_active
        ) VALUES %s ON CONFLICT (mrn) DO NOTHING
    """,
    "patient_addresses": """
        INSERT INTO clinical.patient_addresses (
            mrn, address_type, address_line_1, address_line_2, city,
            state_code, zip_code, county, effective_from, effective_to,
            is_current
        ) VALUES %s
    """,
    "patient_identifiers": """
        INSERT INTO clinical.patient_identifiers (
            mrn, identifier_type, identifier_value, issuing_system,
            assigned_date, is_active
        ) VALUES %s
    """,
    "patient_coverage": """
        INSERT INTO clinical.patient_coverage (
            mrn, payer_id, payer_name_raw, subscriber_id, relationship_code,
            subscriber_last_name, subscriber_first_name, subscriber_dob,
            group_number, plan_name, coverage_rank, effective_from,
            effective_to, is_active
        ) VALUES %s
    """,
}

# Child tables first, so foreign keys are not violated on truncate
TRUNCATE_ORDER = [
    "clinical.patient_coverage", "clinical.patient_identifiers",
    "clinical.patient_addresses", "clinical.patients",
    "clinical.providers", "clinical.departments",
]


def load(tables, truncate):
    """Insert every table into health_ehr."""
    from psycopg2.extras import execute_values

    conn = connect("health_ehr")
    try:
        with conn, conn.cursor() as cur:
            if truncate:
                cur.execute(f"TRUNCATE {', '.join(TRUNCATE_ORDER)} CASCADE")
                print("Existing EHR master data removed.")

            for name, rows in tables:
                if not rows:
                    continue
                for start in range(0, len(rows), BATCH):
                    execute_values(cur, INSERTS[name],
                                   rows[start:start + BATCH], page_size=BATCH)
                print(f"  {name:22} {len(rows):>9,}")
    finally:
        conn.close()


# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate EHR master data.")
    parser.add_argument("--patients", type=int, default=DEFAULT_PATIENTS,
                        help=f"how many patients (default {DEFAULT_PATIENTS:,})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print counts and samples, write nothing")
    parser.add_argument("--truncate", action="store_true",
                        help="empty the tables before inserting")
    args = parser.parse_args()

    rng = random.Random(SEED)
    fake = Faker("en_US")
    Faker.seed(SEED)

    departments = build_departments()

    if args.dry_run:
        billing_providers = []
        print("Dry run: skipping the read from health_claims.")
    else:
        billing_providers = fetch_billing_providers()
        print(f"Read {len(billing_providers):,} providers from billing.")

    providers = build_providers(rng, billing_providers, departments)

    print(f"Generating {args.patients:,} patients...")
    patients, addresses, identifiers, coverage = build_patients(
        rng, fake, args.patients)

    duplicates = sum(1 for p in patients if p[14] is not None)
    linked = sum(1 for i in identifiers if i[1] == "ACCOUNT_NUMBER")
    no_cov = args.patients - len({c[0] for c in coverage})

    print()
    print(f"departments         {len(departments):>9,}")
    print(f"providers           {len(providers):>9,}")
    print(f"patients            {len(patients):>9,}")
    print(f"  merged duplicates {duplicates:>9,}")
    print(f"patient_addresses   {len(addresses):>9,}")
    print(f"patient_identifiers {len(identifiers):>9,}")
    print(f"  account-linked    {linked:>9,} "
          f"({linked / len(patients):.0%} of patients)")
    print(f"patient_coverage    {len(coverage):>9,}")
    print(f"  with no coverage  {no_cov:>9,} "
          f"({no_cov / len(patients):.0%} of patients)")

    if args.dry_run:
        print("\nSample patient:")
        print(" ", patients[0])
        print("Sample coverage:")
        print(" ", coverage[0])
        return

    print("\nLoading:")
    load([
        ("departments", departments),
        ("providers", providers),
        ("patients", patients),
        ("patient_addresses", addresses),
        ("patient_identifiers", identifiers),
        ("patient_coverage", coverage),
    ], args.truncate)


if __name__ == "__main__":
    main()
