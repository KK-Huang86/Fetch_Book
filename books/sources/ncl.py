from __future__ import annotations

import csv
import io
import re

import httpx

from books.services.schema import (
    CategoryInput,
    ParsedBookRecord,
    ParseFailure,
    normalize_isbn,
    split_authors,
)

# Confirmed against 4 real monthly downloads (2024-12, 2025-01, 2025-07,
# 2025-08) — see design.md decision 8. Field names, not positions, are the
# contract: NCL has renamed columns before (常用分類→建議上架分類 etc.).
_CLASSIFICATION_NUMBER_FIELD = "分類號"
_SHELF_CATEGORY_FIELDS = ("建議上架分類", "常用分類")  # 常用分類：2025-01 及更早
_SUBJECT_TAG_FIELD = "圖書主題"

NCL_BASE_URL = "https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/{year_month}_isbn.csv"
_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")

# design.md decision 8: no published NCL timeout SLA, so this mirrors
# decision 9's Google Books connect timeout; read is longer since a
# monthly CSV (~1-1.5MB observed) is a bigger single transfer than one
# API response.
_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class NclDownloadError(Exception):
    """Base class for NCL CSV download failures."""


class NclNotFoundError(NclDownloadError):
    """The requested month's CSV has not been published yet (HTTP 404)."""


class NclDownloadTimeoutError(NclDownloadError):
    """Connect or read timeout while downloading."""


class NclNetworkError(NclDownloadError):
    """Network-level failure other than a timeout (DNS, connection reset, ...)."""


def build_ncl_csv_url(month: str) -> str:
    """Build the monthly CSV URL. `month` must be `YYYY-MM` (Gregorian
    year — confirmed by real request, see design.md decision 8)."""
    if not isinstance(month, str) or not _MONTH_PATTERN.fullmatch(month):
        raise ValueError(f"month must be in YYYY-MM format, got {month!r}")
    year_month = month.replace("-", "")
    return NCL_BASE_URL.format(year_month=year_month)


def download_ncl_csv(month: str, client: httpx.Client) -> bytes:
    """Download the raw CSV bytes for `month`. Pure I/O — parsing is
    `parse_ncl_csv`'s job. Raises a subclass of `NclDownloadError` on
    failure; callers (ingest_month/task) decide what each case means
    (e.g. 404 is "not yet published", not a hard failure, when scheduled)."""
    url = build_ncl_csv_url(month)

    try:
        response = client.get(url, timeout=_DOWNLOAD_TIMEOUT)
    except httpx.TimeoutException as exc:
        raise NclDownloadTimeoutError(f"timed out downloading {url}") from exc
    except httpx.RequestError as exc:
        raise NclNetworkError(f"network error downloading {url}: {exc}") from exc

    if response.status_code == 404:
        raise NclNotFoundError(f"{month} NCL CSV not yet published ({url})")

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise NclDownloadError(
            f"unexpected HTTP {response.status_code} downloading {url}"
        ) from exc

    return response.content


def _first_value(row: dict[str, str], fields: tuple[str, ...]) -> str:
    for field_name in fields:
        value = (row.get(field_name) or "").strip()
        if value:
            return value
    return ""


def _row_categories(row: dict[str, str]) -> list[CategoryInput]:
    categories: list[CategoryInput] = []

    classification_number = row.get(_CLASSIFICATION_NUMBER_FIELD, "").strip()
    if classification_number:
        categories.append(
            CategoryInput(
                type="classification_number",
                code=classification_number,
                label=classification_number,
            )
        )

    shelf_category = _first_value(row, _SHELF_CATEGORY_FIELDS)
    if shelf_category:
        categories.append(
            CategoryInput(type="shelf_category", code="", label=shelf_category)
        )

    subject_tag = row.get(_SUBJECT_TAG_FIELD, "").strip()
    if subject_tag:
        categories.append(CategoryInput(type="subject_tag", code="", label=subject_tag))

    return categories


def parse_ncl_csv(raw: bytes) -> tuple[list[ParsedBookRecord], list[ParseFailure]]:
    """Parse NCL's monthly 新書預告書訊 CSV (already-downloaded bytes) into
    intermediate book records. Pure function — no network I/O.

    A row with a missing or invalid ISBN is reported as a ParseFailure and
    excluded from `records`; it never raises and never aborts the rest of
    the batch (per design.md decision 8/CLAUDE.md 資料操作穩健性規範).
    """
    records: list[ParsedBookRecord] = []
    failures: list[ParseFailure] = []

    if not raw:
        return records, failures

    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        raw_isbn = (row.get("ISBN") or "").strip()
        isbn13 = normalize_isbn(raw_isbn)

        if isbn13 is None:
            if not raw_isbn:
                failures.append(
                    ParseFailure(
                        isbn=None,
                        error_code="missing_isbn",
                        message="row has no ISBN value",
                    )
                )
            else:
                failures.append(
                    ParseFailure(
                        isbn=raw_isbn,
                        error_code="invalid_isbn",
                        message=f"ISBN '{raw_isbn}' failed checksum/format validation",
                    )
                )
            continue

        records.append(
            ParsedBookRecord(
                isbn13=isbn13,
                title=(row.get("申請書名") or "").strip(),
                authors=split_authors(row.get("作者")),
                publisher=(row.get("出版機構") or "").strip(),
                categories=_row_categories(row),
            )
        )

    return records, failures
