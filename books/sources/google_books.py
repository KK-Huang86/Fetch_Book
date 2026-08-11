from __future__ import annotations

import random
import time
from typing import Callable

import httpx

from books.services.schema import CategoryInput, GoogleBooksResult, normalize_isbn

GOOGLE_BOOKS_BASE_URL = "https://www.googleapis.com/books/v1/volumes"

# design.md decision 9.
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_MAX_TOTAL_WAIT = 30.0
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def _parse_retry_after(header_value: str | None) -> float | None:
    if header_value is None:
        return None
    try:
        return float(header_value)
    except ValueError:
        # HTTP-date form is rare for this API in practice; fall back to
        # our own backoff schedule rather than trying to parse it.
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return max(0.0, retry_after)
    jitter = random.uniform(0, 0.5)
    return _BASE_DELAY * (2**attempt) + jitter


def _extract_isbn13(industry_identifiers: list[dict] | None) -> str | None:
    """design.md decision 4: prefer ISBN_13, else convert ISBN_10."""
    isbn13 = None
    isbn10 = None
    for identifier in industry_identifiers or []:
        if identifier.get("type") == "ISBN_13":
            isbn13 = identifier.get("identifier")
        elif identifier.get("type") == "ISBN_10":
            isbn10 = identifier.get("identifier")
    if isbn13:
        return normalize_isbn(isbn13)
    if isbn10:
        return normalize_isbn(isbn10)
    return None


def _extract_cover_image_url(image_links: dict | None) -> str | None:
    if not image_links:
        return None
    return image_links.get("thumbnail") or image_links.get("smallThumbnail")


def _extract_categories(categories: list[str] | None) -> list[CategoryInput]:
    return [
        CategoryInput(type="subject_tag", code="", label=label)
        for label in (categories or [])
    ]


def _parse_found_response(payload: dict, queried_isbn13: str) -> GoogleBooksResult:
    items = payload.get("items") or []
    if not items:
        return GoogleBooksResult(status="not_found", isbn13=queried_isbn13)

    volume_info = items[0].get("volumeInfo", {})
    # _extract_isbn13 validates decision 4's priority rule but the join
    # key back to our Book row stays the ISBN we searched by (design.md
    # decision 6's idempotency contract keys off our own normalized isbn13,
    # not whatever Google echoes back).
    _extract_isbn13(volume_info.get("industryIdentifiers"))

    return GoogleBooksResult(
        status="found",
        isbn13=queried_isbn13,
        cover_image_url=_extract_cover_image_url(volume_info.get("imageLinks")),
        categories=_extract_categories(volume_info.get("categories")),
    )


def query_google_books_by_isbn(
    isbn13: str,
    client: httpx.Client,
    api_key: str,
    sleep: Callable[[float], None] = time.sleep,
) -> GoogleBooksResult:
    """Look up a single ISBN via the Google Books API. Enrichment-only
    (design.md decision 1) — never raises; always returns a
    GoogleBooksResult with status found/not_found/failed."""
    params = {"q": f"isbn:{isbn13}", "key": api_key}
    elapsed_wait = 0.0
    error_message = "unknown error"

    for attempt in range(_MAX_RETRIES + 1):
        retry_after: float | None = None
        try:
            response = client.get(GOOGLE_BOOKS_BASE_URL, params=params, timeout=_TIMEOUT)
        except httpx.TimeoutException as exc:
            error_message = f"timeout: {exc}"
        except httpx.RequestError as exc:
            # Only timeout/429/5xx are retryable (design.md decision 9);
            # any other network error fails fast.
            return GoogleBooksResult(
                status="failed", isbn13=isbn13, error_message=f"network error: {exc}"
            )
        else:
            if response.status_code == 200:
                return _parse_found_response(response.json(), isbn13)
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                return GoogleBooksResult(
                    status="failed",
                    isbn13=isbn13,
                    error_message=f"HTTP {response.status_code}",
                )
            error_message = f"HTTP {response.status_code}"
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))

        remaining_budget = _MAX_TOTAL_WAIT - elapsed_wait
        if attempt >= _MAX_RETRIES or remaining_budget <= 0:
            return GoogleBooksResult(status="failed", isbn13=isbn13, error_message=error_message)

        delay = min(_backoff_delay(attempt, retry_after), remaining_budget)
        sleep(delay)
        elapsed_wait += delay

    return GoogleBooksResult(status="failed", isbn13=isbn13, error_message=error_message)
