from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from books.models import IngestionRun


def _fake_run(status, **overrides):
    defaults = dict(
        month="2025-08",
        trigger_type=IngestionRun.TriggerType.MANUAL,
        started_at=timezone.now(),
        status=status,
        total=4,
        succeeded=4,
        failed=0,
        google_enriched=2,
    )
    defaults.update(overrides)
    return IngestionRun.objects.create(**defaults)


@pytest.mark.django_db
class TestMonthValidation:
    def test_missing_month_exits_2(self, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books")
        assert exc_info.value.code == 2

    @pytest.mark.parametrize("month", ["2025/08", "202508", "2025-8", "2025-13-extra", "25-08"])
    def test_invalid_month_format_exits_2(self, settings, month):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books", "--month", month)
        assert exc_info.value.code == 2

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_valid_month_format_is_accepted(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)

        with pytest.raises(SystemExit):
            call_command("ingest_books", "--month", "2025-08")

        mock_ingest_month.assert_called_once()


@pytest.mark.django_db
class TestMissingApiKey:
    def test_empty_api_key_exits_1_without_calling_ingest_month(self, settings):
        settings.GOOGLE_BOOKS_API_KEY = ""
        with patch("books.management.commands.ingest_books.ingest_month") as mock_ingest_month:
            with pytest.raises(SystemExit) as exc_info:
                call_command("ingest_books", "--month", "2025-08")
            mock_ingest_month.assert_not_called()
        assert exc_info.value.code == 1


@pytest.mark.django_db
class TestExitCodesFollowRunStatus:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_succeeded_exits_0(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.code == 0

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_partially_failed_exits_0(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.PARTIALLY_FAILED)
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.code == 0

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_failed_exits_1(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.FAILED)
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.code == 1

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_not_yet_published_month_is_reported_as_failure_via_status_failed(
        self, mock_ingest_month, settings
    ):
        # design.md decision 10: manual trigger + NCL 404 -> ingest_month
        # (seam 7) already maps this to status=failed, not
        # skipped_not_yet_published (that status is scheduled-only). The
        # command only needs to honor whatever status it gets back.
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(
            IngestionRun.Status.FAILED, total=0, succeeded=0, failed=1, google_enriched=0
        )
        with pytest.raises(SystemExit) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.code == 1


@pytest.mark.django_db
class TestCallsIngestMonthWithManualTriggerType:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_trigger_type_is_manual(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)

        with pytest.raises(SystemExit):
            call_command("ingest_books", "--month", "2025-08")

        mock_ingest_month.assert_called_once_with("2025-08", trigger_type=IngestionRun.TriggerType.MANUAL)


@pytest.mark.django_db
class TestSummaryOutput:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_prints_summary_stats_to_stdout(self, mock_ingest_month, settings, capsys):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(
            IngestionRun.Status.SUCCEEDED, total=10, succeeded=9, failed=1, google_enriched=5
        )

        with pytest.raises(SystemExit):
            call_command("ingest_books", "--month", "2025-08")

        out = capsys.readouterr().out
        assert "10" in out
        assert "9" in out
        assert "succeeded" in out.lower()
