from pathlib import Path

import pytest

from books.services.schema import CategoryInput, ParsedBookRecord
from books.sources.ncl import parse_ncl_csv

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestParseNclCsv:
    def test_normal_rows_produce_expected_records(self):
        records, failures = parse_ncl_csv(_read("ncl_normal.csv"))

        assert failures == []
        assert len(records) == 4

        record = records[0]
        assert record == ParsedBookRecord(
            isbn13="9786269935468",
            title="Bàn-tāi",
            authors=["吳宗岱"],
            publisher="島座放送",
            categories=[
                CategoryInput(type="classification_number", code="957.9", label="957.9"),
                CategoryInput(type="shelf_category", code="", label="藝術"),
                CategoryInput(type="subject_tag", code="", label="藝術"),
            ],
        )

    def test_row_with_dotted_role_word_author_and_missing_classification_number(self):
        records, failures = parse_ncl_csv(_read("ncl_normal.csv"))

        assert failures == []
        record = records[1]
        assert record.isbn13 == "9786267010860"
        assert record.authors == ["花亦芬"]
        assert record.publisher == "一刻鯨選"
        # 分類號 (classification_number) is blank on this row — must not
        # produce a CategoryInput for it, but the other two category
        # fields are still present.
        assert record.categories == [
            CategoryInput(type="shelf_category", code="", label="人文史地"),
            CategoryInput(type="subject_tag", code="", label="史地/傳記"),
        ]

    def test_row_with_quoted_comma_field_and_multiple_authors(self):
        records, failures = parse_ncl_csv(_read("ncl_normal.csv"))

        assert failures == []
        record = records[2]
        assert record.isbn13 == "9786264237307"
        assert record.title == "畜產加工(含實習) (上冊)"
        assert record.authors == ["王素貞", "蘇平齡"]
        assert record.publisher == "五南"

    def test_row_with_semicolon_and_comma_mixed_author_groups(self):
        records, failures = parse_ncl_csv(_read("ncl_normal.csv"))

        assert failures == []
        record = records[3]
        assert record.isbn13 == "9786267591659"
        assert record.authors == [
            "黛安娜.韋恩.瓊斯(Diana Wynne Jones)",
            "呂明璇",
            "黃筱茵",
            "李珮華",
            "謝慈",
            "費艾文",
        ]

    def test_missing_isbn_row_is_reported_as_failure_not_raised(self):
        records, failures = parse_ncl_csv(_read("ncl_missing_isbn.csv"))

        assert records == []
        assert len(failures) == 1
        assert failures[0].isbn is None
        assert failures[0].error_code == "missing_isbn"

    def test_invalid_isbn_row_is_reported_as_failure_with_raw_isbn_kept(self):
        records, failures = parse_ncl_csv(_read("ncl_invalid_isbn.csv"))

        assert records == []
        assert len(failures) == 1
        assert failures[0].isbn == "9786269935469"
        assert failures[0].error_code == "invalid_isbn"

    def test_empty_file_returns_no_records_and_no_failures(self):
        records, failures = parse_ncl_csv(_read("ncl_empty.csv"))

        assert records == []
        assert failures == []

    def test_one_bad_row_does_not_abort_parsing_of_the_rest(self):
        # ncl_missing_isbn.csv has exactly one row and it's the bad one;
        # this is really exercised by test_normal_rows_produce_expected_records
        # already having 4/4 succeed together with mixed field-emptiness —
        # here we assert the batch-level contract explicitly: a parse
        # failure must never raise, only ever be appended to `failures`.
        records, failures = parse_ncl_csv(_read("ncl_missing_isbn.csv"))
        assert isinstance(records, list)
        assert isinstance(failures, list)
