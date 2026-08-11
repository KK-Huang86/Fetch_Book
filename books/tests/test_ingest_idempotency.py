import pytest

from books.models import Book, BookCategory, Category
from books.services.ingest import should_query_google_books

ISBN = "9786269935468"


def _make_book(**overrides):
    defaults = dict(isbn13=ISBN, title="Some Book", first_seen_month="2025-08")
    defaults.update(overrides)
    return Book.objects.create(**defaults)


def _link_category(book, source):
    category = Category.objects.create(
        source=source, type=Category.Type.SUBJECT_TAG, code="", label="Fiction"
    )
    BookCategory.objects.create(book=book, category=category)


@pytest.mark.django_db
class TestShouldQueryGoogleBooks:
    def test_isbn_not_in_db_yet_should_query(self):
        assert should_query_google_books(ISBN) is True

    def test_has_cover_and_google_books_category_should_skip(self):
        book = _make_book(cover_image_url="https://example.com/cover.jpg")
        _link_category(book, Category.Source.GOOGLE_BOOKS)

        assert should_query_google_books(ISBN) is False

    def test_has_cover_but_no_google_books_category_should_query(self):
        _make_book(cover_image_url="https://example.com/cover.jpg")
        assert should_query_google_books(ISBN) is True

    def test_has_google_books_category_but_no_cover_should_query(self):
        book = _make_book(cover_image_url=None)
        _link_category(book, Category.Source.GOOGLE_BOOKS)

        assert should_query_google_books(ISBN) is True

    def test_has_neither_cover_nor_google_books_category_should_query(self):
        _make_book(cover_image_url=None)
        assert should_query_google_books(ISBN) is True

    def test_ncl_only_category_does_not_count_as_google_books_coverage(self):
        book = _make_book(cover_image_url="https://example.com/cover.jpg")
        _link_category(book, Category.Source.NCL)

        assert should_query_google_books(ISBN) is True
