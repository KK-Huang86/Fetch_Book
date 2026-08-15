from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from books.models import IngestionRun
from books.services.ingest import ingest_month, is_retryable_failure


@shared_task
def ping() -> str:
    """Placeholder task to verify Celery wiring."""
    return "pong"


class IngestMonthRunFailed(Exception):
    """Raised only for a *retryable* IngestionRun.Status.FAILED (see
    is_retryable_failure) — gives Celery's autoretry_for something to
    react to. Not raised for skipped_not_yet_published, partially_failed,
    or a non-retryable failed (a permanent data-content problem that a
    same-day retry can't fix)."""


@shared_task
def trigger_daily_ingestion() -> dict:
    """Celery Beat's actual daily entry point (see migration
    0004_wire_real_ingestion_task). Resolves the target month exactly
    once and hands off to the retryable task — this way every retry of
    ingest_month_task targets the SAME month even if a retry's backoff
    delay happens to cross a month boundary (PR #16 review, finding 2).
    This task is never itself retried, so recomputing "now" here is
    always correct.

    Fails fast — no retry, a missing key isn't fixed by trying again —
    if GOOGLE_BOOKS_API_KEY isn't configured, instead of silently letting
    every book in the batch fail Google enrichment one at a time."""
    if not settings.GOOGLE_BOOKS_API_KEY:
        raise ImproperlyConfigured("GOOGLE_BOOKS_API_KEY is not set")

    month = timezone.localtime(timezone.now()).strftime("%Y-%m")
    async_result = ingest_month_task.delay(month)
    return {"month": month, "task_id": async_result.id}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def ingest_month_task(self, month: str) -> dict:
    """design.md decision 10. Always trigger_type='scheduled' — a
    caller-supplied month via the management command (seam 6) is a
    separate, non-retried code path that calls ingest_month directly."""
    run = ingest_month(month, trigger_type=IngestionRun.TriggerType.SCHEDULED)

    if run.status == IngestionRun.Status.FAILED and is_retryable_failure(run):
        raise IngestMonthRunFailed(f"ingest_month failed for {month} (run id={run.id})")

    return {
        "month": month,
        "run_id": run.id,
        "status": run.status,
        "total": run.total,
        "succeeded": run.succeeded,
        "failed": run.failed,
        "google_enriched": run.google_enriched,
    }
