"""tests/segment/test_zip_identity_verifier.py

Phase 9F - ZIP identity verifier の自動テスト (Test A ~ R)

ネットワーク / DB / 実 ZIP への依存なし。
すべて tmp_path にフィクスチャを生成して検証する。
"""
from __future__ import annotations

import hashlib
import io
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from src.segment.zip_identity_verifier import (
    TrustedProvenance,
    ZipIdentityVerdict,
    verify_zip_identity,
    ALLOWED_DOCUMENT_TYPES,
    PROVENANCE_VERSION,
)

# ================================================================
# フィクスチャヘルパー
# ================================================================

def _make_zip(path: Path, internal_id: str, ticker: str = "TXXX", period: str = "2027-03-31", quarter: str = "1Q", doc_type: str = "attachment_xbrl") -> str:
    """エントリ名や ixbrl.htm 等に指定メタデータを含んだダミー ZIP を作成する。"""
    q_map = {"1Q": "1", "2Q": "2", "3Q": "3", "FY": "4"}
    q_num = q_map.get(quarter, "1")
    
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
    
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{internal_id}.xsd", b"")
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{internal_id}-ixbrl.htm", summary_htm_content.encode("utf-8"))
        zf.writestr("XBRLData/Attachment/manifest.xml", f'<manifest><instance id="qcedjpfr" preferredFilename="tse-qcedjpfr-{ticker}0-{period}-01-{internal_id}.xbrl"/></manifest>'.encode("utf-8"))
        if doc_type != "attachment_xbrl" and doc_type in ALLOWED_DOCUMENT_TYPES:
            zf.writestr(f"XBRLData/Attachment/{doc_type}.xbrl", b"")
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _make_zip_multi(path: Path, internal_ids: list[str]) -> str:
    """複数の internal ID を含む ZIP を生成する。"""
    ticker = "TXXX"
    period = "2027-03-31"
    quarter = "1Q"
    q_num = "1"
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
        for iid in internal_ids:
            zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{iid}.xsd", b"")
        zf.writestr(f"XBRLData/Summary/tse-qcedjpsm-{ticker}0-{internal_ids[0]}-ixbrl.htm", summary_htm_content.encode("utf-8"))
        zf.writestr("XBRLData/Attachment/manifest.xml", f'<manifest><instance id="qcedjpfr" preferredFilename="tse-qcedjpfr-{ticker}0-{period}-01-{internal_ids[0]}.xbrl"/></manifest>'.encode("utf-8"))
    return path


def _make_provenance(
    requested_id: str,
    internal_id: str,
    sha256: str,
    ticker: str = "TXXX",
    period: str = "2027-03-31",
    quarter: str = "1Q",
    document_type: str = "attachment_xbrl",
    source: str = "jquants",
    request_succeeded: bool = True,
    response_status: int = 200,
    downloaded_size: int = 100,
) -> TrustedProvenance:
    return TrustedProvenance(
        source=source,
        requested_disclosure_no=requested_id,
        requested_file_type="x",
        resolved_by_function="get_file_url",
        official_request_succeeded=request_succeeded,
        response_status=response_status,
        downloaded_size=downloaded_size,
        downloaded_sha256=sha256,
        internal_document_id=internal_id,
        ticker=ticker,
        period=period,
        quarter=quarter,
        document_type=document_type,
        resolved_at=datetime.now(timezone.utc).isoformat(),
        provenance_version=PROVENANCE_VERSION,
    )


# ================================================================
# Test A: ID 完全一致 ZIP は provenance なしで合格 → exact_document_id_match
# ================================================================
def test_a_exact_match(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, req_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,
    )
    assert v.passed is True
    assert v.verdict == "exact_document_id_match"
    assert v.internal_id == req_id
    assert v.zip_sha256 == sha


