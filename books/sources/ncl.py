from __future__ import annotations

import csv
import io

from books.services.schema import (
    CategoryInput,
    ParsedBookRecord,
    ParseFailure,
    normalize_isbn,
    split_authors,
)

# Confirmed against 4 real monthly downloads (2024-12, 2025-01, 2025-07,
# 2025-08) — see design.md decision 8. Field names, not positions, are the
# contract: NCL has renamed columns before (常用分類→建議上架分類 etc.).
_CLASSIFICATION_NUMBER_FIELD = "分類號"
_SHELF_CATEGORY_FIELD = "建議上架分類"
_SUBJECT_TAG_FIELD = "圖書主題"


def _row_categories(row: dict[str, str]) -> list[CategoryInput]:
    categories: list[CategoryInput] = []

    classification_number = row.get(_CLASSIFICATION_NUMBER_FIELD, "").strip()
    if classification_number:
        categories.append(
            CategoryInput(
                type="classification_number",
                code=classification_number,
                label=classification_number,
            )
        )

    shelf_category = row.get(_SHELF_CATEGORY_FIELD, "").strip()
    if shelf_category:
        categories.append(
            CategoryInput(type="shelf_category", code="", label=shelf_category)
        )

    subject_tag = row.get(_SUBJECT_TAG_FIELD, "").strip()
    if subject_tag:
        categories.append(CategoryInput(type="subject_tag", code="", label=subject_tag))

    return categories


def parse_ncl_csv(raw: bytes) -> tuple[list[ParsedBookRecord], list[ParseFailure]]:
    """Parse NCL's monthly 新書預告書訊 CSV (already-downloaded bytes) into
    intermediate book records. Pure function — no network I/O.

    A row with a missing or invalid ISBN is reported as a ParseFailure and
    excluded from `records`; it never raises and never aborts the rest of
    the batch (per design.md decision 8/CLAUDE.md 資料操作穩健性規範).
    """
    records: list[ParsedBookRecord] = []
    failures: list[ParseFailure] = []

    if not raw:
        return records, failures

    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        raw_isbn = (row.get("ISBN") or "").strip()
        isbn13 = normalize_isbn(raw_isbn)

        if isbn13 is None:
            if not raw_isbn:
                failures.append(
                    ParseFailure(
                        isbn=None,
                        error_code="missing_isbn",
                        message="row has no ISBN value",
                    )
                )
            else:
                failures.append(
                    ParseFailure(
                        isbn=raw_isbn,
                        error_code="invalid_isbn",
                        message=f"ISBN '{raw_isbn}' failed checksum/format validation",
                    )
                )
            continue

        records.append(
            ParsedBookRecord(
                isbn13=isbn13,
                title=(row.get("申請書名") or "").strip(),
                authors=split_authors(row.get("作者")),
                publisher=(row.get("出版機構") or "").strip(),
                categories=_row_categories(row),
            )
        )

    return records, failures
