import os
import tempfile
import zipfile
import pytest
from pathlib import Path
from unittest.mock import patch
from src.segment.segment_zip_resolver import resolve_xbrl_zip

def _make_dummy_xbrl_zip(path: str, ticker: str, period: str, quarter: str, doc_id: str) -> None:
    """メタデータを実体から正しく抽出できるような構造のダミー ZIP を生成する。"""
    summary_htm_content = f"""
    <html xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ix="http://www.xbrl.org/2003/instance">
      <body>
        <xbrli:identifier scheme="http://www.tse.or.jp/sicc">{ticker}0</xbrli:identifier>
        <xbrli:endDate>{period}</xbrli:endDate>
        <xbrli:instant>{period}</xbrli:instant>
        <ix:nonFraction name="tse-ed-t:QuarterlyPeriod">1</ix:nonFraction>
      </body>
    </html>
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}.xsd", b"")
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}-ixbrl.htm", summary_htm_content.encode("utf-8"))
        zf.writestr("XBRLData/Attachment/manifest.xml", f'<manifest><instance id="qcedjpfr" preferredFilename="tse-qcedjpfr-{ticker}0-{period}-01-{doc_id}.xbrl"/></manifest>'.encode("utf-8"))

@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_resolve_xbrl_zip_local_archive(mock_get, mock_get_file_url):
    with tempfile.TemporaryDirectory() as tmp_archive, tempfile.TemporaryDirectory() as tmp_cache:
        # Create a mock zip in archive
        ticker_dir = os.path.join(tmp_archive, "1234_Company")
        os.makedirs(ticker_dir)
        zip_path = os.path.join(ticker_dir, "xbrl_12345678901234.zip")
        _make_dummy_xbrl_zip(zip_path, "1234", "2027-03-31", "1Q", "12345678901234")

        result = resolve_xbrl_zip(
            doc_id="12345678901234",
            ticker="1234",
            expected_quarter="1Q",
            expected_period="2027-03-31",
            local_archive_dir=tmp_archive,
            cache_dir=tmp_cache,
            allow_jquants_fetch=True
        )

        assert result.source == "local_archive"
        assert result.status == "FOUND_CACHE"  # resolution_kind = exact_cache
        assert result.zip_path == zip_path
        assert result.cache_hit == True
        assert result.downloaded == False
        mock_get_file_url.assert_not_called()

@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_resolve_xbrl_zip_tdnet_cache(mock_get, mock_get_file_url):
    with tempfile.TemporaryDirectory() as tmp_archive, tempfile.TemporaryDirectory() as tmp_cache:
        # Create a mock zip in cache
        doc_dir = os.path.join(tmp_cache, "12345678901234")
        os.makedirs(doc_dir)
        zip_path = os.path.join(doc_dir, "xbrl.zip")
        _make_dummy_xbrl_zip(zip_path, "1234", "2027-03-31", "1Q", "12345678901234")

        result = resolve_xbrl_zip(
            doc_id="12345678901234",
            ticker="1234",
            expected_quarter="1Q",
            expected_period="2027-03-31",
            local_archive_dir=tmp_archive,
            cache_dir=tmp_cache,
            allow_jquants_fetch=True
        )

        assert result.source == "tdnet_cache"
        assert result.status == "FOUND_CACHE"
        assert result.zip_path == zip_path
        assert result.cache_hit == True
        assert result.downloaded == False
        mock_get_file_url.assert_not_called()

@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_resolve_xbrl_zip_jquants_fetch(mock_get, mock_get_file_url):
    with tempfile.TemporaryDirectory() as tmp_archive, tempfile.TemporaryDirectory() as tmp_cache:
        mock_get_file_url.return_value = {'xbrl': 'https://mock.jquants.com/signed/url'}
        
        # ダミー ZIP バイト列を作成して返すように設定
        temp_zip = os.path.join(tmp_archive, "temp_official.zip")
        _make_dummy_xbrl_zip(temp_zip, "1234", "2027-03-31", "1Q", "12345678901234")
        with open(temp_zip, "rb") as f:
            zip_bytes = f.read()

        class MockResponse:
            status_code = 200
            def iter_content(self, chunk_size):
                yield zip_bytes
                
        mock_get.return_value = MockResponse()

        result = resolve_xbrl_zip(
            doc_id="12345678901234",
            ticker="1234",
            expected_quarter="1Q",
            expected_period="2027-03-31",
            local_archive_dir=tmp_archive,
            cache_dir=tmp_cache,
            allow_jquants_fetch=True
        )

        assert result.source == "jquants_download"
        assert result.status == "DOWNLOADED_FROM_JQUANTS"
        assert result.cache_hit == False
        assert result.downloaded == True
        mock_get_file_url.assert_called_once()
        mock_get.assert_called_once()
        
        # Check cache was populated
        expected_cache_path = os.path.join(tmp_cache, "12345678901234", "xbrl.zip")
        assert os.path.exists(expected_cache_path)

@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_resolve_xbrl_zip_jquants_fetch_disabled(mock_get, mock_get_file_url):
    with tempfile.TemporaryDirectory() as tmp_archive, tempfile.TemporaryDirectory() as tmp_cache:
        result = resolve_xbrl_zip(
            doc_id="12345678901234",
            ticker="1234",
            expected_quarter="1Q",
            expected_period="2027-03-31",
            local_archive_dir=tmp_archive,
            cache_dir=tmp_cache,
            allow_jquants_fetch=False
        )

        assert result.source == "not_found"
        assert result.status == "SKIPPED_NO_FETCH_ALLOWED"
        assert result.cache_hit == False
        assert result.downloaded == False
        mock_get_file_url.assert_not_called()

@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
@patch('src.segment.segment_zip_resolver.time.sleep')
def test_resolve_xbrl_zip_jquants_fetch_retry_exhausted(mock_sleep, mock_get, mock_get_file_url):
    with tempfile.TemporaryDirectory() as tmp_archive, tempfile.TemporaryDirectory() as tmp_cache:
        mock_get_file_url.return_value = {'xbrl': 'https://mock.jquants.com/signed/url'}
        
        class MockResponse:
            status_code = 429
            def iter_content(self, chunk_size):
                yield b""
                
        mock_get.return_value = MockResponse()

        result = resolve_xbrl_zip(
            doc_id="12345678901234",
            ticker="1234",
            expected_quarter="1Q",
            expected_period="2027-03-31",
            local_archive_dir=tmp_archive,
            cache_dir=tmp_cache,
            allow_jquants_fetch=True,
            max_retries=2
        )

        assert result.source == "fetch_failed"
        assert result.status == "JQUANTS_RATE_LIMIT_RETRY_EXHAUSTED"
        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1
