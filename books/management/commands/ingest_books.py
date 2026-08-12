from __future__ import annotations

import re
import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from books.models import IngestionRun
from books.services.ingest import ingest_month

_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")


class Command(BaseCommand):
    """design.md decision 11: manual backfill/retrigger entry point.
    Shares ingest_month with the Celery task (seam 8) — this command only
    decides trigger_type and translates the resulting IngestionRun.status
    into an exit code."""

    help = "Manually trigger book ingestion for a given month (format: YYYY-MM)."

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="Target month, YYYY-MM")

    def handle(self, *args, **options):
        month = options["month"]

        if not month or not _MONTH_PATTERN.fullmatch(month):
            self.stderr.write(self.style.ERROR(f"Invalid --month: {month!r}, expected YYYY-MM"))
            sys.exit(2)

        if not settings.GOOGLE_BOOKS_API_KEY:
            self.stderr.write(self.style.ERROR("GOOGLE_BOOKS_API_KEY is not set"))
            sys.exit(1)

        run = ingest_month(month, trigger_type=IngestionRun.TriggerType.MANUAL)

        self.stdout.write(
            f"month={run.month} status={run.status} total={run.total} "
            f"succeeded={run.succeeded} failed={run.failed} "
            f"google_enriched={run.google_enriched}"
        )

        if run.status == IngestionRun.Status.FAILED:
            sys.exit(1)
        sys.exit(0)
