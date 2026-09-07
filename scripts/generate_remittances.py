"""
Generate 835 remittance advice for Riverside Regional Health.

Populates four tables in health_claims.billing:
    remittances, remittance_claims, remittance_lines, claim_adjustments

This is the OUTCOME of a claim, and it arrives days or weeks after the
claim was submitted. Everything the model is trying to predict — whether
a claim was denied, why, and how much was paid — lives here and nowhere
else.

WHY THIS IS A SEPARATE SCRIPT AND A SEPARATE TABLE
--------------------------------------------------
Putting a denial flag on the claim row would make a denial-prediction
model trivially accurate and completely useless, because that flag does
not exist at the moment a claim is submitted. Keeping the outcome in a
separate table that arrives later forces the pipeline to reconstruct
"what was known at submission time", which is what stops label leakage
(problem D1) from being an accident waiting to happen.

Deliberate messiness built in here, not injected later:
  - payment lag varies by payer, 21 to 68 days, with a long tail
  - ~4% of claims never receive a remittance at all: not paid, not
    denied, just silent. They are not negatives, and treating them as
    negatives poisons the model.
  - one payment covers many claims; one claim can be split across
    several payments (problem A4)
  - a claim can be paid, then reversed, then repaid — status code 22
  - adjudication is per LINE, not per claim: line 1 paid, line 2 denied,
    line 3 bundled. Any claim-grain fact table gives wrong answers.
  - a small share of remittance rows reference a claim_id billing does
    not have, because the payer corrected something on their side
    (problem A5)
  - the payer sometimes re-codes a procedure, so the adjudicated code
    differs from the submitted one

Run this AFTER generate_claims.py.

Usage:
    python3 generate_remittances.py --dry-run
    python3 generate_remittances.py --start 2025-10-01 --end 2025-12-31
    python3 generate_remittances.py --truncate
"""

import argparse
import os
import random
from collections import defaultdict
from datetime import date, timedelta

from identifiers import make_remittance_id

SEED = 20250831
BATCH = 5_000

# (payer_id, denial_rate, mean_days_to_pay) from the persona
PAYER_BEHAVIOUR = {
    "MCR01": (0.06, 21),
    "OHMCD": (0.14, 38),
    "ANTOH": (0.09, 32),
    "UHC01": (0.12, 35),
    "MAPLN": (0.15, 44),
    "AETBH": (0.11, 34),
    "CIG01": (0.10, 33),
    "OHWCP": (0.19, 68),
    "SELFP": (0.00, 0),      # self-pay never produces an 835
}
DEFAULT_BEHAVIOUR = (0.11, 36)

# CARC distribution from the persona: (group, code, share)
DENIAL_REASONS = [
    ("CO", "197", 0.18),    # prior authorisation absent
    ("CO", "16",  0.15),    # missing or invalid information
    ("CO", "97",  0.13),    # bundled into another service
    ("CO", "45",  0.12),    # charge exceeds fee schedule
    ("PR", "1",   0.09),    # deductible
    ("CO", "29",  0.08),    # timely filing limit exceeded
    ("CO", "11",  0.07),    # diagnosis inconsistent with procedure
    ("CO", "18",  0.06),    # duplicate claim
    ("CO", "109", 0.05),    # not covered by this payer
    ("CO", "4",   0.02),    # modifier missing or inconsistent
    ("CO", "96",  0.02),    # non-covered charge
    ("CO", "119", 0.01),    # benefit maximum reached
    ("CO", "204", 0.01),    # not covered under the patient's plan
    ("PR", "2",   0.01),    # coinsurance
]

# Contractual write-off: what the payer will never pay because of the
# contracted rate. Present on almost every paid claim, and the single
# biggest reason charge and payment differ.
CONTRACTUAL_CODE = ("CO", "45")

RARC_CODES = ["N130", "N386", "M80", "MA04", "N4", "M15", "N362", "N19"]

# Timely filing: a claim submitted more than this many days after service
# gets denied for age, whatever else is wrong with it.
TIMELY_FILING_DAYS = 90
TIMELY_FILING_DENIAL = ("CO", "29")

NEVER_REMITTED_RATE = 0.04
SPLIT_PAYMENT_RATE = 0.03
REVERSAL_RATE = 0.012
PHANTOM_CLAIM_RATE = 0.004      # payer references a claim billing lacks
RECODE_RATE = 0.025             # payer re-codes the submitted procedure

