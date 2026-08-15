import pytest
from django.utils import timezone

from books.models import IngestionFailure, IngestionRun
from books.services.ingest import is_retryable_failure


def _make_run(status, **overrides):
    defaults = dict(
        month="2025-08",
        trigger_type=IngestionRun.TriggerType.SCHEDULED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        status=status,
        total=0,
        succeeded=0,
        failed=0,
        google_enriched=0,
    )
    defaults.update(overrides)
    return IngestionRun.objects.create(**defaults)


@pytest.mark.django_db
class TestIsRetryableFailure:
    # PR #16 review, finding 1: IngestionRun.Status.FAILED covers both
    # transient/systemic problems (NCL unreachable) AND permanent
    # data-content problems (every row in this month's CSV is invalid) —
    # only the former is worth a same-day Celery retry.

    def test_non_failed_status_is_never_retryable(self):
        for status in [
            IngestionRun.Status.SUCCEEDED,
            IngestionRun.Status.PARTIALLY_FAILED,
            IngestionRun.Status.SKIPPED_NOT_YET_PUBLISHED,
            IngestionRun.Status.RUNNING,
        ]:
            run = _make_run(status)
            assert is_retryable_failure(run) is False

    def test_failed_with_ncl_download_failure_is_retryable(self):
        run = _make_run(IngestionRun.Status.FAILED, total=0, failed=1)
        IngestionFailure.objects.create(
            run=run,
            stage=IngestionFailure.Stage.NCL_DOWNLOAD,
            error_code="NclDownloadTimeoutError",
            message="timed out",
        )
        assert is_retryable_failure(run) is True

    def test_failed_with_only_parse_failures_is_not_retryable(self):
        # e.g. every row in the CSV is missing an ISBN — the download
        # succeeded, the content itself is the problem; retrying with the
        # identical bytes changes nothing.
        run = _make_run(IngestionRun.Status.FAILED, total=3, failed=3)
        for _ in range(3):
            IngestionFailure.objects.create(
                run=run,
                stage=IngestionFailure.Stage.NCL_PARSE,
                error_code="missing_isbn",
                message="row has no ISBN value",
            )
        assert is_retryable_failure(run) is False

    def test_failed_with_only_book_upsert_failures_is_not_retryable(self):
        run = _make_run(IngestionRun.Status.FAILED, total=2, failed=2)
        for _ in range(2):
            IngestionFailure.objects.create(
                run=run,
                stage=IngestionFailure.Stage.BOOK_UPSERT,
                error_code="IntegrityError",
                message="constraint violation",
            )
        assert is_retryable_failure(run) is False

    def test_failed_with_no_failure_rows_at_all_is_not_retryable(self):
        # Defensive/unexpected shape (status=failed but nothing recorded)
        # — default to not retrying rather than assuming transience.
        run = _make_run(IngestionRun.Status.FAILED, total=0, failed=1)
        assert is_retryable_failure(run) is False
