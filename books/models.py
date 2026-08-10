from django.db import models


class Publisher(models.Model):
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name


class Author(models.Model):
    # v1: exact string match (trimmed) only — no fuzzy dedup.
    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    class Source(models.TextChoices):
        NCL = "ncl", "NCL"
        GOOGLE_BOOKS = "google_books", "Google Books"

    class Type(models.TextChoices):
        CLASSIFICATION_NUMBER = "classification_number", "Classification number"
        SHELF_CATEGORY = "shelf_category", "Shelf category"
        SUBJECT_TAG = "subject_tag", "Subject tag"

    source = models.CharField(max_length=32, choices=Source.choices)
    type = models.CharField(max_length=32, choices=Type.choices)
    # No null=True: PostgreSQL treats NULL != NULL, which would let the
    # (source, type, code, label) unique constraint below admit duplicate
    # "no code" categories (e.g. Google Books subject tags). "" is the
    # canonical "no code" value instead.
    code = models.CharField(max_length=64, blank=True, default="")
    label = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "type", "code", "label"], name="uq_category"
            )
        ]

    def __str__(self):
        return f"{self.source}:{self.type}:{self.label}"


class Book(models.Model):
    class CoverImageHosting(models.TextChoices):
        SELF = "self", "Self-hosted (S3/CloudFront)"
        HOTLINK = "hotlink", "Hotlink to source"

    isbn13 = models.CharField(max_length=13, unique=True)
    isbn10 = models.CharField(max_length=10, null=True, blank=True)
    title = models.CharField(max_length=1024)
    publisher = models.ForeignKey(
        Publisher, null=True, blank=True, on_delete=models.SET_NULL, related_name="books"
    )
    authors = models.ManyToManyField(Author, through="BookAuthor", related_name="books")
    categories = models.ManyToManyField(
        Category, through="BookCategory", related_name="books"
    )
    cover_image_url = models.URLField(max_length=1024, null=True, blank=True)
    cover_image_hosting = models.CharField(
        max_length=16, choices=CoverImageHosting.choices, null=True, blank=True
    )
    # First NCL month (YYYY-MM) this ISBN was ingested from. Set once, never updated.
    first_seen_month = models.CharField(max_length=7)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.isbn13} {self.title}"


class BookAuthor(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["book", "author"], name="uq_book_author")
        ]


class BookCategory(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["book", "category"], name="uq_book_category")
        ]


class IngestionRun(models.Model):
    class TriggerType(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIALLY_FAILED = "partially_failed", "Partially failed"
        FAILED = "failed", "Failed"
        SKIPPED_NOT_YET_PUBLISHED = (
            "skipped_not_yet_published",
            "Skipped (not yet published)",
        )

    month = models.CharField(max_length=7)  # YYYY-MM
    trigger_type = models.CharField(max_length=16, choices=TriggerType.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices)
    total = models.PositiveIntegerField(default=0)
    succeeded = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    google_enriched = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.month} ({self.trigger_type}) - {self.status}"


class IngestionFailure(models.Model):
    class Stage(models.TextChoices):
        NCL_DOWNLOAD = "ncl_download", "NCL download"
        NCL_PARSE = "ncl_parse", "NCL parse"
        GOOGLE_LOOKUP = "google_lookup", "Google Books lookup"
        BOOK_UPSERT = "book_upsert", "Book upsert"
        COVER_STORAGE = "cover_storage", "Cover image storage"
        INGESTION = "ingestion", "Ingestion run (unclassified)"

    run = models.ForeignKey(
        IngestionRun, on_delete=models.CASCADE, related_name="failures"
    )
    isbn = models.CharField(max_length=13, null=True, blank=True)
    stage = models.CharField(max_length=32, choices=Stage.choices)
    error_code = models.CharField(max_length=128)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
