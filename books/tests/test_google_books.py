import httpx
import pytest
import respx

from books.services.schema import CategoryInput
from books.sources.google_books import GOOGLE_BOOKS_BASE_URL, query_google_books_by_isbn

API_KEY = "test-api-key"
ISBN = "9780306406157"
OTHER_ISBN = "9789571234564"
_MISSING = object()


def _volume(
    *,
    industry_identifiers=_MISSING,
    categories=None,
    image_links=None,
):
    # Defaults to an industryIdentifiers entry matching ISBN (module-level
    # query target) so tests about categories/cover/etc. don't have to
    # think about ISBN-matching unless that's what they're testing.
    if industry_identifiers is _MISSING:
        industry_identifiers = [{"type": "ISBN_13", "identifier": ISBN}]
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


class TestIsbnMatching:
    @respx.mock
    def test_prefers_isbn13_identifier_when_both_present_and_isbn13_matches(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [
                        _volume(
                            industry_identifiers=[
                                # ISBN_10 deliberately does NOT correspond
                                # to ISBN13 (different book) — if the
                                # implementation used ISBN_10 instead of
                                # preferring ISBN_13 it would still match
                                # here by coincidence, so this alone isn't
                                # sufficient; paired with the "isbn10-only"
                                # test below to pin down the real behaviour.
                                {"type": "ISBN_10", "identifier": "4006381330"},
                                {"type": "ISBN_13", "identifier": ISBN},
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
    def test_isbn10_only_identifier_normalizes_and_matches(self):
        # 0306406152 normalizes to 9780306406157 (== ISBN).
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_search_response(
                    [_volume(industry_identifiers=[{"type": "ISBN_10", "identifier": "0306406152"}])]
                ),
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"

    @respx.mock
    def test_first_item_isbn_mismatch_second_item_matches_uses_second(self):
        mismatched = _volume(
            industry_identifiers=[{"type": "ISBN_13", "identifier": OTHER_ISBN}],
            categories=["Wrong Book Category"],
        )
        matching = _volume(
            industry_identifiers=[{"type": "ISBN_13", "identifier": ISBN}],
            categories=["Correct Category"],
        )
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([mismatched, matching]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"
        assert result.categories == [CategoryInput(type="subject_tag", code="", label="Correct Category")]

    @respx.mock
    def test_all_items_isbn_mismatch_returns_not_found(self):
        mismatched = _volume(industry_identifiers=[{"type": "ISBN_13", "identifier": OTHER_ISBN}])
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([mismatched]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"
        assert result.categories == []
        assert result.cover_image_url is None

    @respx.mock
    def test_item_with_no_industry_identifiers_at_all_does_not_match(self):
        no_ids = _volume(industry_identifiers=None)
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([no_ids]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"


class TestSuccessfulLookup:

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


class TestMalformedResponse:
    # Acceptance criterion: "查無資料或請求失敗時回傳明確結果，不拋出未處理
    # 例外" — this must hold even when Google's 200 response body itself is
    # garbage, not just for the HTTP-status/timeout cases already covered.

    @respx.mock
    def test_invalid_json_body_returns_failed_not_raise(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, content=b"<html>temporary error</html>")
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        assert result.error_message

    @respx.mock
    def test_items_not_a_list_returns_failed_not_raise(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json={"kind": "books#volumes", "items": "oops"})
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"

    @respx.mock
    def test_item_that_is_not_a_dict_is_skipped_not_crashed(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response(["not-a-dict-item"]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"

    @respx.mock
    def test_item_missing_volume_info_key_is_skipped_not_crashed(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([{"id": "abc123"}]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"

    @respx.mock
    def test_item_with_volume_info_explicitly_none_is_skipped_not_crashed(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200, json=_search_response([{"id": "abc123", "volumeInfo": None}])
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"

    @respx.mock
    def test_volume_info_wrong_type_is_skipped_not_crashed(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(
                200, json=_search_response([{"id": "abc123", "volumeInfo": "not-a-dict"}])
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "not_found"

    @respx.mock
    def test_industry_identifiers_entry_that_is_not_a_dict_does_not_crash(self):
        volume = _volume(industry_identifiers=["not-a-dict", {"type": "ISBN_13", "identifier": ISBN}])
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([volume]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"

    @respx.mock
    def test_categories_not_a_list_is_treated_as_empty(self):
        volume = _volume(categories="not-a-list")
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([volume]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "found"
        assert result.categories == []

    @respx.mock
    def test_categories_are_stripped_deduped_and_empty_entries_dropped(self):
        volume = _volume(categories=["Fiction", " Fiction ", "", "  ", "Fantasy", 123])
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            return_value=httpx.Response(200, json=_search_response([volume]))
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.categories == [
            CategoryInput(type="subject_tag", code="", label="Fiction"),
            CategoryInput(type="subject_tag", code="", label="Fantasy"),
        ]


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

    @respx.mock
    def test_network_error_message_never_includes_the_api_key(self):
        # design.md decision 12: IngestionFailure.message must never
        # contain the API key or a fully-keyed request URL. httpx
        # exception __str__ can include the request (and therefore the
        # `key=` query param), so the adapter must not just str() it.
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            side_effect=httpx.ConnectError(
                f"Connection refused: {GOOGLE_BOOKS_BASE_URL}?q=isbn:{ISBN}&key={API_KEY}"
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        assert API_KEY not in result.error_message
        assert "key=" not in result.error_message

    @respx.mock
    def test_timeout_error_message_never_includes_the_api_key(self):
        respx.get(GOOGLE_BOOKS_BASE_URL).mock(
            side_effect=httpx.ReadTimeout(
                f"Timed out: {GOOGLE_BOOKS_BASE_URL}?q=isbn:{ISBN}&key={API_KEY}"
            )
        )
        with httpx.Client() as client:
            result = query_google_books_by_isbn(ISBN, client, API_KEY, sleep=_no_recording_sleep)

        assert result.status == "failed"
        assert API_KEY not in result.error_message
        assert "key=" not in result.error_message

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
