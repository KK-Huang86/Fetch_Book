from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from books.models import IngestionFailure, IngestionRun
from books.tasks import IngestMonthRunFailed, ingest_month_task, trigger_daily_ingestion


def _make_run(status, **overrides):
    defaults = dict(
        month="2025-08",
        trigger_type=IngestionRun.TriggerType.SCHEDULED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        status=status,
        total=4,
        succeeded=4,
        failed=0,
        google_enriched=2,
    )
    defaults.update(overrides)
    return IngestionRun.objects.create(**defaults)


@pytest.mark.django_db
class TestTriggerDailyIngestion:
    # PR #16 review, finding 2: this task's only job is to resolve
    # "today's month" exactly once and hand off to the retryable task —
    # it is itself never retried, so recomputing "now" here is always
    # correct (unlike doing it inside the retried task, where a retry's
    # backoff delay could cross a month boundary and silently start
    # working on a different month than the one that originally failed).

    @patch("books.tasks.ingest_month_task")
    def test_fails_fast_when_api_key_missing_and_never_delegates(
        self, mock_task, settings
    ):
        settings.GOOGLE_BOOKS_API_KEY = ""
        with pytest.raises(ImproperlyConfigured):
            trigger_daily_ingestion()
        mock_task.delay.assert_not_called()

    @patch("books.tasks.ingest_month_task")
    @patch("books.tasks.timezone.now")
    def test_delegates_with_taipei_local_month(self, mock_now, mock_task, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        # 2025-08-31 20:00 UTC == 2025-09-01 04:00 Asia/Taipei (UTC+8) —
        # a naive UTC-based month calculation would get this wrong.
        mock_now.return_value = datetime(2025, 8, 31, 20, 0, tzinfo=dt_timezone.utc)
        mock_task.delay.return_value.id = "fake-task-id"

        result = trigger_daily_ingestion()

        mock_task.delay.assert_called_once_with("2025-09")
        assert result == {"month": "2025-09", "task_id": "fake-task-id"}


@pytest.mark.django_db
class TestIngestMonthTask:
    @patch("books.tasks.ingest_month")
    def test_always_uses_scheduled_trigger_type(self, mock_ingest_month):
        mock_ingest_month.return_value = _make_run(IngestionRun.Status.SUCCEEDED)
        ingest_month_task("2025-08")
        _, kwargs = mock_ingest_month.call_args
        assert kwargs["trigger_type"] == IngestionRun.TriggerType.SCHEDULED

    @patch("books.tasks.ingest_month")
    def test_succeeded_returns_normally_no_retry_triggered(self, mock_ingest_month):
        mock_ingest_month.return_value = _make_run(IngestionRun.Status.SUCCEEDED)
        result = ingest_month_task("2025-08")
        assert result["status"] == IngestionRun.Status.SUCCEEDED

    @patch("books.tasks.ingest_month")
    def test_partially_failed_returns_normally_no_retry_triggered(self, mock_ingest_month):
        mock_ingest_month.return_value = _make_run(IngestionRun.Status.PARTIALLY_FAILED)
        result = ingest_month_task("2025-08")
        assert result["status"] == IngestionRun.Status.PARTIALLY_FAILED

    @patch("books.tasks.ingest_month")
    def test_skipped_not_yet_published_returns_normally_no_retry_triggered(
        self, mock_ingest_month
    ):
        mock_ingest_month.return_value = _make_run(
            IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED,
            total=0,
            succeeded=0,
            failed=0,
            google_enriched=0,
        )
        result = ingest_month_task("2025-08")
        assert result["status"] == IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED

    @patch("books.tasks.ingest_month")
    def test_retryable_failed_raises_to_trigger_autoretry(self, mock_ingest_month):
        run = _make_run(IngestionRun.Status.FAILED, total=0, succeeded=0, failed=1)
        IngestionFailure.objects.create(
            run=run,
            stage=IngestionFailure.Stage.NCL_DOWNLOAD,
            error_code="NclDownloadTimeoutError",
            message="timed out",
        )
        mock_ingest_month.return_value = run

        with pytest.raises(IngestMonthRunFailed):
            ingest_month_task("2025-08")

    @patch("books.tasks.ingest_month")
    def test_non_retryable_failed_returns_normally_no_retry_triggered(
        self, mock_ingest_month
    ):
        # PR #16 review, finding 1: e.g. every row in the CSV was invalid
        # — a permanent data-content problem, not a transient one.
        # Retrying with the identical CSV bytes wouldn't change anything.
        run = _make_run(IngestionRun.Status.FAILED, total=3, succeeded=0, failed=3)
        for _ in range(3):
            IngestionFailure.objects.create(
                run=run,
                stage=IngestionFailure.Stage.NCL_PARSE,
                error_code="missing_isbn",
                message="row has no ISBN value",
            )
        mock_ingest_month.return_value = run

        result = ingest_month_task("2025-08")
        assert result["status"] == IngestionRun.Status.FAILED

    @patch("books.tasks.ingest_month")
    def test_result_dict_includes_run_stats(self, mock_ingest_month):
        mock_ingest_month.return_value = _make_run(
            IngestionRun.Status.SUCCEEDED,
            total=10,
            succeeded=9,
            failed=1,
            google_enriched=5,
        )
        result = ingest_month_task("2025-08")
        assert result["month"] == "2025-08"
        assert result["status"] == IngestionRun.Status.SUCCEEDED
        assert result["total"] == 10
        assert result["succeeded"] == 9
        assert result["failed"] == 1
        assert result["google_enriched"] == 5


class TestRetryConfiguration:
    # Not re-testing Celery's own retry engine (that's Celery's job to
    # get right) — just pinning the configuration values design.md
    # decision 10 requires, so a refactor can't silently drop them.
    def test_max_retries_is_3(self):
        assert ingest_month_task.max_retries == 3

    def test_retry_backoff_is_enabled(self):
        assert ingest_month_task.retry_backoff is True

    def test_retry_backoff_max_is_600(self):
        assert ingest_month_task.retry_backoff_max == 600

    def test_retry_jitter_is_true(self):
        assert ingest_month_task.retry_jitter is True

    def test_autoretry_for_includes_exception(self):
        assert Exception in ingest_month_task.autoretry_for
