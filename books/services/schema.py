from __future__ import annotations

import re


def _isbn10_check_digit_valid(digits: str) -> bool:
    total = sum((10 - i) * (10 if d == "X" else int(d)) for i, d in enumerate(digits))
    return total % 11 == 0


def _isbn13_check_digit(first_12: str) -> str:
    total = sum(
        int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first_12)
    )
    return str((10 - total % 10) % 10)


def _isbn13_check_digit_valid(digits: str) -> bool:
    return digits[-1] == _isbn13_check_digit(digits[:12])


def _isbn10_to_isbn13(isbn10: str) -> str:
    first_12 = "978" + isbn10[:9]
    return first_12 + _isbn13_check_digit(first_12)


def normalize_isbn(raw: str | None) -> str | None:
    """Normalize a raw ISBN string to ISBN-13, or None if invalid/missing."""
    if not raw:
        return None

    cleaned = re.sub(r"[\s-]", "", raw).upper()

    if len(cleaned) == 10 and re.fullmatch(r"[0-9]{9}[0-9X]", cleaned):
        if not _isbn10_check_digit_valid(cleaned):
            return None
        return _isbn10_to_isbn13(cleaned)

    if (
        len(cleaned) == 13
        and cleaned.isdigit()
        and cleaned.startswith(("978", "979"))
    ):
        if not _isbn13_check_digit_valid(cleaned):
            return None
        return cleaned

    return None
