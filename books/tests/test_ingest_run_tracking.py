import pytest

from books.models import IngestionFailure, IngestionRun
from books.services.ingest import (
    finish_ingestion_run,
    record_ingestion_failure,
    start_ingestion_run,
)


@pytest.mark.django_db
class TestStartIngestionRun:
    def test_creates_running_run_with_month_and_trigger_type(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        assert run.month == "2025-08"
        assert run.trigger_type == IngestionRun.TriggerType.SCHEDULED
        assert run.status == IngestionRun.Status.RUNNING
        assert run.started_at is not None
        assert run.finished_at is None

    def test_manual_trigger_type_is_recorded(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.MANUAL)
        assert run.trigger_type == IngestionRun.TriggerType.MANUAL


@pytest.mark.django_db
class TestFinishIngestionRun:
    def test_updates_status_stats_and_finished_at(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        finish_ingestion_run(
            run,
            status=IngestionRun.Status.SUCCEEDED,
            total=10,
            succeeded=9,
            failed=1,
            google_enriched=4,
        )

        run.refresh_from_db()
        assert run.status == IngestionRun.Status.SUCCEEDED
        assert run.total == 10
        assert run.succeeded == 9
        assert run.failed == 1
        assert run.google_enriched == 4
        assert run.finished_at is not None

    def test_partially_failed_status_is_recorded(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)
        finish_ingestion_run(
            run,
            status=IngestionRun.Status.PARTIALLY_FAILED,
            total=5,
            succeeded=3,
            failed=2,
            google_enriched=1,
        )
        run.refresh_from_db()
        assert run.status == IngestionRun.Status.PARTIALLY_FAILED


@pytest.mark.django_db
class TestRecordIngestionFailure:
    def test_creates_failure_linked_to_run(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        failure = record_ingestion_failure(
            run,
            stage=IngestionFailure.Stage.NCL_PARSE,
            error_code="missing_isbn",
            message="row has no ISBN value",
            isbn=None,
        )

        assert failure.run_id == run.id
        assert failure.stage == IngestionFailure.Stage.NCL_PARSE
        assert failure.error_code == "missing_isbn"
        assert failure.isbn is None
        assert run.failures.count() == 1

    def test_isbn_is_stored_when_present(self):
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)
        failure = record_ingestion_failure(
            run,
            stage=IngestionFailure.Stage.GOOGLE_LOOKUP,
            error_code="HTTP 500",
            message="network error: ConnectError",
            isbn="9786269935468",
        )
        assert failure.isbn == "9786269935468"

    def test_message_containing_api_key_pattern_is_rejected(self):
        # design.md decision 12: IngestionFailure.message 不得包含 API 金鑰
        # 或帶金鑰參數的完整請求網址 — defense in depth, in case a future
        # caller forgets to sanitize before calling this.
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)

        with pytest.raises(ValueError):
            record_ingestion_failure(
                run,
                stage=IngestionFailure.Stage.GOOGLE_LOOKUP,
                error_code="HTTP 500",
                message="failed: https://www.googleapis.com/books/v1/volumes?key=SECRET123",
                isbn="9786269935468",
            )

        assert not IngestionFailure.objects.filter(run=run).exists()

    def test_sanitized_message_from_google_books_error_is_accepted(self):
        # The actual shape google_books.py produces post-fix — confirms
        # the sanitized form passes through cleanly end to end.
        run = start_ingestion_run("2025-08", trigger_type=IngestionRun.TriggerType.SCHEDULED)
        failure = record_ingestion_failure(
            run,
            stage=IngestionFailure.Stage.GOOGLE_LOOKUP,
            error_code="network_error",
            message="network error: ConnectError",
            isbn="9786269935468",
        )
        assert "key=" not in failure.message