# What share of the charge the payer allows, by payer type
ALLOWED_RATIO = {
    "MCR01": (0.30, 0.42),
    "OHMCD": (0.22, 0.34),
    "MAPLN": (0.32, 0.46),
    "OHWCP": (0.55, 0.78),
}
DEFAULT_ALLOWED_RATIO = (0.38, 0.58)

PAYMENT_METHODS = ["ACH"] * 8 + ["CHK", "FWT"]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def weighted_pick(rng, options):
    """Pick one option from [(a, b, weight), ...] by its last element."""
    total = sum(o[-1] for o in options)
    r = rng.random() * total
    upto = 0.0
    for option in options:
        upto += option[-1]
        if r <= upto:
            return option
    return options[-1]


def payment_lag(rng, payer_id):
    """
    Days between submission and the payer's response.

    Log-normal-ish: most land near the payer's mean, a tail runs long.
    That tail is what makes "no remittance yet" different from "denied".
    """
    _rate, mean_days = PAYER_BEHAVIOUR.get(payer_id, DEFAULT_BEHAVIOUR)
    if mean_days == 0:
        return None
    lag = rng.gauss(mean_days, mean_days * 0.35)
    if rng.random() < 0.08:
        lag *= rng.uniform(1.8, 3.4)        # stuck in review
    return max(3, int(lag))


def allowed_amount(rng, payer_id, charge):
    """What the payer says the service is worth under contract."""
    low, high = ALLOWED_RATIO.get(payer_id, DEFAULT_ALLOWED_RATIO)
    return round(charge * rng.uniform(low, high), 2)


# ---------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------

