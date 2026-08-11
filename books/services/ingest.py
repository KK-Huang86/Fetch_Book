from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from books.models import (
    Author,
    Book,
    BookAuthor,
    BookCategory,
    Category,
    IngestionFailure,
    IngestionRun,
    Publisher,
)
from books.services.merge import ExistingBookState, ResolvedCategory, merge_book_fields
from books.services.schema import GoogleBooksResult, ParsedBookRecord

# design.md decision 12: IngestionFailure.message 不得包含 API 金鑰或帶金鑰
# 參數的完整請求網址. This is a defense-in-depth guard at the write layer,
# not the primary sanitization — sources (e.g. google_books.py) are
# responsible for not constructing such a message in the first place.
_FORBIDDEN_MESSAGE_PATTERN = re.compile(r"key=", re.IGNORECASE)


def _read_existing_state(book: Book) -> ExistingBookState:
    categories = [
        ResolvedCategory(source=c.source, type=c.type, code=c.code, label=c.label)
        for c in book.categories.all()
    ]
    return ExistingBookState(
        cover_image_url=book.cover_image_url,
        cover_image_hosting=book.cover_image_hosting,
        categories=categories,
    )


def _apply_authors(book: Book, author_names: list[str]) -> None:
    # design.md decision 5: authors are rebuilt from NCL's latest data,
    # not accumulated — clear this book's links (not the Author rows
    # themselves, which other books may still reference) and relink.
    BookAuthor.objects.filter(book=book).delete()
    for name in author_names:
        author, _ = Author.objects.get_or_create(name=name)
        BookAuthor.objects.get_or_create(book=book, author=author)


def _apply_categories(book: Book, categories: list[ResolvedCategory]) -> None:
    # merge_book_fields already computed the full union (existing + new);
    # linking is additive/idempotent here, never removes a link.
    for resolved in categories:
        category, _ = Category.objects.get_or_create(
            source=resolved.source,
            type=resolved.type,
            code=resolved.code,
            label=resolved.label,
        )
        BookCategory.objects.get_or_create(book=book, category=category)


def should_query_google_books(isbn13: str) -> bool:
    """design.md decision 6: skip the Google Books lookup only if this
    ISBN already has both a non-empty cover AND a google_books-sourced
    category; otherwise (including "not in the DB yet") query it."""
    book = Book.objects.filter(isbn13=isbn13).first()
    if book is None:
        return True

    has_cover = bool(book.cover_image_url)
    has_google_category = book.categories.filter(source=Category.Source.GOOGLE_BOOKS).exists()
    return not (has_cover and has_google_category)


def upsert_book(
    ncl_record: ParsedBookRecord,
    month: str,
    google_result: GoogleBooksResult | None = None,
) -> Book:
    """design.md decision 5. `isbn13` is the upsert key. One atomic block
    per book — a failure here rolls back only this book; seam 7 catches
    the exception per-book so it doesn't abort the rest of the batch."""
    with transaction.atomic():
        book = Book.objects.select_for_update().filter(isbn13=ncl_record.isbn13).first()
        existing_state = _read_existing_state(book) if book else None
        merged = merge_book_fields(ncl_record, existing_state, google_result)

        publisher = None
        if merged.publisher:
            publisher, _ = Publisher.objects.get_or_create(name=merged.publisher)

        if book is None:
            book = Book.objects.create(
                isbn13=ncl_record.isbn13,
                title=merged.title,
                publisher=publisher,
                cover_image_url=merged.cover_image_url,
                cover_image_hosting=merged.cover_image_hosting,
                first_seen_month=month,
            )
        else:
            book.title = merged.title
            book.publisher = publisher
            book.cover_image_url = merged.cover_image_url
            book.cover_image_hosting = merged.cover_image_hosting
            book.save()

        _apply_authors(book, merged.authors)
        _apply_categories(book, merged.categories)

        return book


def start_ingestion_run(month: str, trigger_type: str) -> IngestionRun:
    return IngestionRun.objects.create(
        month=month,
        trigger_type=trigger_type,
        started_at=timezone.now(),
        status=IngestionRun.Status.RUNNING,
    )


def finish_ingestion_run(
    run: IngestionRun,
    status: str,
    total: int,
    succeeded: int,
    failed: int,
    google_enriched: int,
) -> None:
    run.status = status
    run.total = total
    run.succeeded = succeeded
    run.failed = failed
    run.google_enriched = google_enriched
    run.finished_at = timezone.now()
    run.save()


def record_ingestion_failure(
    run: IngestionRun,
    stage: str,
    error_code: str,
    message: str,
    isbn: str | None = None,
) -> IngestionFailure:
    if _FORBIDDEN_MESSAGE_PATTERN.search(message):
        raise ValueError(
            "IngestionFailure.message must not contain an API key "
            "(design.md decision 12) — sanitize the message before recording it"
        )
    return IngestionFailure.objects.create(
        run=run, isbn=isbn, stage=stage, error_code=error_code, message=message
    )
