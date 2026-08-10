import pytest

from books.services.schema import normalize_isbn


class TestNormalizeIsbn:
    def test_valid_isbn13_returns_as_is(self):
        assert normalize_isbn("9789571234564") == "9789571234564"

    def test_valid_isbn10_converts_to_isbn13(self):
        assert normalize_isbn("0306406152") == "9780306406157"

    def test_valid_isbn10_with_x_check_digit_converts_to_isbn13(self):
        assert normalize_isbn("080442957X") == "9780804429573"
        assert normalize_isbn("080442957x") == "9780804429573"

    def test_strips_hyphens_and_whitespace(self):
        assert normalize_isbn("978-957-123-456-4") == "9789571234564"
        assert normalize_isbn(" 978 9571 23456 4 ") == "9789571234564"
        assert normalize_isbn("0-306-40615-2") == "9780306406157"

    def test_invalid_checksum_returns_none(self):
        assert normalize_isbn("9789571234567") is None
        assert normalize_isbn("0306406153") is None

    @pytest.mark.parametrize("raw", [None, ""])
    def test_empty_or_none_returns_none(self, raw):
        assert normalize_isbn(raw) is None

    def test_non_numeric_characters_return_none(self):
        assert normalize_isbn("abcdefghij") is None
        assert normalize_isbn("97A9571234564") is None

    def test_wrong_length_returns_none(self):
        assert normalize_isbn("12345") is None
        assert normalize_isbn("97895712345678901") is None