def build(rng, claims, lines_by_claim):
    """
    Build every remittance table.

    Returns a dict of table name -> rows. remittance_claims and
    remittance_lines are linked by a positional index rather than a
    database id, because the ids do not exist until insert time.
    """
    remittances = []
    remit_claims = []       # (remit_index, claim_id, ...)
    remit_lines = []        # (remit_claim_index, ...)
    adjustments = []        # (remit_claim_index|None, remit_line_index|None, ...)

    # Payments are batched: a payer sends one file covering many claims
    # on a given day.
    pending = defaultdict(list)     # (payer_id, response_date) -> [claim]

    for claim in claims:
        (claim_id, freq_code, payer_id, submission_date, statement_from,
         total_charge, claim_status) = claim

        if claim_status == "VOIDED" or freq_code == "8":
            continue                        # a void is never adjudicated
        if payer_id == "SELFP":
            continue                        # no payer, no 835

        if rng.random() < NEVER_REMITTED_RATE:
            # Silent. Not paid, not denied. The pipeline must not treat
            # these as negatives.
            continue

        lag = payment_lag(rng, payer_id)
        if lag is None:
            continue

        response_date = submission_date + timedelta(days=lag)
        pending[(payer_id, response_date)].append(claim)

    remit_seq = 0

    for (payer_id, response_date), batch in sorted(
            pending.items(), key=lambda kv: (kv[0][1], kv[0][0])):

        remit_seq += 1
        remittance_id = make_remittance_id(remit_seq, response_date.year)
        # Reserve this remittance's slot before the loop appends any
        # split or reversal rows, so their indices stay stable.
        remit_index = len(remittances)
        remittances.append(None)

        # The payer generates the file, then it takes a few days to
        # arrive and a few more to be posted.
        production = response_date
        received = production + timedelta(days=rng.randint(1, 6))
        posted = received + timedelta(days=rng.randint(0, 4))

        remittance_total = 0.0

        for claim in batch:
            (claim_id, freq_code, _payer, submission_date, statement_from,
             total_charge, _status) = claim

            lines = lines_by_claim.get(claim_id, [])
            if not lines:
                continue

            denial_rate, _mean = PAYER_BEHAVIOUR.get(payer_id,
                                                     DEFAULT_BEHAVIOUR)

            # Timely filing overrides everything: a claim submitted too
            # long after the service is denied for age.
            days_to_submit = (submission_date - statement_from).days
            forced_timely = days_to_submit > TIMELY_FILING_DAYS

            # Adjudicate LINE BY LINE. A claim is not one outcome.
            line_results = []
            for line_id, line_number, proc_code, mod1, units, charge in lines:
                charge = float(charge or 0)

                if forced_timely:
                    denied, group, carc = True, *TIMELY_FILING_DENIAL
                elif rng.random() < denial_rate:
                    group, carc, _share = weighted_pick(rng, DENIAL_REASONS)
                    denied = True
                else:
                    denied, group, carc = False, None, None

                allowed = 0.0 if denied else allowed_amount(rng, payer_id,
                                                            charge)
                paid = allowed

                # Deductible and coinsurance come out of the allowed
                # amount and become the patient's problem.
                patient_resp = 0.0
                if not denied and rng.random() < 0.34:
                    patient_resp = round(allowed * rng.uniform(0.05, 0.40), 2)
                    paid = round(allowed - patient_resp, 2)

                # The payer sometimes re-codes what was submitted
                adjudicated_code = proc_code
                if rng.random() < RECODE_RATE:
                    adjudicated_code = proc_code[:-1] + \
                        rng.choice("0123456789")

                line_results.append({
                    "line_id": line_id, "line_number": line_number,
                    "code": adjudicated_code, "mod1": mod1,
                    "charge": charge, "allowed": allowed, "paid": paid,
                    "units": units, "denied": denied,
                    "group": group, "carc": carc,
                    "patient_resp": patient_resp,
                })

            if not line_results:
                continue

            claim_charge = sum(r["charge"] for r in line_results)
            claim_paid = sum(r["paid"] for r in line_results)
            claim_patient = sum(r["patient_resp"] for r in line_results)
            all_denied = all(r["denied"] for r in line_results)

            # 1 processed as primary, 4 denied, 22 reversal
            status_code = "4" if all_denied else "1"

            # Split payment: the payer pays part now and part later. Both
            # halves reference the same claim, so a claim-grain join
            # double counts unless it aggregates.
            split = (not all_denied) and rng.random() < SPLIT_PAYMENT_RATE
            share = rng.uniform(0.35, 0.65) if split else 1.0

            rc_index = len(remit_claims)
            remit_claims.append((
                remit_index,
                # The payer occasionally reports a claim billing has no
                # record of — their own correction, or an acquired
                # practice's claim.
                claim_id if rng.random() > PHANTOM_CLAIM_RATE
                else f"PYR{rng.randint(10**11, 10**12 - 1)}",
                f"{payer_id}{rng.randint(10**9, 10**10 - 1)}",
                status_code,
                round(claim_charge, 2),
                round(claim_paid * share, 2),
                round(claim_patient, 2),
            ))

            remittance_total += claim_paid * share

            for r in line_results:
                rl_index = len(remit_lines)
                remit_lines.append((
                    rc_index,
                    r["line_id"], r["line_number"], r["code"], r["mod1"],
                    None,
                    round(r["charge"], 2),
                    round(r["paid"] * share, 2),
                    round(r["allowed"], 2),
                    r["units"],
                ))

                if r["denied"]:
                    adjustments.append((
                        None, rl_index, r["group"], r["carc"],
                        rng.choice(RARC_CODES) if rng.random() < 0.42
                        else None,
                        round(r["charge"], 2), None,
                    ))
                else:
                    # Contractual write-off, present on nearly every
                    # paid line: the gap between charge and allowed.
                    writeoff = round(r["charge"] - r["allowed"], 2)
                    if writeoff > 0:
                        adjustments.append((
                            None, rl_index, CONTRACTUAL_CODE[0],
                            CONTRACTUAL_CODE[1], None, writeoff, None,
                        ))
                    if r["patient_resp"] > 0:
                        adjustments.append((
                            None, rl_index, "PR",
                            rng.choice(["1", "2", "3"]), None,
                            round(r["patient_resp"], 2), None,
                        ))

            # Second half of a split payment, sent later
            if split:
                remit_seq += 1
                later = received + timedelta(days=rng.randint(14, 60))
                second_index = len(remittances)
                remittances.append((
                    make_remittance_id(remit_seq, later.year), payer_id,
                    rng.choice(PAYMENT_METHODS),
                    f"{rng.randint(10**9, 10**10 - 1)}",
                    round(claim_paid * (1 - share), 2),
                    later, later + timedelta(days=rng.randint(1, 5)),
                    later + timedelta(days=rng.randint(1, 8)),
                ))
                remit_claims.append((
                    second_index, claim_id,
                    f"{payer_id}{rng.randint(10**9, 10**10 - 1)}",
                    "1",
                    round(claim_charge, 2),
                    round(claim_paid * (1 - share), 2),
                    0,
                ))

            # A payment reversed and reissued. The original stays.
            if (not all_denied) and rng.random() < REVERSAL_RATE:
                remit_seq += 1
                later = received + timedelta(days=rng.randint(20, 90))
                rev_index = len(remittances)
                remittances.append((
                    make_remittance_id(remit_seq, later.year), payer_id,
                    rng.choice(PAYMENT_METHODS),
                    f"{rng.randint(10**9, 10**10 - 1)}",
                    round(-claim_paid * share, 2),
                    later, later + timedelta(days=rng.randint(1, 5)),
                    later + timedelta(days=rng.randint(1, 8)),
                ))
                remit_claims.append((
                    rev_index, claim_id,
                    f"{payer_id}{rng.randint(10**9, 10**10 - 1)}",
                    "22",                       # reversal of previous payment
                    round(claim_charge, 2),
                    round(-claim_paid * share, 2),
                    0,
                ))

        remittances[remit_index] = (
            remittance_id, payer_id,
            rng.choice(PAYMENT_METHODS),
            f"{rng.randint(10**9, 10**10 - 1)}",
            round(remittance_total, 2),
            production, received, posted,
        )

    return {
        "remittances": remittances,
        "remittance_claims": remit_claims,
        "remittance_lines": remit_lines,
        "claim_adjustments": adjustments,
    }


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def connect():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5433"),
        dbname="health_claims",
        user=os.getenv("PGUSER", "dp_admin"),
        password=os.getenv("PGPASSWORD", "admin"),
    )


