"""
Generate the billing_providers master data for Riverside Regional Health.

Produces 2,219 providers:
    29 organisations, 890 employed individuals, 1,300 affiliated individuals.

The run is deterministic: the same seed always produces the same providers,
so the table can be rebuilt identically after a reset.
"""

import argparse
import os
import random
from datetime import date, timedelta

from faker import Faker

from npi import make_npi

SEED = 20250831

# Riverside Regional Health facilities
FACILITIES = ["RRH-MAIN", "RRH-NORTH", "RRH-EAST", "RRH-WEST"]

# (taxonomy_code, specialty_description)
INDIVIDUAL_SPECIALTIES = [
    ("207Q00000X", "Family Medicine"),
    ("207R00000X", "Internal Medicine"),
    ("207RC0000X", "Cardiovascular Disease"),
    ("2085R0202X", "Diagnostic Radiology"),
    ("207T00000X", "Neurological Surgery"),
    ("208000000X", "Pediatrics"),
    ("207V00000X", "Obstetrics & Gynecology"),
    ("207L00000X", "Anesthesiology"),
    ("208600000X", "Surgery"),
    ("207P00000X", "Emergency Medicine"),
    ("2084N0400X", "Neurology"),
    ("207X00000X", "Orthopaedic Surgery"),
    ("207N00000X", "Dermatology"),
    ("207W00000X", "Ophthalmology"),
    ("207RG0100X", "Gastroenterology"),
]

# (taxonomy_code, specialty_description, name suffixes that fit that specialty)
ORGANISATION_SPECIALTIES = [
    ("282N00000X", "General Acute Care Hospital",
     ["Medical Center", "Hospital", "Health Services"]),
    ("261QP2300X", "Primary Care Clinic",
     ["Family Practice", "Clinic", "Health Partners"]),
    ("261QA1903X", "Ambulatory Surgical Center",
     ["Surgical Associates", "Surgery Center"]),
    ("291U00000X", "Clinical Medical Laboratory",
     ["Laboratory", "Diagnostics"]),
    ("261QR0200X", "Radiology Clinic",
     ["Imaging Center", "Radiology Associates"]),
]

ORG_PREFIXES = [
    "Riverside", "Scioto", "Olentangy", "Franklin", "Whetstone",
    "Grandview", "Clintonville", "Dublin", "Westerville", "Gahanna",
    "Hilliard", "Worthington", "Bexley", "Upper Arlington", "Reynoldsburg",
]

# Volumes from the Riverside persona
N_ORGANISATIONS = 29
N_EMPLOYED = 890
N_AFFILIATED = 1300

TERMINATION_RATE = 0.06

ENROLMENT_START = date(2015, 1, 1)
ENROLMENT_END = date(2024, 12, 31)
TERMINATION_START = date(2023, 1, 1)
TERMINATION_END = date(2025, 12, 31)


def random_nine_digits(rng):
    """Return a random 9-digit string that does not start with 0."""
    first = rng.choice("123456789")
    rest = "".join(rng.choice("0123456789") for _ in range(8))
    return first + rest


