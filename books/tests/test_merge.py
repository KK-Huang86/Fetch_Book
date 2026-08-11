import pytest

from books.services.merge import ExistingBookState, ResolvedCategory, merge_book_fields
from books.services.schema import CategoryInput, GoogleBooksResult, ParsedBookRecord

NCL_CATEGORY = CategoryInput(type="classification_number", code="957.9", label="957.9")
NCL_SHELF = CategoryInput(type="shelf_category", code="", label="藝術")


def _ncl_record(**overrides):
    defaults = dict(
        isbn13="9786269935468",
        title="Bàn-tāi",
        authors=["吳宗岱"],
        publisher="島座放送",
        categories=[NCL_CATEGORY, NCL_SHELF],
    )
    defaults.update(overrides)
    return ParsedBookRecord(**defaults)


class TestBibliographicFieldsAlwaysFromNcl:
    def test_new_book_uses_ncl_title_authors_publisher(self):
        merged = merge_book_fields(_ncl_record(), existing=None)

        assert merged.title == "Bàn-tāi"
        assert merged.authors == ["吳宗岱"]
        assert merged.publisher == "島座放送"

    def test_authors_are_replaced_not_unioned(self):
        # design.md decision 5: authors 以最新 NCL 資料「覆蓋」M2M — there is
        # no "existing authors" concept for merge.py to consult at all.
        merged = merge_book_fields(_ncl_record(authors=["新作者"]), existing=None)
        assert merged.authors == ["新作者"]


class TestCategoryUnion:
    def test_new_book_categories_are_ncl_categories_tagged_source_ncl(self):
        merged = merge_book_fields(_ncl_record(), existing=None)

        assert merged.categories == [
            ResolvedCategory(source="ncl", type="classification_number", code="957.9", label="957.9"),
            ResolvedCategory(source="ncl", type="shelf_category", code="", label="藝術"),
        ]

    def test_existing_categories_are_kept_even_if_absent_from_this_run(self):
        existing = ExistingBookState(
            cover_image_url=None,
            cover_image_hosting=None,
            categories=[
                ResolvedCategory(source="ncl", type="shelf_category", code="", label="舊分類")
            ],
        )
        merged = merge_book_fields(_ncl_record(categories=[NCL_CATEGORY]), existing=existing)

        assert ResolvedCategory(source="ncl", type="shelf_category", code="", label="舊分類") in merged.categories
        assert ResolvedCategory(source="ncl", type="classification_number", code="957.9", label="957.9") in merged.categories
        assert len(merged.categories) == 2

    def test_duplicate_category_from_existing_and_new_is_not_duplicated(self):
        existing = ExistingBookState(
            cover_image_url=None,
            cover_image_hosting=None,
            categories=[ResolvedCategory(source="ncl", type="classification_number", code="957.9", label="957.9")],
        )
        merged = merge_book_fields(_ncl_record(categories=[NCL_CATEGORY]), existing=existing)

        assert merged.categories == [
            ResolvedCategory(source="ncl", type="classification_number", code="957.9", label="957.9")
        ]

    def test_google_categories_are_tagged_source_google_books_and_unioned(self):
        google_result = GoogleBooksResult(
            status="found",
            isbn13="9786269935468",
            categories=[CategoryInput(type="subject_tag", code="", label="Fiction")],
        )
        merged = merge_book_fields(_ncl_record(categories=[]), existing=None, google_result=google_result)

        assert merged.categories == [
            ResolvedCategory(source="google_books", type="subject_tag", code="", label="Fiction")
        ]

    def test_google_categories_ignored_when_status_not_found(self):
        google_result = GoogleBooksResult(
            status="not_found",
            isbn13="9786269935468",
            categories=[CategoryInput(type="subject_tag", code="", label="Should Not Appear")],
        )
        merged = merge_book_fields(_ncl_record(categories=[]), existing=None, google_result=google_result)
        assert merged.categories == []

    def test_google_categories_ignored_when_status_failed(self):
        google_result = GoogleBooksResult(
            status="failed", isbn13="9786269935468", error_message="HTTP 500"
        )
        merged = merge_book_fields(_ncl_record(categories=[]), existing=None, google_result=google_result)
        assert merged.categories == []

    def test_no_google_result_at_all_only_uses_ncl_categories(self):
        merged = merge_book_fields(_ncl_record(categories=[NCL_CATEGORY]), existing=None, google_result=None)
        assert merged.categories == [
            ResolvedCategory(source="ncl", type="classification_number", code="957.9", label="957.9")
        ]


