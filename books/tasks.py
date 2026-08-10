from celery import shared_task


@shared_task
def ping() -> str:
    """Placeholder task to verify Celery wiring. Replaced by the real
    scheduled ingestion task in issue #8."""
    return "pong"
