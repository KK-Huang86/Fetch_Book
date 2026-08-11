from unittest.mock import MagicMock, patch

import pytest
from django.db import DatabaseError, IntegrityError

from books.models import Author, Book, BookAuthor, Category
from books.services.ingest import upsert_book
from books.services.schema import CategoryInput, GoogleBooksResult, ParsedBookRecord

ISBN = "9786269935468"


def _ncl_record(**overrides):
    defaults = dict(
        isbn13=ISBN,
        title="Bàn-tāi",
        authors=["吳宗岱"],
        publisher="島座放送",
        categories=[CategoryInput(type="classification_number", code="957.9", label="957.9")],
    )
    defaults.update(overrides)
    return ParsedBookRecord(**defaults)


@pytest.mark.django_db
class TestFirstInsert:
    def test_creates_new_book_with_ncl_fields(self):
        book = upsert_book(_ncl_record(), month="2025-08")

        assert book.isbn13 == ISBN
        assert book.title == "Bàn-tāi"
        assert book.publisher.name == "島座放送"
        assert [a.name for a in book.authors.all()] == ["吳宗岱"]
        assert book.first_seen_month == "2025-08"

    def test_empty_publisher_string_leaves_publisher_null(self):
        book = upsert_book(_ncl_record(publisher=""), month="2025-08")
        assert book.publisher is None

    def test_ncl_categories_are_linked_with_source_ncl(self):
        book = upsert_book(_ncl_record(), month="2025-08")
        categories = list(book.categories.all())
        assert len(categories) == 1
        assert categories[0].source == Category.Source.NCL
        assert categories[0].type == Category.Type.CLASSIFICATION_NUMBER
        assert categories[0].code == "957.9"


@pytest.mark.django_db
class TestRepeatedUpsertBySameIsbn:
    def test_does_not_create_a_duplicate_row(self):
        upsert_book(_ncl_record(), month="2025-08")
        upsert_book(_ncl_record(), month="2025-09")
        assert Book.objects.filter(isbn13=ISBN).count() == 1

    def test_updates_title_from_latest_ncl_data(self):
        upsert_book(_ncl_record(title="舊書名"), month="2025-08")
        book = upsert_book(_ncl_record(title="新書名"), month="2025-09")
        assert book.title == "新書名"

    def test_first_seen_month_is_set_only_on_first_insert(self):
        upsert_book(_ncl_record(), month="2025-07")
        book = upsert_book(_ncl_record(), month="2025-09")
        assert book.first_seen_month == "2025-07"

    def test_categories_accumulate_across_upserts(self):
        upsert_book(
            _ncl_record(categories=[CategoryInput(type="classification_number", code="957.9", label="957.9")]),
            month="2025-08",
        )
        book = upsert_book(
            _ncl_record(categories=[CategoryInput(type="shelf_category", code="", label="藝術")]),
            month="2025-09",
        )
        labels = {(c.type, c.label) for c in book.categories.all()}
        assert labels == {("classification_number", "957.9"), ("shelf_category", "藝術")}

    def test_authors_are_rebuilt_not_accumulated(self):
        upsert_book(_ncl_record(authors=["舊作者"]), month="2025-08")
        book = upsert_book(_ncl_record(authors=["新作者"]), month="2025-09")

        assert [a.name for a in book.authors.all()] == ["新作者"]
        # The old author row itself isn't deleted (may be referenced by
        # other books) — only this book's link to it is removed.
        assert Author.objects.filter(name="舊作者").exists()
        assert not BookAuthor.objects.filter(book=book, author__name="舊作者").exists()

    def test_existing_non_empty_cover_is_preserved_across_upserts(self):
        first_google_result = GoogleBooksResult(
            status="found", isbn13=ISBN, cover_image_url="https://books.google.com/first.jpg"
        )
        upsert_book(_ncl_record(), month="2025-08", google_result=first_google_result)

        second_google_result = GoogleBooksResult(
            status="found", isbn13=ISBN, cover_image_url="https://books.google.com/second.jpg"
        )
        book = upsert_book(_ncl_record(), month="2025-09", google_result=second_google_result)

        assert book.cover_image_url == "https://books.google.com/first.jpg"


@pytest.mark.django_db
class TestConcurrentInsertRace:
    # select_for_update() can't lock a row that doesn't exist yet, so a
    # real race is: our SELECT finds nothing, a concurrent process commits
    # the same new ISBN, then OUR OWN create() hits the unique constraint.
    #
    # To simulate this with a real (not faked) IntegrityError: the winner
    # row is genuinely created first, then the *first* call to
    # select_for_update() within upsert_book is forced to act as if it
    # found nothing (as it would have, a moment earlier) while every
    # subsequent call — the one in the except-block recovery path — goes
    # through untouched to the real manager, which does see the winner.
    def _force_first_select_for_update_to_find_nothing(self):
        real_select_for_update = Book.objects.select_for_update
        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                empty_queryset = MagicMock()
                empty_queryset.filter.return_value.first.return_value = None
                return empty_queryset
            return real_select_for_update(*args, **kwargs)

        return patch("books.services.ingest.Book.objects.select_for_update", side_effect=_side_effect)

    def test_integrity_error_on_create_recovers_by_locking_and_updating_the_winner(self):
        Book.objects.create(isbn13=ISBN, title="Winner's Title", first_seen_month="2025-07")

        with self._force_first_select_for_update_to_find_nothing():
            book = upsert_book(_ncl_record(title="Our Title"), month="2025-08")

        assert Book.objects.filter(isbn13=ISBN).count() == 1
        # Recovery still applies the merge rules against the winner's
        # actual row, not a stale "existing=None" assumption.
        assert book.title == "Our Title"
        assert book.first_seen_month == "2025-07"

    def test_race_recovery_still_respects_existing_non_empty_cover(self):
        Book.objects.create(
            isbn13=ISBN,
            title="Winner's Title",
            first_seen_month="2025-07",
            cover_image_url="https://cdn.example.com/winner-cover.jpg",
            cover_image_hosting="self",
        )
        google_result = GoogleBooksResult(
            status="found", isbn13=ISBN, cover_image_url="https://books.google.com/new.jpg"
        )

        with self._force_first_select_for_update_to_find_nothing():
            book = upsert_book(_ncl_record(), month="2025-08", google_result=google_result)

        assert book.cover_image_url == "https://cdn.example.com/winner-cover.jpg"


@pytest.mark.django_db
class TestAtomicRollback:
    def test_failure_partway_through_does_not_leave_a_partial_book_row(self):
        with patch(
            "books.services.ingest._apply_categories",
            side_effect=DatabaseError("simulated failure"),
        ):
            with pytest.raises(DatabaseError):
                upsert_book(_ncl_record(), month="2025-08")

        assert not Book.objects.filter(isbn13=ISBN).exists()
