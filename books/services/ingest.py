from __future__ import annotations

import re
from collections import Counter

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
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
from books.sources.google_books import query_google_books_by_isbn
from books.sources.ncl import NclDownloadError, NclNotFoundError, download_ncl_csv, parse_ncl_csv

# design.md decision 12: IngestionFailure.message 不得包含 API 金鑰或帶金鑰
# 參數的完整請求網址. This is a defense-in-depth *redaction* at the write
# layer, not the primary sanitization (sources like google_books.py are
# responsible for not constructing such a message in the first place) and
# not a rejection either — this function is normally called from
# exception-handling paths, so it must never itself raise on a bad
# message, or "record this one failure" turns into "abort the batch".
_SENSITIVE_QUERY_PARAM_PATTERN = re.compile(r"(?i)\b(api[_-]?key|key)\s*=\s*[^&\s]+")


def _redact_sensitive_query_params(message: str) -> str:
    return _SENSITIVE_QUERY_PARAM_PATTERN.sub(r"\1=[REDACTED]", message)


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


def _get_or_create_publisher(name: str) -> Publisher | None:
    if not name:
        return None
    publisher, _ = Publisher.objects.get_or_create(name=name)
    return publisher


def _write_book_fields(book: Book, merged, publisher: Publisher | None) -> None:
    book.title = merged.title
    book.publisher = publisher
    book.cover_image_url = merged.cover_image_url
    book.cover_image_hosting = merged.cover_image_hosting
    book.save()


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
        publisher = _get_or_create_publisher(merged.publisher)

        if book is None:
            try:
                # Nested atomic() = savepoint: on IntegrityError only this
                # insert rolls back, not the whole outer transaction, so
                # we can keep querying afterwards.
                with transaction.atomic():
                    book = Book.objects.create(
                        isbn13=ncl_record.isbn13,
                        title=merged.title,
                        publisher=publisher,
                        cover_image_url=merged.cover_image_url,
                        cover_image_hosting=merged.cover_image_hosting,
                        first_seen_month=month,
                    )
            except IntegrityError:
                # select_for_update() can't lock a row that doesn't exist
                # — we lost a race with a concurrent insert of the same
                # new ISBN. Lock and re-merge against the winner's actual
                # state (not our stale "existing=None" assumption), then
                # update it instead of failing this run.
                book = Book.objects.select_for_update().get(isbn13=ncl_record.isbn13)
                existing_state = _read_existing_state(book)
                merged = merge_book_fields(ncl_record, existing_state, google_result)
                publisher = _get_or_create_publisher(merged.publisher)
                _write_book_fields(book, merged, publisher)
        else:
            _write_book_fields(book, merged, publisher)

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
    safe_message = _redact_sensitive_query_params(message)
    return IngestionFailure.objects.create(
        run=run, isbn=isbn, stage=stage, error_code=error_code, message=safe_message
    )


def _record_duplicate_isbn_warnings(run: IngestionRun, records: list[ParsedBookRecord]) -> None:
    # design.md decision 4: 同一來源同一 ISBN 於同一次 CSV 中重複出現，以
    # 最後一筆為準（already the natural effect of upserting each occurrence
    # in order — no special-casing needed for that part）並記錄一筆 warning。
    isbn_counts = Counter(r.isbn13 for r in records)
    for isbn13, count in isbn_counts.items():
        if count > 1:
            record_ingestion_failure(
                run,
                stage=IngestionFailure.Stage.NCL_PARSE,
                error_code="duplicate_isbn_in_csv",
                message=f"ISBN appeared {count} times in this month's CSV; using the last occurrence",
                isbn=isbn13,
            )


