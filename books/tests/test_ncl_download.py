import httpx
import pytest
import respx

from books.sources.ncl import (
    NCL_BASE_URL,
    NclDownloadError,
    NclDownloadTimeoutError,
    NclNetworkError,
    NclNotFoundError,
    build_ncl_csv_url,
    download_ncl_csv,
)


class TestBuildNclCsvUrl:
    def test_builds_url_with_gregorian_yyyymm(self):
        assert (
            build_ncl_csv_url("2025-08")
            == "https://isbn.ncl.edu.tw/NEW_ISBNNet/opendata/202508_isbn.csv"
        )

    @pytest.mark.parametrize(
        "month", ["2025/08", "202508", "25-08", "", "2025-13-bad", "2025-8"]
    )
    def test_malformed_month_string_raises_value_error(self, month):
        with pytest.raises(ValueError):
            build_ncl_csv_url(month)

    @pytest.mark.parametrize("month", [None, 202508])
    def test_non_string_month_raises_value_error(self, month):
        with pytest.raises(ValueError):
            build_ncl_csv_url(month)


class TestDownloadNclCsv:
    @respx.mock
    def test_successful_download_returns_bytes(self):
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            return_value=httpx.Response(200, content=b"header\nrow\n")
        )
        with httpx.Client() as client:
            result = download_ncl_csv("2025-08", client)
        assert result == b"header\nrow\n"

    @respx.mock
    def test_404_raises_not_found_error(self):
        respx.get(NCL_BASE_URL.format(year_month="202509")).mock(
            return_value=httpx.Response(404)
        )
        with httpx.Client() as client, pytest.raises(NclNotFoundError):
            download_ncl_csv("2025-09", client)

    @respx.mock
    def test_read_timeout_raises_ncl_download_timeout_error(self):
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with httpx.Client() as client, pytest.raises(NclDownloadTimeoutError):
            download_ncl_csv("2025-08", client)

    @respx.mock
    def test_connect_timeout_raises_ncl_download_timeout_error(self):
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            side_effect=httpx.ConnectTimeout("timed out connecting")
        )
        with httpx.Client() as client, pytest.raises(NclDownloadTimeoutError):
            download_ncl_csv("2025-08", client)

    @respx.mock
    def test_connection_error_raises_ncl_network_error(self):
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with httpx.Client() as client, pytest.raises(NclNetworkError):
            download_ncl_csv("2025-08", client)

    @respx.mock
    def test_unexpected_5xx_raises_ncl_download_error(self):
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            return_value=httpx.Response(500)
        )
        with httpx.Client() as client, pytest.raises(NclDownloadError):
            download_ncl_csv("2025-08", client)

    @respx.mock
    def test_empty_200_response_returns_empty_bytes(self):
        # A 200 with an empty body is design.md's "空檔案" case — the
        # download layer must not treat it as an error.
        respx.get(NCL_BASE_URL.format(year_month="202508")).mock(
            return_value=httpx.Response(200, content=b"")
        )
        with httpx.Client() as client:
            result = download_ncl_csv("2025-08", client)
        assert result == b""

    def test_invalid_month_is_rejected_before_any_http_call(self):
        with httpx.Client() as client, pytest.raises(ValueError):
            download_ncl_csv("not-a-month", client)
