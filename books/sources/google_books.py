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


def _extract_isbn13(industry_identifiers: object) -> str | None:
    """design.md decision 4: prefer ISBN_13, else convert ISBN_10.

    Defensive against malformed input (Google's response is an external
    boundary, see tasks.md 3.x "不拋出未處理例外"): a non-list, or entries
    that aren't dicts, are treated as "no usable identifier" rather than
    raising.
    """
    if not isinstance(industry_identifiers, list):
        return None

    isbn13 = None
    isbn10 = None
    for identifier in industry_identifiers:
        if not isinstance(identifier, dict):
            continue
        if identifier.get("type") == "ISBN_13":
            isbn13 = identifier.get("identifier")
        elif identifier.get("type") == "ISBN_10":
            isbn10 = identifier.get("identifier")
    if isbn13:
        return normalize_isbn(isbn13)
    if isbn10:
        return normalize_isbn(isbn10)
    return None


def _extract_cover_image_url(image_links: object) -> str | None:
    if not isinstance(image_links, dict):
        return None
    return image_links.get("thumbnail") or image_links.get("smallThumbnail")


def _extract_categories(categories: object) -> list[CategoryInput]:
    if not isinstance(categories, list):
        return []
    labels = dict.fromkeys(
        label.strip()
        for label in categories
        if isinstance(label, str) and label.strip()
    )
    return [CategoryInput(type="subject_tag", code="", label=label) for label in labels]


class _MalformedGoogleBooksResponse(Exception):
    """Raised for a 200 response whose shape can't be interpreted at all
    (not: a per-item quirk, which is handled defensively inline instead)."""


def _parse_found_response(payload: dict, queried_isbn13: str) -> GoogleBooksResult:
    items = payload.get("items")
    if items is None:
        return GoogleBooksResult(status="not_found", isbn13=queried_isbn13)
    if not isinstance(items, list):
        raise _MalformedGoogleBooksResponse("'items' is not a list")

    for item in items:
        if not isinstance(item, dict):
            continue
        volume_info = item.get("volumeInfo")
        if not isinstance(volume_info, dict):
            continue
        # `q=isbn:X` is not guaranteed to return only exact matches as
        # items[0] — confirm this item's own ISBN (decision 4 priority:
        # ISBN_13 first, else ISBN_10 converted) actually is the one we
        # queried before trusting its cover/categories for that ISBN.
        if _extract_isbn13(volume_info.get("industryIdentifiers")) != queried_isbn13:
            continue

        return GoogleBooksResult(
            status="found",
            isbn13=queried_isbn13,
            cover_image_url=_extract_cover_image_url(volume_info.get("imageLinks")),
            categories=_extract_categories(volume_info.get("categories")),
        )

    return GoogleBooksResult(status="not_found", isbn13=queried_isbn13)


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
            # design.md decision 12: IngestionFailure.message must never
            # contain the API key / a fully-keyed request URL. httpx
            # exception __str__ can embed the request (and its `key=`
            # query param), so only the exception *type* is safe to keep.
            error_message = f"timeout: {type(exc).__name__}"
        except httpx.RequestError as exc:
            # Only timeout/429/5xx are retryable (design.md decision 9);
            # any other network error fails fast.
            return GoogleBooksResult(
                status="failed",
                isbn13=isbn13,
                error_message=f"network error: {type(exc).__name__}",
            )
        else:
            if response.status_code == 200:
                try:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise _MalformedGoogleBooksResponse("response body is not a JSON object")
                    return _parse_found_response(payload, isbn13)
                except (
                    ValueError,  # json.JSONDecodeError subclasses ValueError
                    TypeError,
                    AttributeError,
                    _MalformedGoogleBooksResponse,
                ):
                    return GoogleBooksResult(
                        status="failed",
                        isbn13=isbn13,
                        error_message="invalid Google Books response",
                    )
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