def ingest_month(month: str, trigger_type: str) -> IngestionRun:
    """design.md decision 1/10/11: framework-agnostic core of both the
    management command (manual) and the Celery task (scheduled) — neither
    re-implements this flow, they only differ in trigger_type and how
    they're invoked. NCL download → parse → per-book enrichment
    idempotency check → optional Google Books lookup → upsert, with a
    single IngestionRun tracking the whole batch and per-row
    IngestionFailure entries for anything that didn't make it in.
    """
    run = start_ingestion_run(month, trigger_type)

    # Declared before the try so the outer except (anything NOT already
    # handled by the narrower except clauses below) can still report
    # however far the batch actually got, instead of always 0/0/0/0.
    total = 0
    succeeded = 0
    failed = 0
    google_enriched = 0

    try:
        with httpx.Client() as client:
            try:
                raw_csv = download_ncl_csv(month, client)
            except NclNotFoundError as exc:
                if trigger_type == IngestionRun.TriggerType.SCHEDULED:
                    # design.md decision 10: not a failure when scheduled —
                    # the Celery task (seam 8) will simply try again tomorrow.
                    finish_ingestion_run(
                        run,
                        status=IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED,
                        total=0,
                        succeeded=0,
                        failed=0,
                        google_enriched=0,
                    )
                else:
                    # design.md decision 10: manual trigger picked this
                    # month on purpose, so 404 must be an explicit error.
                    record_ingestion_failure(
                        run,
                        stage=IngestionFailure.Stage.NCL_DOWNLOAD,
                        error_code="not_yet_published",
                        message=str(exc),
                    )
                    finish_ingestion_run(
                        run,
                        status=IngestionRun.Status.FAILED,
                        total=0,
                        succeeded=0,
                        failed=1,
                        google_enriched=0,
                    )
                return run
            except NclDownloadError as exc:
                record_ingestion_failure(
                    run,
                    stage=IngestionFailure.Stage.NCL_DOWNLOAD,
                    error_code=type(exc).__name__,
                    message=str(exc),
                )
                finish_ingestion_run(
                    run,
                    status=IngestionRun.Status.FAILED,
                    total=0,
                    succeeded=0,
                    failed=1,
                    google_enriched=0,
                )
                return run

            records, parse_failures = parse_ncl_csv(raw_csv)
            # Set as soon as it's knowable, not after the book loop — a
            # fatal error partway through must still report the real
            # total (see the outer except below).
            total = len(records) + len(parse_failures)

            for failure in parse_failures:
                record_ingestion_failure(
                    run,
                    stage=IngestionFailure.Stage.NCL_PARSE,
                    error_code=failure.error_code,
                    message=failure.message,
                    isbn=failure.isbn,
                )
            _record_duplicate_isbn_warnings(run, records)

            failed = len(parse_failures)
            api_key = settings.GOOGLE_BOOKS_API_KEY

            for record in records:
                google_result: GoogleBooksResult | None = None
                if should_query_google_books(record.isbn13):
                    google_result = query_google_books_by_isbn(record.isbn13, client, api_key)
                    if google_result.status == "found":
                        google_enriched += 1
                    elif google_result.status == "failed":
                        # design.md spec "Google Books 查無對應資料": keep
                        # the NCL data, record the lookup failure, don't
                        # abort.
                        record_ingestion_failure(
                            run,
                            stage=IngestionFailure.Stage.GOOGLE_LOOKUP,
                            error_code=google_result.error_message or "unknown_error",
                            message=google_result.error_message or "",
                            isbn=record.isbn13,
                        )

                try:
                    upsert_book(record, month, google_result)
                    succeeded += 1
                except Exception as exc:  # noqa: BLE001 — boundary: one
                    # bad book must not abort the rest of the batch.
                    failed += 1
                    record_ingestion_failure(
                        run,
                        stage=IngestionFailure.Stage.BOOK_UPSERT,
                        error_code=type(exc).__name__,
                        message=str(exc),
                        isbn=record.isbn13,
                    )

        if total == 0 or failed == 0:
            status = IngestionRun.Status.SUCCEEDED
        elif succeeded == 0:
            status = IngestionRun.Status.FAILED
        else:
            status = IngestionRun.Status.PARTIALLY_FAILED

        finish_ingestion_run(
            run,
            status=status,
            total=total,
            succeeded=succeeded,
            failed=failed,
            google_enriched=google_enriched,
        )
        return run
    except Exception as exc:
        # Anything not already handled above (CSV decode errors, a DB
        # error from should_query_google_books, a genuine bug in an
        # adapter, ...) must not leave this run stuck at status='running'
        # forever (spec: 非預期錯誤 MUST 標記為失敗並保留可查詢的失敗紀
        # 錄). Re-raise afterwards so a manual command can report exit 1
        # and a future Celery task (issue #8) can still retry.
        #
        # v1 has no separate skipped/aborted counter: every row not
        # already confirmed succeeded counts as failed, so
        # total == succeeded + failed still holds even on a fatal
        # mid-batch abort (total was set as soon as parsing finished,
        # above — not left at its initial 0).
        try:
            record_ingestion_failure(
                run,
                stage=IngestionFailure.Stage.INGESTION,
                error_code=type(exc).__name__,
                message=str(exc),
            )
        finally:
            # Best-effort: if even *recording* the failure raised (e.g.
            # the DB write itself is what's broken), still try to close
            # out the run rather than leaving it running on top of that.
            # This can't be a hard guarantee under a total DB outage.
            finish_ingestion_run(
                run,
                status=IngestionRun.Status.FAILED,
                total=total,
                succeeded=succeeded,
                failed=max(total - succeeded, 1),
                google_enriched=google_enriched,
            )
        raise