# ================================================================
# Test B: 関連書類 ID が一致し provenance あり → official_linked_xbrl_match
# ================================================================
def test_b_official_linked_xbrl(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
        downloaded_size=zip_path.stat().st_size,
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is True
    assert v.verdict == "official_linked_xbrl_match"
    assert v.internal_id == int_id
    assert v.zip_sha256 == sha


# ================================================================
# Test C: 関連書類 ID だが provenance なし → provenance_missing
# ================================================================
def test_c_provenance_missing(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,
    )
    assert v.passed is False
    assert v.rejection_reason == "provenance_missing"


# ================================================================
# Test D: ZIP hash 不一致 → provenance_hash_mismatch
# ================================================================
def test_d_hash_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256="wronghash123456789012345678901234567890123456789012345678901234",
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "provenance_hash_mismatch"


# ================================================================
# Test E: provenance が別 requested ID に紐付く → provenance_requested_id_mismatch
# ================================================================
def test_e_requested_id_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id="20260199999999",  # 別の requested ID
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "provenance_requested_id_mismatch"


# ================================================================
# Test F: 別会社 ZIP → ticker_mismatch
# ================================================================
def test_f_ticker_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    # ZIP内の ticker を "TYYY" (期待値 "TXXX" と異なる) にする
    _make_zip(zip_path, int_id, ticker="TYYY", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TYYY",  # provenance も ZIP に合わせる (偽造防止照合は PASS するが expected と mismatch)
        period="2027-03-31",
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",  # 期待値 "TXXX"
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "ticker_mismatch"


# ================================================================
# Test G: 別期間 ZIP → period_mismatch
# ================================================================
def test_g_period_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    # ZIP 内の period を "2026-03-31" (期待値 "2027-03-31" と異なる) にする
    _make_zip(zip_path, int_id, ticker="TXXX", period="2026-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2026-03-31",  # provenance も ZIP に合わせる
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",  # 期待値 "2027-03-31"
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "period_mismatch"


# ================================================================
# Test H: 別 quarter ZIP → quarter_mismatch
# ================================================================
def test_h_quarter_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    # ZIP 内の quarter を "2Q" にする
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="2Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="2Q",  # provenance も ZIP に合わせる
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",  # 期待値 "1Q"
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "quarter_mismatch"


# ================================================================
# Test I: 許可されていない document type → document_type_mismatch
# ================================================================
def test_i_document_type_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    # 許可されていない doc_type を設定して ZIP を作る
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q", doc_type="yuho_securities_report")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
        document_type="yuho_securities_report",  # 許可外
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "document_type_mismatch"


# ================================================================
# Test J: 複数書類 ID 混在 ZIP は拒否 → multiple_internal_document_ids
# ================================================================
def test_j_multiple_internal_ids(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    # 複数 ID 混在
    _make_zip_multi(zip_path, ["20260102399999", "20260103444444"])

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,
    )
    assert v.passed is False
    assert v.rejection_reason == "multiple_internal_document_ids"


# ================================================================
# Test K: 0 バイト ZIP 拒否 → zero_byte_zip
# ================================================================
def test_k_zero_byte_zip(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    zip_path.write_bytes(b"")

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,
    )
    assert v.passed is False
    assert v.rejection_reason == "zero_byte_zip"


# ================================================================
# Test L: 破損 ZIP 拒否 → broken_zip
# ================================================================
def test_l_broken_zip(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    zip_path.write_bytes(b"PK\x03\x04brokenzipdata123456789")

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,
    )
    assert v.passed is False
    assert v.rejection_reason == "broken_zip"


# ================================================================
# Test M: キャッシュされた provenance の再利用 → linked PASS
# ================================================================
def test_m_cache_provenance_reuse(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    # 既にキャッシュされている provenance
    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
        downloaded_size=zip_path.stat().st_size,
    )
    # verify実行
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is True
    assert v.verdict == "official_linked_xbrl_match"
    assert v.internal_id == int_id


# ================================================================
# Test N: キャッシュ改ざん検出 (ファイル内容変更) → provenance_hash_mismatch
# ================================================================
def test_n_cache_tampered(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    # 正常取得時の provenance
    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
    )

    # ZIP ファイルが改ざん（上書き）されたとする
    zip_path.write_bytes(b"PK\x03\x04tamperedzipdata1234567890")

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    # ハッシュミスマッチ (または破損)
    assert v.rejection_reason in ("provenance_hash_mismatch", "broken_zip")


# ================================================================
# Test O: 9982 相当フィクスチャによる linked verification 疎通確認
# ================================================================
def test_o_9982_equivalent_fixture(tmp_path):
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker=ticker, period=period, quarter=quarter)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker=ticker,
        period=period,
        quarter=quarter,
        downloaded_size=zip_path.stat().st_size,
    )

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker=ticker,
        expected_period=period,
        expected_quarter=quarter,
        trusted_provenance=prov,
    )
    assert v.passed is True
    assert v.verdict == "official_linked_xbrl_match"
    assert v.internal_id == int_id


# ================================================================
# Test P: 7601 相当フィクスチャによる exact verification 疎通確認
# ================================================================
def test_p_7601_equivalent_fixture(tmp_path):
    req_id = "20260709590505"
    ticker = "7601"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, req_id, ticker=ticker, period=period, quarter=quarter)
    sha = _sha256(zip_path)

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker=ticker,
        expected_period=period,
        expected_quarter=quarter,
        trusted_provenance=None,
    )
    assert v.passed is True
    assert v.verdict == "exact_document_id_match"
    assert v.internal_id == req_id


# ================================================================
# Test Q: 同一 ticker・異なる期間の公式 ZIP が渡された場合 → period_mismatch
# ================================================================
def test_q_same_ticker_different_period(tmp_path):
    req_id = "20260709590450"
    int_id = "20260710399820"
    ticker = "9982"
    period = "2027-02-28"
    quarter = "1Q"

    zip_path = tmp_path / f"{req_id}.zip"
    # ZIP内の period を "2026-02-28" (別年度) にする
    _make_zip(zip_path, int_id, ticker=ticker, period="2026-02-28", quarter=quarter)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker=ticker,
        period="2026-02-28",
        quarter=quarter,
    )

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker=ticker,
        expected_period=period,  # 期待値 "2027-02-28"
        expected_quarter=quarter,
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "period_mismatch"


# ================================================================
# Test R: 偽造 provenance による侵入 → untrusted_source
# ================================================================
def test_r_provenance_source_forgery(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id, ticker="TXXX", period="2027-03-31", quarter="1Q")
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        source="external_input",  # 偽: jquants ではない
        ticker="TXXX",
        period="2027-03-31",
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "untrusted_source"