def build_providers(rng, fake):
    """Build the full provider list. Returns a list of tuples ready for insert."""
    providers = []
    used_npis = set()

    def next_npi():
        """Return a unique Luhn-valid NPI."""
        while True:
            npi = make_npi(random_nine_digits(rng))
            if npi not in used_npis:
                used_npis.add(npi)
                return npi

    def enrolment_and_termination():
        """Return (enrolment_date, termination_date, is_active)."""
        enrolled = fake.date_between(start_date=ENROLMENT_START,
                                     end_date=ENROLMENT_END)
        if rng.random() < TERMINATION_RATE:
            # A provider cannot leave before they joined, so start the
            # termination window after the enrolment date
            window_start = max(TERMINATION_START, enrolled)
            terminated = window_start + timedelta(
                days=rng.randint(30, max(31, (TERMINATION_END - window_start).days))
            )
            return enrolled, terminated, False
        return enrolled, None, True

    # --- Organisations -------------------------------------------------
    used_org_names = set()
    for _ in range(N_ORGANISATIONS):
        taxonomy, specialty, suffixes = rng.choice(ORGANISATION_SPECIALTIES)
        enrolled, terminated, active = enrolment_and_termination()

        while True:
            name = f"{rng.choice(ORG_PREFIXES)} {rng.choice(suffixes)}"
            if name not in used_org_names:
                used_org_names.add(name)
                break

        providers.append((
            next_npi(),
            None,                       # provider_last_name
            None,                       # provider_first_name
            name,                       # organisation_name
            "ORGANISATION",
            taxonomy,
            specialty,
            f"31-{rng.randint(1000000, 9999999)}",   # tax_id (EIN format)
            rng.choice(FACILITIES),
            True,                       # is_employed
            enrolled,
            terminated,
            active,
        ))

    # --- Individuals ---------------------------------------------------
    for count, employed in ((N_EMPLOYED, True), (N_AFFILIATED, False)):
        for _ in range(count):
            taxonomy, specialty = rng.choice(INDIVIDUAL_SPECIALTIES)
            enrolled, terminated, active = enrolment_and_termination()

            providers.append((
                next_npi(),
                fake.last_name(),
                fake.first_name(),
                None,                   # organisation_name
                "INDIVIDUAL",
                taxonomy,
                specialty,
                f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-"
                f"{rng.randint(1000, 9999)}",        # tax_id (SSN format)
                rng.choice(FACILITIES) if employed else None,
                employed,
                enrolled,
                terminated,
                active,
            ))

    return providers


INSERT_SQL = """
INSERT INTO billing.billing_providers (
    provider_npi, provider_last_name, provider_first_name, organisation_name,
    provider_type, taxonomy_code, specialty_description, tax_id,
    facility_code, is_employed, enrolment_date, termination_date, is_active
) VALUES %s
ON CONFLICT (provider_npi) DO NOTHING
"""


def load_to_postgres(providers, truncate):
    """Insert the providers into health_claims.billing.billing_providers."""
    import psycopg2
    from psycopg2.extras import execute_values

    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5433"),
        dbname="health_claims",
        user=os.getenv("PGUSER", "dp_admin"),
        password=os.getenv("PGPASSWORD", "admin"),
    )

    try:
        with conn, conn.cursor() as cur:
            if truncate:
                cur.execute("TRUNCATE billing.billing_providers CASCADE")
                print("Existing providers removed.")

            execute_values(cur, INSERT_SQL, providers, page_size=500)
            cur.execute("SELECT count(*) FROM billing.billing_providers")
            print(f"Providers in table: {cur.fetchone()[0]}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Generate billing providers.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print a sample instead of writing to the database")
    parser.add_argument("--truncate", action="store_true",
                        help="empty the table before inserting")
    args = parser.parse_args()

    rng = random.Random(SEED)
    fake = Faker("en_US")
    Faker.seed(SEED)

    providers = build_providers(rng, fake)

    orgs = sum(1 for p in providers if p[4] == "ORGANISATION")
    employed = sum(1 for p in providers if p[4] == "INDIVIDUAL" and p[9])
    affiliated = sum(1 for p in providers if p[4] == "INDIVIDUAL" and not p[9])
    terminated = sum(1 for p in providers if not p[12])

    print(f"Generated {len(providers)} providers")
    print(f"  organisations : {orgs}")
    print(f"  employed      : {employed}")
    print(f"  affiliated    : {affiliated}")
    print(f"  terminated    : {terminated} "
          f"({terminated / len(providers):.1%})")

    if args.dry_run:
        print("\nSample rows:")
        for row in providers[:3] + providers[30:33]:
            print(" ", row)
        return

    load_to_postgres(providers, args.truncate)


if __name__ == "__main__":
    main()