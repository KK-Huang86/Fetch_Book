from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from books.models import IngestionRun

# PR #13 review, finding 3: business-logic change from an earlier version
# of this command that called sys.exit() unconditionally, even on
# success. That made call_command() always raise SystemExit — awkward for
# other code/tests reusing this command, and not idiomatic Django (which
# expects handle() to `return` on success and raise CommandError on
# failure; CommandError.returncode is what actually controls the process
# exit code when run for real via manage.py). Every test below asserts
# the new CommandError/return contract instead of SystemExit.


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
    def test_missing_month_raises_command_error_returncode_2(self, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        with pytest.raises(CommandError) as exc_info:
            call_command("ingest_books")
        assert exc_info.value.returncode == 2

    @pytest.mark.parametrize(
        "month",
        [
            "2025/08",
            "202508",
            "2025-8",
            "2025-13-extra",
            "25-08",
            "2025-13",  # shaped like YYYY-MM but not a real month
            "2025-00",
        ],
    )
    def test_invalid_month_raises_command_error_returncode_2(self, settings, month):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        with pytest.raises(CommandError) as exc_info:
            call_command("ingest_books", "--month", month)
        assert exc_info.value.returncode == 2

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_valid_month_format_is_accepted(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)

        call_command("ingest_books", "--month", "2025-08")

        mock_ingest_month.assert_called_once()


@pytest.mark.django_db
class TestMissingApiKey:
    def test_empty_api_key_raises_command_error_returncode_1_without_calling_ingest_month(
        self, settings
    ):
        settings.GOOGLE_BOOKS_API_KEY = ""
        with patch("books.management.commands.ingest_books.ingest_month") as mock_ingest_month:
            with pytest.raises(CommandError) as exc_info:
                call_command("ingest_books", "--month", "2025-08")
            mock_ingest_month.assert_not_called()
        assert exc_info.value.returncode == 1


@pytest.mark.django_db
class TestUnexpectedIngestMonthException:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_reraised_exception_becomes_command_error_returncode_1(
        self, mock_ingest_month, settings
    ):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.side_effect = RuntimeError("db connection lost")

        with pytest.raises(CommandError) as exc_info:
            call_command("ingest_books", "--month", "2025-08")

        assert exc_info.value.returncode == 1
        assert exc_info.value.__cause__ is not None


@pytest.mark.django_db
class TestExitBehaviorFollowsRunStatus:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_succeeded_returns_normally_no_exception(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)
        call_command("ingest_books", "--month", "2025-08")  # must not raise

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_partially_failed_returns_normally_no_exception(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.PARTIALLY_FAILED)
        call_command("ingest_books", "--month", "2025-08")  # must not raise

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_failed_raises_command_error_returncode_1(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.FAILED)
        with pytest.raises(CommandError) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.returncode == 1

    @patch("books.management.commands.ingest_books.ingest_month")
    def test_not_yet_published_month_is_reported_via_status_failed(
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
        with pytest.raises(CommandError) as exc_info:
            call_command("ingest_books", "--month", "2025-08")
        assert exc_info.value.returncode == 1


@pytest.mark.django_db
class TestCallsIngestMonthWithManualTriggerType:
    @patch("books.management.commands.ingest_books.ingest_month")
    def test_trigger_type_is_manual(self, mock_ingest_month, settings):
        settings.GOOGLE_BOOKS_API_KEY = "test-key"
        mock_ingest_month.return_value = _fake_run(IngestionRun.Status.SUCCEEDED)

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

        call_command("ingest_books", "--month", "2025-08")

        out = capsys.readouterr().out
        assert "10" in out
        assert "9" in out
        assert "succeeded" in out.lower()
