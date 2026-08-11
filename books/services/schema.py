from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class CategoryInput:
    """One (type, code, label) category value parsed from a source, prior
    to being resolved against the DB's Category table (seam 4/5)."""

    type: str
    code: str
    label: str


@dataclass(frozen=True)
class ParsedBookRecord:
    """Source-agnostic intermediate shape produced by a source parser
    (e.g. books/sources/ncl.py) and consumed by merge/upsert (seam 4/5)."""

    isbn13: str
    title: str
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    categories: list[CategoryInput] = field(default_factory=list)


@dataclass(frozen=True)
class ParseFailure:
    """A row that could not be turned into a ParsedBookRecord. `isbn` is
    the raw (possibly invalid) ISBN string if one was present, else None."""

    isbn: str | None
    error_code: str
    message: str


@dataclass(frozen=True)
class GoogleBooksResult:
    """Outcome of a single-ISBN Google Books lookup (books/sources/google_books.py).

    Google Books is enrichment-only (design.md decision 1/5): a "found"
    result supplies cover_image_url/categories for merge to layer on top
    of NCL data, never title/authors/publisher. `status` is always one of
    "found"/"not_found"/"failed" — the function never raises for a
    not-found ISBN or an exhausted-retry request (design.md decision 9 /
    tasks.md 3.x acceptance criteria: 回傳明確結果，不拋出未處理例外);
    `error_message` is only meaningful when status == "failed".
    """

    status: Literal["found", "not_found", "failed"]
    isbn13: str
    cover_image_url: str | None = None
    categories: list[CategoryInput] = field(default_factory=list)
    error_message: str | None = None


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


_AUTHOR_GROUP_SEPARATORS = re.compile(r"[;；]")
_AUTHOR_NAME_SEPARATORS = re.compile(r"[,，、]")
_TRAILING_PUNCTUATION = ".·・、,，；;"
# Role-suffix tokens observed in NCL's 作者 field (著/編著/譯/主編/...),
# longest first so e.g. "編著" is stripped as one unit, not left as "編".
_ROLE_SUFFIXES = tuple(
    sorted(
        ["編著", "主編", "主講", "口述", "審訂", "編輯", "作", "譯", "著", "繪", "編", "撰"],
        key=len,
        reverse=True,
    )
)


def _strip_role_suffix(name: str) -> str:
    name = name.strip()
    changed = True
    while changed:
        changed = False
        without_punct = name.rstrip(_TRAILING_PUNCTUATION)
        if without_punct != name:
            name = without_punct
            changed = True
        if name in _ROLE_SUFFIXES:
            # The whole segment IS a role word (malformed source data) —
            # stop here rather than stripping it down to a shorter token
            # or an empty string.
            break
        for token in _ROLE_SUFFIXES:
            if name.endswith(token) and len(name) > len(token):
                name = name[: -len(token)]
                changed = True
                break
    return name.strip()


def split_authors(raw: str | None) -> list[str]:
    """Split NCL's free-text 作者 field into individual author names.

    NCL mixes multiple names, role words (著/編著/譯/主編/...), and two
    layers of separators (";" between role groups, "," within a group) in
    one field, e.g. "王素貞, 蘇平齡編著" or
    "黛安娜.韋恩.瓊斯(Diana Wynne Jones)著; 呂明璇, 黃筱茵譯".
    """
    if not raw or not raw.strip():
        return []

    names: list[str] = []
    for group in _AUTHOR_GROUP_SEPARATORS.split(raw):
        for name in _AUTHOR_NAME_SEPARATORS.split(group):
            cleaned = _strip_role_suffix(name)
            if cleaned:
                names.append(cleaned)
    return names


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
