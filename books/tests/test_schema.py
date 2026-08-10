import pytest

from books.services.schema import normalize_isbn, split_authors


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

    def test_valid_ean13_with_non_book_prefix_returns_none(self):
        # 400... is a valid EAN-13 checksum but not a 978/979 ISBN prefix.
        assert normalize_isbn("4006381333931") is None


class TestSplitAuthors:
    def test_single_author_with_role_suffix_strips_suffix(self):
        assert split_authors("吳宗岱著") == ["吳宗岱"]

    def test_multiple_authors_comma_separated_with_trailing_suffix(self):
        assert split_authors("王素貞, 蘇平齡編著") == ["王素貞", "蘇平齡"]

    def test_role_word_glued_by_period_is_stripped(self):
        # Real NCL sample: "花亦芬作.主講" — "作" and "主講" are role words
        # joined onto the name by "." rather than a name separator.
        assert split_authors("花亦芬作.主講") == ["花亦芬"]

    def test_semicolon_separated_role_groups_each_stripped(self):
        # Real NCL sample: author group (single, parenthesised romanization)
        # separated by ";" from a translator group (five names, comma-
        # separated, suffix only attached to the last one).
        assert split_authors(
            "黛安娜.韋恩.瓊斯(Diana Wynne Jones)著; 呂明璇, 黃筱茵, 李珮華, 謝慈, 費艾文譯"
        ) == [
            "黛安娜.韋恩.瓊斯(Diana Wynne Jones)",
            "呂明璇",
            "黃筱茵",
            "李珮華",
            "謝慈",
            "費艾文",
        ]

    def test_full_width_comma_and_semicolon_separators(self):
        assert split_authors("傅曉鳴，廖彥博；徐月寶編") == ["傅曉鳴", "廖彥博", "徐月寶"]

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_or_none_returns_empty_list(self, raw):
        assert split_authors(raw) == []

    def test_name_that_is_only_a_role_word_is_kept_as_is(self):
        # Guard against stripping a segment down to nothing: if the whole
        # segment IS a role word (malformed source data), keep it rather
        # than silently dropping the author entirely.
        assert split_authors("主編") == ["主編"]
