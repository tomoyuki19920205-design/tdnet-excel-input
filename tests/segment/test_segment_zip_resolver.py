import os
import tempfile
import zipfile
import json
import hashlib
import pytest
from pathlib import Path
from unittest.mock import patch
from src.segment.segment_zip_resolver import resolve_xbrl_zip
from src.segment.zip_identity_verifier import verify_zip_identity

def _make_dummy_xbrl_zip(path: str, ticker: str, period: str, quarter: str, doc_id: str) -> None:
    """メタデータを実体から正しく抽出できるような構造のダミー ZIP を生成する。"""
    quarter_number = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}[quarter]
    summary_htm_content = f"""
    <html xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ix="http://www.xbrl.org/2003/instance">
      <body>
        <xbrli:identifier scheme="http://www.tse.or.jp/sicc">{ticker}0</xbrli:identifier>
        <xbrli:endDate>{period}</xbrli:endDate>
        <xbrli:instant>{period}</xbrli:instant>
        <ix:nonFraction name="tse-ed-t:QuarterlyPeriod">{quarter_number}</ix:nonFraction>
      </body>
    </html>
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}.xsd", b"")
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}-ixbrl.htm", summary_htm_content.encode("utf-8"))
        zf.writestr("XBRLData/Attachment/manifest.xml", f'<manifest><instance id="qcedjpfr" preferredFilename="tse-qcedjpfr-{ticker}0-{period}-01-{doc_id}.xbrl"/></manifest>'.encode("utf-8"))


def _make_fy_forecast_zip(path: str, ticker: str, actual_period: str, forecast_period: str, doc_id: str) -> None:
    content = f"""
    <html xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ix="http://www.xbrl.org/2003/instance">
      <body>
        <xbrli:identifier scheme="http://www.tse.or.jp/sicc">{ticker}0</xbrli:identifier>
        <xbrli:endDate>{actual_period}</xbrli:endDate>
        <xbrli:endDate>{forecast_period}</xbrli:endDate>
        <ix:nonFraction name="tse-ed-t:QuarterlyPeriod">4</ix:nonFraction>
        <span>AnnualMember</span>
      </body>
    </html>
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}.xsd", b"")
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{doc_id}-ixbrl.htm", content.encode("utf-8"))


def _write_sidecar(path: str, requested_id: str, internal_id: str, ticker: str, period: str, quarter: str = "FY") -> None:
    payload = {
        "schema_version": "1",
        "source": "jquants",
        "requested_disclosure_no": requested_id,
        "requested_file_type": "x",
        "internal_document_id": internal_id,
        "zip_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "downloaded_size": os.path.getsize(path),
        "ticker": ticker,
        "period": period,
        "quarter": quarter,
        "document_type": "attachment_xbrl",
        "fetched_at": "2026-07-13T00:00:00+00:00",
        "resolved_by_function": "get_file_url",
    }
    Path(path + ".provenance.json").write_text(json.dumps(payload), encoding="utf-8")


@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_linked_cache_stale_period_is_rebuilt_from_zip(mock_get, mock_get_file_url, tmp_path):
    requested_id = "20260713591788"
    internal_id = "20260713340570"
    cache_dir = tmp_path / "cache"
    doc_dir = cache_dir / requested_id
    doc_dir.mkdir(parents=True)
    zip_path = doc_dir / "xbrl.zip"
    _make_fy_forecast_zip(str(zip_path), "4057", "2026-05-31", "2027-05-31", internal_id)
    _write_sidecar(str(zip_path), requested_id, internal_id, "4057", "2027-05-31")

    result = resolve_xbrl_zip(
        doc_id=requested_id,
        ticker="4057",
        expected_quarter="FY",
        expected_period="2026-05-31",
        local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(cache_dir),
        allow_jquants_fetch=False,
        persist_provenance=False,
    )

    assert result.status == "FOUND_CACHE_LINKED"
    assert result.trusted_provenance.period == "2026-05-31"
    assert result.trusted_provenance.quarter == "FY"
    verdict = verify_zip_identity(
        str(zip_path), requested_id, "4057", "2026-05-31", "FY",
        trusted_provenance=result.trusted_provenance,
    )
    assert verdict.passed is True
    assert json.loads(Path(str(zip_path) + ".provenance.json").read_text())["period"] == "2027-05-31"
    mock_get_file_url.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.parametrize(
    "actual_period,quarter,expected_period,expected_quarter",
    [("2027-05-31", "FY", "2026-05-31", "FY"), ("2026-05-31", "3Q", "2026-05-31", "FY")],
)
def test_linked_cache_does_not_inject_missing_expected_metadata(
    tmp_path, actual_period, quarter, expected_period, expected_quarter
):
    requested_id = "20260713591788"
    internal_id = "20260713340570"
    cache_dir = tmp_path / "cache"
    doc_dir = cache_dir / requested_id
    doc_dir.mkdir(parents=True)
    zip_path = doc_dir / "xbrl.zip"
    _make_dummy_xbrl_zip(str(zip_path), "4057", actual_period, quarter, internal_id)
    _write_sidecar(str(zip_path), requested_id, internal_id, "4057", actual_period, quarter)

    result = resolve_xbrl_zip(
        doc_id=requested_id, ticker="4057", expected_quarter=expected_quarter,
        expected_period=expected_period, local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(cache_dir), allow_jquants_fetch=False, persist_provenance=False,
    )
    assert result.zip_path is None


@patch('src.segment.segment_zip_resolver.get_file_url')
@patch('src.segment.segment_zip_resolver.requests.get')
def test_fresh_official_zip_uses_expected_actual_period(mock_get, mock_get_file_url, tmp_path):
    requested_id = "20260713591788"
    internal_id = "20260713340570"
    source_zip = tmp_path / "official.zip"
    _make_fy_forecast_zip(str(source_zip), "4057", "2026-05-31", "2027-05-31", internal_id)
    zip_bytes = source_zip.read_bytes()
    mock_get_file_url.return_value = {"xbrl": "https://example.invalid/official.zip"}

    class MockResponse:
        status_code = 200

        def iter_content(self, chunk_size):
            yield zip_bytes

    mock_get.return_value = MockResponse()
    result = resolve_xbrl_zip(
        doc_id=requested_id, ticker="4057", expected_quarter="FY",
        expected_period="2026-05-31", local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"), allow_jquants_fetch=True,
        persist_provenance=False,
    )

    assert result.status == "DOWNLOADED_FROM_JQUANTS"
    assert result.trusted_provenance.period == "2026-05-31"
    assert result.trusted_provenance.quarter == "FY"
    verdict = verify_zip_identity(
        result.zip_path, requested_id, "4057", "2026-05-31", "FY",
        trusted_provenance=result.trusted_provenance,
    )
    assert verdict.passed is True

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
