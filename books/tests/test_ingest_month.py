from pathlib import Path
from unittest.mock import patch

import pytest

from books.models import Book, IngestionFailure, IngestionRun
from books.services.ingest import ingest_month
from books.services.schema import CategoryInput, GoogleBooksResult
from books.sources.ncl import NclDownloadTimeoutError, NclNotFoundError

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _found(isbn13: str, **overrides) -> GoogleBooksResult:
    # Includes a category by default so this represents a *fully* enriched
    # result (cover + a google_books category) — should_query_google_books
    # requires both, per design.md decision 6.
    defaults = dict(
        status="found",
        isbn13=isbn13,
        cover_image_url="https://books.google.com/x.jpg",
        categories=[CategoryInput(type="subject_tag", code="", label="Fiction")],
    )
    defaults.update(overrides)
    return GoogleBooksResult(**defaults)


def _not_found(isbn13: str) -> GoogleBooksResult:
    return GoogleBooksResult(status="not_found", isbn13=isbn13)


def _failed(isbn13: str, message: str = "HTTP 500") -> GoogleBooksResult:
    return GoogleBooksResult(status="failed", isbn13=isbn13, error_message=message)


@pytest.mark.django_db
class TestSuccessfulRun:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_all_valid_rows_are_upserted_and_run_marked_succeeded(self, mock_download, mock_google):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _found(isbn13)

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.SUCCEEDED
        assert run.total == 4
        assert run.succeeded == 4
        assert run.failed == 0
        assert run.google_enriched == 4
        assert Book.objects.count() == 4

    @patch("books.services.ingest.download_ncl_csv")
    def test_empty_csv_is_succeeded_with_zero_total(self, mock_download):
        mock_download.return_value = _read("ncl_empty.csv")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.SUCCEEDED
        assert run.total == 0
        assert Book.objects.count() == 0


@pytest.mark.django_db
class TestParseFailuresDoNotAbortTheBatch:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_bad_rows_interleaved_with_good_rows_still_upserts_the_good_ones(
        self, mock_download, mock_google
    ):
        mock_download.return_value = _read("ncl_mixed_batch.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _found(isbn13)

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.PARTIALLY_FAILED
        assert run.total == 4  # 2 good + 2 bad rows in ncl_mixed_batch.csv
        assert run.succeeded == 2
        assert run.failed == 2
        assert Book.objects.count() == 2
        assert run.failures.filter(stage=IngestionFailure.Stage.NCL_PARSE).count() == 2

    @patch("books.services.ingest.download_ncl_csv")
    def test_all_rows_invalid_results_in_failed_status(self, mock_download):
        mock_download.return_value = _read("ncl_missing_isbn.csv")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.FAILED
        assert run.succeeded == 0
        assert run.failed == 1


@pytest.mark.django_db
class TestDuplicateIsbnWithinSameCsv:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_last_occurrence_wins_and_one_warning_is_recorded(self, mock_download, mock_google):
        mock_download.return_value = _read("ncl_duplicate_isbn.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _not_found(isbn13)

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert Book.objects.count() == 1
        book = Book.objects.get(isbn13="9786269935468")
        assert book.title == "Bàn-tāi (更正書名)"  # the later row in the CSV
        duplicate_warnings = run.failures.filter(error_code="duplicate_isbn_in_csv")
        assert duplicate_warnings.count() == 1
        assert duplicate_warnings.first().isbn == "9786269935468"


@pytest.mark.django_db
class TestEnrichmentIdempotencyAcrossFullFlow:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_already_enriched_book_is_not_queried_again(self, mock_download, mock_google):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _found(isbn13)

        ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)
        assert mock_google.call_count == 4

        mock_google.reset_mock()
        ingest_month("2025-09", trigger_type=IngestionRun.TriggerType.SCHEDULED)
        # All 4 books already have a cover + a google_books category from
        # the first run — none should be queried again.
        assert mock_google.call_count == 0


@pytest.mark.django_db
class TestGoogleLookupFailureDoesNotAbortTheBatch:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_book_still_upserted_with_ncl_only_data_and_failure_recorded(
        self, mock_download, mock_google
    ):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _failed(isbn13, "HTTP 500")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert Book.objects.count() == 4
        assert run.succeeded == 4  # NCL data alone is still a successful upsert
        assert run.google_enriched == 0
        assert run.failures.filter(stage=IngestionFailure.Stage.GOOGLE_LOOKUP).count() == 4
        book = Book.objects.get(isbn13="9786269935468")
        assert book.cover_image_url is None


@pytest.mark.django_db
class TestBookUpsertFailureDoesNotAbortTheBatch:
    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_one_upsert_exception_still_lets_the_rest_succeed(self, mock_download, mock_google):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_google.side_effect = lambda isbn13, *a, **kw: _found(isbn13)

        from books.services import ingest as ingest_module

        real_upsert_book = ingest_module.upsert_book

        def _flaky_upsert(ncl_record, month, google_result=None):
            if ncl_record.isbn13 == "9786269935468":
                raise RuntimeError("simulated DB failure")
            return real_upsert_book(ncl_record, month, google_result)

        with patch("books.services.ingest.upsert_book", side_effect=_flaky_upsert):
            run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.PARTIALLY_FAILED
        assert run.succeeded == 3
        assert run.failed == 1
        assert Book.objects.count() == 3
        assert run.failures.filter(stage=IngestionFailure.Stage.BOOK_UPSERT).count() == 1


@pytest.mark.django_db
class TestNclNotYetPublished:
    @patch("books.services.ingest.download_ncl_csv")
    def test_scheduled_trigger_is_skipped_not_failed(self, mock_download):
        mock_download.side_effect = NclNotFoundError("2025-08 not yet published")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED
        assert run.failures.count() == 0

    @patch("books.services.ingest.download_ncl_csv")
    def test_manual_trigger_is_an_explicit_failure(self, mock_download):
        mock_download.side_effect = NclNotFoundError("2025-08 not yet published")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.MANUAL)

        assert run.status == IngestionRun.Status.FAILED
        assert run.failures.filter(stage=IngestionFailure.Stage.NCL_DOWNLOAD).count() == 1


