from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from books.models import IngestionRun
from books.services.ingest import ingest_month


@shared_task
def ping() -> str:
    """Placeholder task to verify Celery wiring."""
    return "pong"


class IngestMonthRunFailed(Exception):
    """Raised only when ingest_month reports IngestionRun.Status.FAILED,
    to give Celery's autoretry_for something to react to (design.md
    decision 10). Not raised for skipped_not_yet_published (a same-day
    retry makes no sense — tomorrow's scheduled run is the natural retry)
    or partially_failed (the run completed; row-level issues are already
    recorded as IngestionFailure and are typically not transient)."""


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def ingest_current_month(self) -> dict:
    """design.md decision 10: Celery Beat's daily entry point. Always
    trigger_type='scheduled' and always targets "whatever month it is
    right now" in Asia/Taipei — a caller-supplied month is the management
    command's job (seam 6), not this task's."""
    month = timezone.localtime(timezone.now()).strftime("%Y-%m")
    run = ingest_month(month, trigger_type=IngestionRun.TriggerType.SCHEDULED)

    if run.status == IngestionRun.Status.FAILED:
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