def fetch_claims(start, end):
    """Read the submitted claims and their lines."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT claim_id, claim_frequency_code, payer_id,
                       submission_date, statement_from_date,
                       total_charge_amount, claim_status
                FROM billing.claims
                WHERE submission_date BETWEEN %s AND %s
                ORDER BY submission_date, claim_id
            """, (start, end))
            claims = cur.fetchall()

            cur.execute("""
                SELECT l.claim_id, l.claim_line_id, l.line_number,
                       l.procedure_code, l.modifier_1, l.service_units,
                       l.charge_amount
                FROM billing.claim_lines l
                JOIN billing.claims c USING (claim_id)
                WHERE c.submission_date BETWEEN %s AND %s
                ORDER BY l.claim_id, l.line_number
            """, (start, end))
            lines = defaultdict(list)
            for row in cur.fetchall():
                lines[row[0]].append(row[1:])

        return claims, lines
    finally:
        conn.close()


REMITTANCE_SQL = """
    INSERT INTO billing.remittances (
        remittance_id, payer_id, payment_method_code, check_eft_number,
        total_payment_amount, production_date, received_date, posted_date
    ) VALUES %s ON CONFLICT (remittance_id) DO NOTHING
"""

REMIT_CLAIM_SQL = """
    INSERT INTO billing.remittance_claims (
        remittance_id, claim_id, payer_claim_control_number,
        claim_status_code, total_charge_amount, total_paid_amount,
        patient_responsibility_amount
    ) VALUES %s RETURNING remittance_claim_id
"""

REMIT_LINE_SQL = """
    INSERT INTO billing.remittance_lines (
        remittance_claim_id, claim_line_id, line_number,
        adjudicated_procedure_code, modifier_1, modifier_2,
        charge_amount, paid_amount, allowed_amount, units_paid
    ) VALUES %s RETURNING remittance_line_id
"""

ADJUSTMENT_SQL = """
    INSERT INTO billing.claim_adjustments (
        remittance_claim_id, remittance_line_id, adjustment_group_code,
        adjustment_reason_code, remark_code, adjustment_amount,
        adjustment_quantity
    ) VALUES %s
"""

TRUNCATE_ORDER = [
    "billing.claim_adjustments", "billing.remittance_lines",
    "billing.remittance_claims", "billing.remittances",
]


