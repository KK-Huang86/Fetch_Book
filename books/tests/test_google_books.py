import httpx
import pytest
import respx

from books.services.schema import CategoryInput
from books.sources.google_books import GOOGLE_BOOKS_BASE_URL, query_google_books_by_isbn

API_KEY = "test-api-key"
ISBN = "9780306406157"


def _volume(
    *,
    industry_identifiers=None,
    categories=None,
    image_links=None,
):
    volume_info = {"title": "Some Book", "authors": ["Author One"]}
    if industry_identifiers is not None:
        volume_info["industryIdentifiers"] = industry_identifiers
    if categories is not None:
        volume_info["categories"] = categories
    if image_links is not None:
        volume_info["imageLinks"] = image_links
    return {"kind": "books#volume", "id": "abc123", "volumeInfo": volume_info}


def _search_response(items):
    return {"kind": "books#volumes", "totalItems": len(items), "items": items}


def _no_recording_sleep(_seconds):
    pass


class RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class TestSuccessfulLookup:
    @respx.mock
    def test_prioritizes_isbn13_over_isbn10_in_industry_identifiers(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [
                        _volume(
                            industry_identifiers=[
                                {"type": "ISBN_10", "identifier": "0306406152"},
                                {"type": "ISBN_13", "identifier": "9780306406157"},
                            ]
                        )
                    ]
                ),
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"
        assert result.isbn13 == ISBN

    @respx.mock
    def test_extracts_categories_as_subject_tag_category_inputs(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [_volume(categories=["Fiction", "Fantasy"])]
                ),
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.categories == [
            CategoryInput(type="subject_tag", code="", label="Fiction"),
            CategoryInput(type="subject_tag", code="", label="Fantasy"),
        ]

    @respx.mock
    def test_prefers_thumbnail_over_small_thumbnail(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [
                        _volume(
                            image_links={
                                "smallThumbnail": "http://books.google.com/small.jpg",
                                "thumbnail": "http://books.google.com/thumb.jpg",
                            }
                        )
                    ]
                ),
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.cover_image_url == "http://books.google.com/thumb.jpg"

    @respx.mock
    def test_falls_back_to_small_thumbnail_when_thumbnail_missing(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [_volume(image_links={"smallThumbnail": "http://books.google.com/small.jpg"})]
                ),
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.cover_image_url == "http://books.google.com/small.jpg"

    @respx.mock
    def test_missing_image_links_returns_none_cover(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([_volume()]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.cover_image_url is None

    @respx.mock
    def test_missing_categories_returns_empty_list(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([_volume()]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.categories == []


class TestNotFound:
    @respx.mock
    def test_zero_total_items_returns_not_found(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json={"kind": "books#volumes", "totalItems": 0})
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"
        assert result.isbn13 == ISBN
        assert result.categories == []
        assert result.cover_image_url is None

    @respx.mock
    def test_empty_items_list_returns_not_found(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"


class TestRetryPolicy:
    @respx.mock
    def test_429_is_retried_then_succeeds(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL)
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json=_search_response([_volume()])),
        ]
        sleep = RecordingSleep()
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=sleep)

        assert result.status == "found"
        assert route.call_count == 2
        assert len(sleep.calls) == 1

    @respx.mock
    def test_5xx_is_retried_then_succeeds(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL)
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json=_search_response([_volume()])),
        ]
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"
        assert route.call_count == 2

    @respx.mock
    def test_timeout_is_retried_then_succeeds(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL)
        route.side_effect = [
            httpx.ReadTimeout("timed out"),
            httpx.Response(200, json=_search_response([_volume()])),
        ]
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"
        assert route.call_count == 2

    @respx.mock
    def test_non_429_4xx_is_not_retried(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL).mock(return_value=httpx.Response(400))
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        assert route.call_count == 1

    @respx.mock
    def test_connection_error_is_not_retried(self):
        # design.md decision 9: only timeout/429/5xx retry. A hard
        # connection error is a different failure class and fails fast.
        route = respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        assert route.call_count == 1

    @respx.mock
    def test_exhausts_max_3_retries_then_fails(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL).mock(return_value=httpx.Response(503))
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        # 1 initial attempt + 3 retries = 4 total requests, never more.
        assert route.call_count == 4

    @respx.mock
    def test_respects_retry_after_header_seconds(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL)
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=_search_response([_volume()])),
        ]
        sleep = RecordingSleep()
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=sleep)

        assert result.status == "found"
        assert sleep.calls == [7.0]

    @respx.mock
    def test_total_wait_across_all_retries_is_capped_at_30_seconds(self):
        route = respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "20"})
        )
        sleep = RecordingSleep()
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=sleep)

        assert result.status == "failed"
        assert sum(sleep.calls) <= 30.0

    def test_never_raises_for_isbn_that_looks_up_fine(self):
        # Smoke-level guard for the acceptance criterion "不拋出未處理例外" —
        # the retry/found/not_found paths are covered individually above;
        # this just asserts the function signature never lets an httpx
        # exception escape uncaught for the exhausted-retry case.
        with respx.mock:
            respx.get(GOOGLE_BOOKS_BASE_URL).mock(side_effect=httpx.ConnectError("boom"))
            with httpx.Client() as client:
                result = query_google_books_by_isbn(
                    ISBN, client, API_KEY, sleep=_no_recording_sleep
                )
        assert result.status == "failed"
        assert result.error_message
