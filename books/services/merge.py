from __future__ import annotations

from dataclasses import dataclass, field

from books.services.schema import CategoryInput, GoogleBooksResult, ParsedBookRecord


@dataclass(frozen=True)
class ResolvedCategory:
    """A CategoryInput with its source resolved (design.md decision 5) —
    CategoryInput itself stays source-less since a source adapter's own
    output is implicitly all-one-source; merge is where NCL and Google
    Books categories meet, so this is where `source` becomes necessary to
    tell them apart and to match the DB's (source, type, code, label) key.
    """

    source: str
    type: str
    code: str
    label: str


@dataclass(frozen=True)
class ExistingBookState:
    """Snapshot of a Book's mergeable fields as currently stored, or None
    (via merge_book_fields' `existing` param) for a book being inserted
    for the first time."""

    cover_image_url: str | None
    cover_image_hosting: str | None
    categories: list[ResolvedCategory] = field(default_factory=list)


@dataclass(frozen=True)
class MergedBookFields:
    """What the upsert layer (seam 5) should write for the book's
    non-key, non-bookkeeping fields (isbn13/first_seen_month/timestamps
    are the upsert layer's own concern, not merge's)."""

    title: str
    authors: list[str]
    publisher: str
    categories: list[ResolvedCategory]
    cover_image_url: str | None
    cover_image_hosting: str | None


def _upgrade_to_https(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _tag_source(categories: list[CategoryInput], source: str) -> list[ResolvedCategory]:
    return [
        ResolvedCategory(source=source, type=c.type, code=c.code, label=c.label)
        for c in categories
    ]


def _union_categories(
    existing: list[ResolvedCategory], new: list[ResolvedCategory]
) -> list[ResolvedCategory]:
    merged = list(existing)
    seen = {(c.source, c.type, c.code, c.label) for c in merged}
    for category in new:
        key = (category.source, category.type, category.code, category.label)
        if key not in seen:
            merged.append(category)
            seen.add(key)
    return merged


def merge_book_fields(
    ncl_record: ParsedBookRecord,
    existing: ExistingBookState | None,
    google_result: GoogleBooksResult | None = None,
) -> MergedBookFields:
    """Apply design.md decision 5's field-override rules. Pure function —
    no DB access; `existing` is whatever the caller (seam 5) already read.
    """
    existing_categories = existing.categories if existing else []
    existing_cover_url = existing.cover_image_url if existing else None
    existing_cover_hosting = existing.cover_image_hosting if existing else None

    new_categories = _tag_source(ncl_record.categories, "ncl")
    if google_result is not None and google_result.status == "found":
        new_categories += _tag_source(google_result.categories, "google_books")

    if existing_cover_url:
        cover_image_url = existing_cover_url
        cover_image_hosting = existing_cover_hosting
    elif (
        google_result is not None
        and google_result.status == "found"
        and google_result.cover_image_url
    ):
        cover_image_url = _upgrade_to_https(google_result.cover_image_url)
        cover_image_hosting = "hotlink"
    else:
        cover_image_url = None
        cover_image_hosting = None

    return MergedBookFields(
        title=ncl_record.title,
        authors=list(ncl_record.authors),
        publisher=ncl_record.publisher,
        categories=_union_categories(existing_categories, new_categories),
        cover_image_url=cover_image_url,
        cover_image_hosting=cover_image_hosting,
    )