def load(tables, truncate):
    """
    Insert the remittance tables, resolving generated ids as we go.

    remittance_claims and remittance_lines use BIGSERIAL keys, so the
    child rows cannot be built until the parents are inserted and their
    ids come back.
    """
    from psycopg2.extras import execute_values

    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            if truncate:
                cur.execute(f"TRUNCATE {', '.join(TRUNCATE_ORDER)} CASCADE")
                print("Existing remittance data removed.")

            remittances = tables["remittances"]
            for i in range(0, len(remittances), BATCH):
                execute_values(cur, REMITTANCE_SQL,
                               remittances[i:i + BATCH], page_size=BATCH)
            print(f"  remittances            {len(remittances):>10,}")

            # Swap the positional remittance index for the real id
            remit_ids = [r[0] for r in remittances]
            rc_rows = [(remit_ids[r[0]],) + tuple(r[1:])
                       for r in tables["remittance_claims"]]

            rc_ids = []
            for i in range(0, len(rc_rows), BATCH):
                returned = execute_values(cur, REMIT_CLAIM_SQL,
                                          rc_rows[i:i + BATCH],
                                          page_size=BATCH, fetch=True)
                rc_ids.extend(r[0] for r in returned)
            print(f"  remittance_claims      {len(rc_rows):>10,}")

            rl_rows = [(rc_ids[r[0]],) + tuple(r[1:])
                       for r in tables["remittance_lines"]]

            rl_ids = []
            for i in range(0, len(rl_rows), BATCH):
                returned = execute_values(cur, REMIT_LINE_SQL,
                                          rl_rows[i:i + BATCH],
                                          page_size=BATCH, fetch=True)
                rl_ids.extend(r[0] for r in returned)
            print(f"  remittance_lines       {len(rl_rows):>10,}")

            adj_rows = [
                (rc_ids[a[0]] if a[0] is not None else None,
                 rl_ids[a[1]] if a[1] is not None else None) + tuple(a[2:])
                for a in tables["claim_adjustments"]
            ]
            for i in range(0, len(adj_rows), BATCH):
                execute_values(cur, ADJUSTMENT_SQL, adj_rows[i:i + BATCH],
                               page_size=BATCH)
            print(f"  claim_adjustments      {len(adj_rows):>10,}")
    finally:
        conn.close()


# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate 835 remittances.")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--end", default="2026-03-31",
                        help="claims submitted up to this date are adjudicated")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--truncate", action="store_true")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rng = random.Random(SEED)

    if args.dry_run:
        claims, lines = synthetic_claims(rng)
        print("Dry run: using synthetic claims.")
    else:
        print("Reading claims...")
        claims, lines = fetch_claims(start, end)
        print(f"Read {len(claims):,} claims, {len(lines):,} with lines.")

    print("Adjudicating...")
    tables = build(rng, claims, lines)

    print()
    for name in ["remittances", "remittance_claims", "remittance_lines",
                 "claim_adjustments"]:
        print(f"{name:24} {len(tables[name]):>10,}")

    adjudicated = {rc[1] for rc in tables["remittance_claims"]}
    billable = [c for c in claims
                if c[2] != "SELFP" and c[1] != "8" and c[6] != "VOIDED"]
    silent = len(billable) - len(adjudicated)

    denied_lines = sum(1 for a in tables["claim_adjustments"]
                       if a[2] in ("CO", "PI") and a[3] != "45")
    total_lines = len(tables["remittance_lines"])

    print(f"\nbillable claims        {len(billable):>10,}")
    print(f"never remitted         {silent:>10,} "
          f"({silent / max(1, len(billable)):.1%})")
    print(f"denied lines           {denied_lines:>10,} "
          f"({denied_lines / max(1, total_lines):.1%})")

    if args.dry_run:
        print("\nSample remittance:", tables["remittances"][0])
        return

    print("\nLoading:")
    load(tables, args.truncate)


def synthetic_claims(rng):
    """Claims for a dry run, so nothing has to be read."""
    claims, lines = [], {}
    payers = list(PAYER_BEHAVIOUR)
    for i in range(1, 30001):
        claim_id = f"RRH25{i:09d}"
        payer = rng.choice(payers)
        service = date(2025, 10, 1) + timedelta(days=rng.randint(0, 91))
        submitted = service + timedelta(days=rng.randint(2, 28))
        n_lines = rng.randint(1, 6)
        charges = [round(rng.uniform(40, 2400), 2) for _ in range(n_lines)]
        claims.append((claim_id, "1", payer, submitted, service,
                       round(sum(charges), 2), "SUBMITTED"))
        lines[claim_id] = [
            (f"{claim_id}-{n:03d}", n, rng.choice(["99213", "80053", "71046"]),
             None, 1, charges[n - 1])
            for n in range(1, n_lines + 1)
        ]
    return claims, lines


if __name__ == "__main__":
    main()
