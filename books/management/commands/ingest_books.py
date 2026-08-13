from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from books.models import IngestionRun
from books.services.ingest import ingest_month
from books.services.schema import is_valid_month


class Command(BaseCommand):
    """design.md decision 11: manual backfill/retrigger entry point.
    Shares ingest_month with the Celery task (seam 8) — this command only
    decides trigger_type and translates the resulting IngestionRun.status
    into an exit code via CommandError.returncode (Django convention:
    handle() returns on success, raises CommandError on failure — this
    also keeps call_command() usable by other code/tests without forcing
    a SystemExit on the success path)."""

    help = "Manually trigger book ingestion for a given month (format: YYYY-MM)."

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="Target month, YYYY-MM")

    def handle(self, *args, **options):
        month = options["month"]

        if not is_valid_month(month):
            raise CommandError(f"Invalid --month: {month!r}, expected YYYY-MM", returncode=2)

        if not settings.GOOGLE_BOOKS_API_KEY:
            raise CommandError("GOOGLE_BOOKS_API_KEY is not set", returncode=1)

        try:
            run = ingest_month(month, trigger_type=IngestionRun.TriggerType.MANUAL)
        except Exception as exc:
            # Only the exception type, never str(exc) — a genuinely novel
            # bug could in principle carry a URL/secret this far even
            # after every other sanitization layer. Full detail (already
            # redacted) is on the IngestionFailure row; __cause__ still
            # carries the original exception for anyone with log access.
            raise CommandError(
                f"ingest_month raised {type(exc).__name__}; see IngestionRun for details",
                returncode=1,
            ) from exc

        summary = (
            f"month={run.month} status={run.status} total={run.total} "
            f"succeeded={run.succeeded} failed={run.failed} "
            f"google_enriched={run.google_enriched}"
        )
        self.stdout.write(summary)

        if run.status == IngestionRun.Status.FAILED:
            raise CommandError(f"ingestion failed for {month} (see IngestionRun {run.id})", returncode=1)

        return summary