class TestCoverImagePolicy:
    def test_new_book_with_no_google_result_has_no_cover(self):
        merged = merge_book_fields(_ncl_record(), existing=None)
        assert merged.cover_image_url is None
        assert merged.cover_image_hosting is None

    def test_new_book_uses_google_cover_when_found(self):
        google_result = GoogleBooksResult(
            status="found",
            isbn13="9786269935468",
            cover_image_url="https://books.google.com/cover.jpg",
        )
        merged = merge_book_fields(_ncl_record(), existing=None, google_result=google_result)
        assert merged.cover_image_url == "https://books.google.com/cover.jpg"
        assert merged.cover_image_hosting == "hotlink"

    def test_http_google_cover_url_is_upgraded_to_https(self):
        google_result = GoogleBooksResult(
            status="found",
            isbn13="9786269935468",
            cover_image_url="http://books.google.com/cover.jpg",
        )
        merged = merge_book_fields(_ncl_record(), existing=None, google_result=google_result)
        assert merged.cover_image_url == "https://books.google.com/cover.jpg"

    def test_existing_non_empty_cover_is_not_overwritten_by_google(self):
        existing = ExistingBookState(
            cover_image_url="https://cdn.example.com/existing.jpg",
            cover_image_hosting="self",
            categories=[],
        )
        google_result = GoogleBooksResult(
            status="found",
            isbn13="9786269935468",
            cover_image_url="https://books.google.com/new-cover.jpg",
        )
        merged = merge_book_fields(_ncl_record(), existing=existing, google_result=google_result)

        assert merged.cover_image_url == "https://cdn.example.com/existing.jpg"
        assert merged.cover_image_hosting == "self"

    def test_existing_http_cover_is_kept_as_is_not_retroactively_upgraded(self):
        # design.md decision 5: an existing non-empty value is never
        # touched, even if it happens to be http:// from before this rule
        # existed — only *new* writes get the https upgrade.
        existing = ExistingBookState(
            cover_image_url="http://cdn.example.com/existing.jpg",
            cover_image_hosting="self",
            categories=[],
        )
        merged = merge_book_fields(_ncl_record(), existing=existing, google_result=None)
        assert merged.cover_image_url == "http://cdn.example.com/existing.jpg"

    def test_existing_empty_cover_gets_filled_from_google(self):
        existing = ExistingBookState(cover_image_url=None, cover_image_hosting=None, categories=[])
        google_result = GoogleBooksResult(
            status="found", isbn13="9786269935468", cover_image_url="https://books.google.com/cover.jpg"
        )
        merged = merge_book_fields(_ncl_record(), existing=existing, google_result=google_result)
        assert merged.cover_image_url == "https://books.google.com/cover.jpg"
        assert merged.cover_image_hosting == "hotlink"

    def test_google_found_but_no_cover_image_url_leaves_cover_empty(self):
        google_result = GoogleBooksResult(status="found", isbn13="9786269935468", cover_image_url=None)
        merged = merge_book_fields(_ncl_record(), existing=None, google_result=google_result)
        assert merged.cover_image_url is None
        assert merged.cover_image_hosting is None


class TestGoogleResultIsbnMustMatchNclRecord:
    def test_mismatched_isbn_with_status_found_raises_value_error(self):
        # Defends the boundary where two sources' data actually gets
        # combined: a wiring bug elsewhere (wrong ISBN passed to the
        # Google query) must not silently attach book A's cover/category
        # to book B's NCL record.
        google_result = GoogleBooksResult(
            status="found",
            isbn13="9789571234564",  # different from _ncl_record()'s ISBN
            cover_image_url="https://books.google.com/cover.jpg",
        )
        with pytest.raises(ValueError):
            merge_book_fields(_ncl_record(), existing=None, google_result=google_result)

    def test_matching_isbn_with_status_found_does_not_raise(self):
        google_result = GoogleBooksResult(status="found", isbn13="9786269935468")
        merge_book_fields(_ncl_record(), existing=None, google_result=google_result)  # no raise

    def test_mismatched_isbn_with_status_not_found_does_not_raise(self):
        # Only a "found" result with a wrong ISBN is a wiring bug; a
        # not_found/failed result's isbn13 field isn't being trusted for
        # any data anyway.
        google_result = GoogleBooksResult(status="not_found", isbn13="9789571234564")
        merge_book_fields(_ncl_record(), existing=None, google_result=google_result)  # no raise