@pytest.mark.django_db
class TestNclDownloadError:
    @patch("books.services.ingest.download_ncl_csv")
    def test_timeout_marks_run_failed_with_no_partial_data(self, mock_download):
        mock_download.side_effect = NclDownloadTimeoutError("timed out")

        run = ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.status == IngestionRun.Status.FAILED
        assert run.total == 0
        assert run.failures.filter(stage=IngestionFailure.Stage.NCL_DOWNLOAD).count() == 1
        assert Book.objects.count() == 0


@pytest.mark.django_db
class TestUnexpectedExceptionsDoNotLeaveARunningRun:
    # PR #13 review, finding 1: anything not explicitly handled (NCL parse
    # crashing on bad bytes, a DB error from should_query_google_books, an
    # actual bug in the Google adapter, ...) used to propagate straight out
    # of ingest_month, leaving IngestionRun stuck at status='running' /
    # finished_at=NULL forever — violating spec's "非預期錯誤...MUST將該次
    # 執行標記為失敗並保留可查詢的失敗紀錄". The fix re-raises (so a future
    # Celery task can still retry) but always finishes the run as failed
    # first.

    @patch("books.services.ingest.parse_ncl_csv")
    @patch("books.services.ingest.download_ncl_csv")
    def test_parse_crash_marks_run_failed_and_still_reraises(self, mock_download, mock_parse):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_parse.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with pytest.raises(UnicodeDecodeError):
            ingest_month("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        run = IngestionRun.objects.get(month="2025-08")
        assert run.status == IngestionRun.Status.FAILED
        assert run.finished_at is not None
        assert run.failures.filter(stage=IngestionFailure.Stage.INGESTION).exists()

    @patch("books.services.ingest.should_query_google_books")
    @patch("books.services.ingest.download_ncl_csv")
    def test_idempotency_check_db_error_marks_run_failed_and_reraises(
        self, mock_download, mock_should_query
    ):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_should_query.side_effect = RuntimeError("connection to server lost")

        with pytest.raises(RuntimeError):
            ingest_month("2025-09", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        run = IngestionRun.objects.get(month="2025-09")
        assert run.status == IngestionRun.Status.FAILED
        assert run.finished_at is not None

    @patch("books.services.ingest.query_google_books_by_isbn")
    @patch("books.services.ingest.download_ncl_csv")
    def test_google_adapter_bug_marks_run_failed_and_reraises(self, mock_download, mock_google):
        mock_download.return_value = _read("ncl_normal.csv")
        mock_google.side_effect = ValueError("adapter bug, not a GoogleBooksResult")

        with pytest.raises(ValueError):
            ingest_month("2025-10", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        run = IngestionRun.objects.get(month="2025-10")
        assert run.status == IngestionRun.Status.FAILED

    def test_never_leaves_a_run_stuck_in_running_status(self):
        with patch("books.services.ingest.download_ncl_csv", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                ingest_month("2025-11", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        run = IngestionRun.objects.get(month="2025-11")
        assert run.status != IngestionRun.Status.RUNNING
        assert run.finished_at is not None
