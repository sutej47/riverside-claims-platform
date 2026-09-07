"""
Identifier helpers shared by every generator.

MRN and account number are derived from the same patient sequence number,
so the EHR generator and the claims generator can agree on which account
belongs to which patient without a shared table — which is exactly the
mapping the pipeline is supposed to have to rediscover.
"""

# Digits are shuffled, not just reformatted, so the link is not obvious
# by eye. A pipeline cannot cheat by substringing one from the other.
_ACCOUNT_SHUFFLE = [3, 7, 1, 5, 0, 8, 2, 6, 4]


def make_mrn(seq: int) -> str:
    """Return the EHR medical record number for a patient sequence number."""
    return f"MRN{seq:08d}"


def make_account_number(seq: int) -> str:
    """Return the billing account number for the same patient."""
    digits = f"{seq:09d}"
    shuffled = "".join(digits[i] for i in _ACCOUNT_SHUFFLE)
    # Check digit keeps the format plausible and gives the pipeline
    # something to validate.
    checksum = sum(int(d) for d in shuffled) % 10
    return f"ACC{shuffled}{checksum}"


def make_encounter_id(seq: int, year: int) -> str:
    """Return an encounter id: ENC + 2-digit year + 9 digits."""
    return f"ENC{year % 100:02d}{seq:09d}"


def make_order_id(seq: int, year: int) -> str:
    """Return an order id: ORD + 2-digit year + 9 digits."""
    return f"ORD{year % 100:02d}{seq:09d}"


def make_claim_id(seq: int, year: int) -> str:
    """Return a claim id: RRH + 2-digit year + 9 digits."""
    return f"RRH{year % 100:02d}{seq:09d}"


def make_remittance_id(seq: int, year: int) -> str:
    """Return a remittance id: RA + 2-digit year + 8 digits."""
    return f"RA{year % 100:02d}{seq:08d}"
