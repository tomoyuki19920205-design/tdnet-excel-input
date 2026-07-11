"""tests/segment/test_runtime_provenance_wiring.py

Phase 9F-R: resolver/pipeline 配線と信頼境界の自動テスト (Test S ~ Z)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
import tempfile
import shutil
from pathlib import Path
from unittest import mock
import pytest

from src.segment.zip_identity_verifier import (
    TrustedProvenance,
    ZipIdentityVerdict,
    verify_zip_identity,
    extract_actual_metadata_from_zip,
)
from src.segment.segment_zip_resolver import resolve_xbrl_zip, ZipResolveResult
from src.events.earnings_production_pipeline import _sync_canonical_segments

# ======================================================================
# テスト用ヘルパー
# ======================================================================
def _make_dummy_xbrl_zip(path: Path, ticker: str, period: str, quarter: str, internal_id: str) -> None:
    """メタデータを実体から正しく抽出できるような構造のダミー ZIP を生成する。"""
    q_map = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}
    q_num = q_map.get(quarter, "1")
    
    # Summary htm の作成
    summary_htm_content = f"""
    <html xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:ix="http://www.xbrl.org/2003/instance">
      <body>
        <xbrli:identifier scheme="http://www.tse.or.jp/sicc">{ticker}0</xbrli:identifier>
        <xbrli:endDate>{period}</xbrli:endDate>
        <xbrli:instant>{period}</xbrli:instant>
        <ix:nonFraction name="tse-ed-t:QuarterlyPeriod">{q_num}</ix:nonFraction>
      </body>
    </html>
    """
    
    with zipfile.ZipFile(path, "w") as zf:
        # ticker / internal_id を含む xsd ファイル
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{internal_id}.xsd", b"")
        # Summary htm
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{internal_id}-ixbrl.htm", summary_htm_content.encode("utf-8"))
        # manifest.xml
        zf.writestr("XBRLData/Attachment/manifest.xml", f'<manifest><instance id="qcedjpfr" preferredFilename="tse-qcedjpfr-{ticker}0-{period}-01-{internal_id}.xbrl"/></manifest>'.encode("utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ======================================================================
# Test S: resolver fresh download wiring
# ======================================================================
@mock.patch("src.segment.segment_zip_resolver.get_file_url")
@mock.patch("requests.get")
def test_s_resolver_fresh_download_wiring(mock_get, mock_get_file_url, tmp_path):
    """J-Quants公式API経由でのZIP新規ダウンロード時に TrustedProvenance が生成されることを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    # ダミー ZIP を生成
    download_zip_path = tmp_path / "download.zip"
    _make_dummy_xbrl_zip(download_zip_path, ticker, period, quarter, int_id)
    zip_bytes = download_zip_path.read_bytes()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    # Mock設定
    mock_get_file_url.return_value = {"xbrl": "http://mock-jquants/file.zip"}
    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [zip_bytes]
    mock_get.return_value = mock_response

    # resolve_xbrl_zip の実行
    cache_dir = tmp_path / "cache"
    archive_dir = tmp_path / "archive"
    
    result = resolve_xbrl_zip(
        doc_id=req_id,
        ticker=ticker,
        expected_quarter=quarter,
        expected_period=period,
        local_archive_dir=str(archive_dir),
        cache_dir=str(cache_dir),
        persist_provenance=True,
    )

    assert result.zip_path is not None
    assert result.resolution_kind == "official_download"
    assert result.trusted_provenance is not None
    
    prov = result.trusted_provenance
    assert prov.requested_disclosure_no == req_id
    assert prov.internal_document_id == int_id
    assert prov.downloaded_sha256 == zip_sha
    assert prov.ticker == ticker
    assert prov.period == period
    assert prov.quarter == quarter
    assert prov.document_type == "attachment_xbrl"

    # sidecar が作成されていることを確認 (方式B)
    sidecar_path = Path(result.zip_path + ".provenance.json")
    assert sidecar_path.exists()
    
    # 照合
    with open(sidecar_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["requested_disclosure_no"] == req_id
    assert data["internal_document_id"] == int_id
    assert data["zip_sha256"] == zip_sha


# ======================================================================
# Test T: pipeline は resolver provenance を使用
# ======================================================================
@mock.patch("src.events.earnings_production_pipeline._extract_and_filter_segments")
def test_t_pipeline_uses_resolver_provenance(mock_extract, tmp_path):
    """_sync_canonical_segments が resolver 由来の provenance を使用して合格することを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / "xbrl.zip"
    _make_dummy_xbrl_zip(zip_path, ticker, period, quarter, int_id)
    sha = _sha256(zip_path)

    # 有効な provenance
    prov = TrustedProvenance(
        source="jquants",
        requested_disclosure_no=req_id,
        requested_file_type="x",
        resolved_by_function="get_file_url",
        official_request_succeeded=True,
        response_status=200,
        downloaded_size=zip_path.stat().st_size,
        downloaded_sha256=sha,
        internal_document_id=int_id,
        ticker=ticker,
        period=period,
        quarter=quarter,
        document_type="attachment_xbrl",
        resolved_at="2026-07-11T12:00:00Z",
    )

    mock_extract.return_value = [{"segment_name": "Apparel", "segment_sales": 100, "segment_profit": 10}]

    # pipeline 実行 (dry_run=True で Supabase への書込みはさせない)
    # _sync_canonical_segments 内部で verify_zip_identity が走り、prov を用いて official_linked_xbrl_match で通過するはず
    with mock.patch("src.events.earnings_production_pipeline.logger") as mock_logger:
        _sync_canonical_segments(
            ticker=ticker,
            period=period,
            quarter=quarter,
            canonical_filing_id="b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c",
            common_disclosure_no=req_id,
            xbrl_path=str(zip_path),
            dry_run=True,
            route="test",
            trusted_provenance=prov,
        )
        
        # エラーログが出力されず、正常開始ログが出ていることを確認
        error_calls = [c for c in mock_logger.error.call_args_list if "stage=identity" in str(c)]
        assert len(error_calls) == 0


# ======================================================================
# Test U: pipeline へ plain ZIP path だけを渡した linked case (拒否)
# ======================================================================
def test_u_pipeline_plain_zip_linked_rejected(tmp_path):
    """provenance を渡さない linked case は拒否されることを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / "xbrl.zip"
    _make_dummy_xbrl_zip(zip_path, ticker, period, quarter, int_id)

    with mock.patch("src.events.earnings_production_pipeline.logger") as mock_logger:
        _sync_canonical_segments(
            ticker=ticker,
            period=period,
            quarter=quarter,
            canonical_filing_id="b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c",
            common_disclosure_no=req_id,
            xbrl_path=str(zip_path),
            dry_run=True,
            route="test",
            trusted_provenance=None,  # なし
        )
        # provenance_missing で identity エラーが記録される
        error_calls = [c for c in mock_logger.error.call_args_list if "provenance_missing" in str(c)]
        assert len(error_calls) > 0


# ======================================================================
# Test V: exact cache wiring (ネットワーク0回)
# ======================================================================
@mock.patch("src.segment.segment_zip_resolver.get_file_url")
def test_v_exact_cache_wiring(mock_get_file_url, tmp_path):
    """内部ID完全一致のキャッシュは、ネットワーク接続なしで exact_cache で PASS することを確認。"""
    req_id = "20260709590450"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    cache_dir = tmp_path / "cache"
    cache_path_dir = cache_dir / req_id
    cache_path_dir.mkdir(parents=True)
    cache_zip = cache_path_dir / "xbrl.zip"
    
    # 内部ID = requested ID となる ZIP
    _make_dummy_xbrl_zip(cache_zip, ticker, period, quarter, req_id)

    result = resolve_xbrl_zip(
        doc_id=req_id,
        ticker=ticker,
        expected_quarter=quarter,
        expected_period=period,
        local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(cache_dir),
        allow_jquants_fetch=True,
    )

    assert result.zip_path == str(cache_zip)
    assert result.resolution_kind == "exact_cache"
    assert result.trusted_provenance is None
    # ネットワークが呼ばれていないこと
    mock_get_file_url.assert_not_called()


# ======================================================================
# Test W: linked cache with valid metadata (ネットワーク0回)
# ======================================================================
@mock.patch("src.segment.segment_zip_resolver.get_file_url")
def test_w_linked_cache_with_valid_sidecar(mock_get_file_url, tmp_path):
    """有効な sidecar メタデータがある場合、ネットワーク接続なしで linked PASS することを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    cache_dir = tmp_path / "cache"
    cache_path_dir = cache_dir / req_id
    cache_path_dir.mkdir(parents=True)
    cache_zip = cache_path_dir / "xbrl.zip"
    _make_dummy_xbrl_zip(cache_zip, ticker, period, quarter, int_id)
    sha = _sha256(cache_zip)

    # sidecar を作成
    prov = TrustedProvenance(
        source="jquants",
        requested_disclosure_no=req_id,
        requested_file_type="x",
        resolved_by_function="get_file_url",
        official_request_succeeded=True,
        response_status=200,
        downloaded_size=cache_zip.stat().st_size,
        downloaded_sha256=sha,
        internal_document_id=int_id,
        ticker=ticker,
        period=period,
        quarter=quarter,
        document_type="attachment_xbrl",
        resolved_at="2026-07-11T12:00:00Z",
    )
    from src.segment.segment_zip_resolver import _write_sidecar_provenance
    _write_sidecar_provenance(str(cache_zip), prov)

    # 実行
    result = resolve_xbrl_zip(
        doc_id=req_id,
        ticker=ticker,
        expected_quarter=quarter,
        expected_period=period,
        local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(cache_dir),
        allow_jquants_fetch=True,
    )

    assert result.zip_path == str(cache_zip)
    assert result.resolution_kind == "verified_linked_cache"
    assert result.trusted_provenance is not None
    assert result.trusted_provenance.internal_document_id == int_id
    mock_get_file_url.assert_not_called()


# ======================================================================
# Test X: linked cache metadata missing
# ======================================================================
@mock.patch("src.segment.segment_zip_resolver.get_file_url")
@mock.patch("requests.get")
def test_x_linked_cache_metadata_missing(mock_get, mock_get_file_url, tmp_path):
    """sidecarがない場合、公式再取得＆ハッシュ照合により PASS することを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    cache_dir = tmp_path / "cache"
    cache_path_dir = cache_dir / req_id
    cache_path_dir.mkdir(parents=True)
    cache_zip = cache_path_dir / "xbrl.zip"
    _make_dummy_xbrl_zip(cache_zip, ticker, period, quarter, int_id)
    sha = _sha256(cache_zip)

    # mock ダウンロード用の実体 (同じ ZIP なので SHA は一致する)
    mock_get_file_url.return_value = {"xbrl": "http://mock-jquants/file.zip"}
    mock_response = mock.Mock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [cache_zip.read_bytes()]
    mock_get.return_value = mock_response

    # resolve_xbrl_zip の実行 (persist_provenance=False にして sidecar は作成しない)
    result = resolve_xbrl_zip(
        doc_id=req_id,
        ticker=ticker,
        expected_quarter=quarter,
        expected_period=period,
        local_archive_dir=str(tmp_path / "archive"),
        cache_dir=str(cache_dir),
        allow_jquants_fetch=True,
        persist_provenance=False,
    )

    assert result.zip_path == str(cache_zip)
    assert result.resolution_kind == "verified_linked_cache"
    assert result.trusted_provenance is not None
    assert result.trusted_provenance.downloaded_sha256 == sha
    # sidecar が作成されていないことを確認
    assert not Path(str(cache_zip) + ".provenance.json").exists()


# ======================================================================
# Test Y: pipeline raw_payload 偽装拒否
# ======================================================================
def test_y_pipeline_fake_provenance_rejected(tmp_path):
    """外部入力で偽装された provenance を pipeline は信用しないことを確認。"""
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / "xbrl.zip"
    _make_dummy_xbrl_zip(zip_path, ticker, period, quarter, int_id)
    sha = _sha256(zip_path)

    # source=jquants を騙る偽装 provenance (外部入力・偽造)
    fake_prov = TrustedProvenance(
        source="forged_source",  # jquants 以外
        requested_disclosure_no=req_id,
        requested_file_type="x",
        resolved_by_function="get_file_url",
        official_request_succeeded=True,
        response_status=200,
        downloaded_size=zip_path.stat().st_size,
        downloaded_sha256=sha,
        internal_document_id=int_id,
        ticker=ticker,
        period=period,
        quarter=quarter,
        document_type="attachment_xbrl",
        resolved_at="2026-07-11T12:00:00Z",
    )

    with mock.patch("src.events.earnings_production_pipeline.logger") as mock_logger:
        _sync_canonical_segments(
            ticker=ticker,
            period=period,
            quarter=quarter,
            canonical_filing_id="b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c",
            common_disclosure_no=req_id,
            xbrl_path=str(zip_path),
            dry_run=True,
            route="test",
            trusted_provenance=fake_prov,
        )
        
        # untrusted_source で identity エラーが記録される
        error_calls = [c for c in mock_logger.error.call_args_list if "untrusted_source" in str(c)]
        assert len(error_calls) > 0


# ======================================================================
# Test Z: constructor 利用制限 (静的 constructor 呼び出し箇所監査)
# ======================================================================
def test_z_provenance_constructor_boundary():
    """本番コード src/ 内で TrustedProvenance のインスタンス化が resolver のみに限定されていることを静的検証する。"""
    src_dir = Path("src")
    constructor_calls = []

    # python ファイルを走査
    for p in src_dir.rglob("*.py"):
        content = p.read_text(encoding="utf-8", errors="ignore")
        # "TrustedProvenance(" の文字列を探す (定義箇所 class TrustedProvenance 以外の呼び出し)
        for line_no, line in enumerate(content.splitlines(), 1):
            if "TrustedProvenance(" in line and "class TrustedProvenance" not in line:
                constructor_calls.append((p.name, line_no, line))

    # インスタンス化呼び出しが許されるのは segment_zip_resolver.py のみ
    for file_name, line_no, line in constructor_calls:
        assert file_name == "segment_zip_resolver.py", f"Constructor call leaked to {file_name}:{line_no} -> {line}"
