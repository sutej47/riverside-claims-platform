def luhn_check_digit(nine_digits: str) -> str:
    """Return the Luhn check digit for the first 9 digits of an NPI."""
    # NPI uses the 80840 healthcare prefix in the Luhn calculation
    digits = "80840" + nine_digits

    total = 0
    # Walk right to left, doubling every second digit
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n = n * 2
            if n > 9:
                n = n - 9
        total = total + n

    # Check digit is what brings the total up to the next multiple of 10
    return str((10 - total % 10) % 10)


def make_npi(nine_digits: str) -> str:
    """Return a full 10-digit NPI (9 digits + check digit)."""
    return nine_digits + luhn_check_digit(nine_digits)


if __name__ == "__main__":
    print(make_npi("123456789"))