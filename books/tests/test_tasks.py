from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest

from books.models import IngestionRun
from books.tasks import IngestMonthRunFailed, ingest_current_month


def _fake_run(status, **overrides):
    defaults = dict(
        id=1,
        month="2025-08",
        status=status,
        total=4,
        succeeded=4,
        failed=0,
        google_enriched=2,
    )
    defaults.update(overrides)

    class _FakeRun:
        pass

    run = _FakeRun()
    for key, value in defaults.items():
        setattr(run, key, value)
    return run


class TestIngestCurrentMonth:
    @patch("books.tasks.ingest_month")
    @patch("books.tasks.timezone.now")
    def test_uses_taipei_local_month_not_utc_month(self, mock_now, mock_ingest_month):
        # 2025-08-31 20:00 UTC == 2025-09-01 04:00 Asia/Taipei (UTC+8) —
        # a naive UTC-based month calculation would get this wrong.
        mock_now.return_value = datetime(2025, 8, 31, 20, 0, tzinfo=dt_timezone.utc)
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED, month="2025-09")

        ingest_current_month()

        mock_ingest_month.assert_called_once_with(
            "2025-09", trigger_type=IngestionRun.TriggerType.SCHEDULED
        )

    @patch("books.tasks.ingest_month")
    @patch("books.tasks.timezone.now")
    def test_always_uses_scheduled_trigger_type(self, mock_now, mock_ingest_month):
        mock_now.return_value = datetime(2025, 8, 15, 3, 0, tzinfo=dt_timezone.utc)
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)

        ingest_current_month()

        _, kwargs = mock_ingest_month.call_args
        assert kwargs["trigger_type"] == IngestionRun.TriggerType.SCHEDULED

    @patch("books.tasks.ingest_month")
    def test_succeeded_returns_normally_no_retry_triggered(self, mock_ingest_month):
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)
        result = ingest_current_month()
        assert result["status"] == IngestionRun.Status.SUCCEEDED

    @patch("books.tasks.ingest_month")
    def test_partially_failed_returns_normally_no_retry_triggered(self, mock_ingest_month):
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.PARTIALLY_FAILED)
        result = ingest_current_month()
        assert result["status"] == IngestionRun.Status.PARTIALLY_FAILED

    @patch("books.tasks.ingest_month")
    def test_skipped_not_yet_published_returns_normally_no_retry_triggered(
        self, mock_ingest_month
    ):
        # design.md decision 10: this must NOT raise — a raised exception
        # is what autoretry_for reacts to, and a same-day retry makes no
        # sense for "not published yet" (tomorrow's scheduled run is the
        # natural retry).
        mock_ingest_month.return_value = _fake_run(
            IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED, total=0, succeeded=0, failed=0,
            google_enriched=0,
        )
        result = ingest_current_month()
        assert result["status"] == IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED

    @patch("books.tasks.ingest_month")
    def test_failed_raises_to_trigger_autoretry(self, mock_ingest_month):
        mock_ingest_month.return_value = _fake_run(
            IngestionRun.Status.FAILED, total=0, succeeded=0, failed=1, google_enriched=0
        )
        with pytest.raises(IngestMonthRunFailed):
            ingest_current_month()

    @patch("books.tasks.ingest_month")
    @patch("books.tasks.timezone.now")
    def test_result_dict_includes_run_stats(self, mock_now, mock_ingest_month):
        mock_now.return_value = datetime(2025, 8, 15, 3, 0, tzinfo=dt_timezone.utc)
        mock_ingest_month.return_value = _fake_run(
            IngestionRun.Status.SUCCEEDED,
            id=42,
            total=10,
            succeeded=9,
            failed=1,
            google_enriched=5,
        )
        result = ingest_current_month()
        assert result == {
            "month": "2025-08",
            "run_id": 42,
            "status": IngestionRun.Status.SUCCEEDED,
            "total": 10,
            "succeeded": 9,
            "failed": 1,
            "google_enriched": 5,
        }


class TestRetryConfiguration:
    # Not re-testing Celery's own retry engine (that's Celery's job to
    # get right) — just pinning the configuration values design.md
    # decision 10 requires, so a refactor can't silently drop them.
    def test_max_retries_is_3(self):
        assert ingest_current_month.max_retries == 3

    def test_retry_backoff_is_enabled(self):
        assert ingest_current_month.retry_backoff is True

    def test_autoretry_for_includes_exception(self):
        assert Exception in ingest_current_month.autoretry_for
