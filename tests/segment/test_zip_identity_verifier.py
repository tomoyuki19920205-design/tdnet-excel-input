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

def _make_zip(path: Path, internal_id: str) -> str:
    """最小限の有効な ZIP を生成する。内部エントリ名に internal_id を含める。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{internal_id}_manifest.xml", b"<xbrl/>".decode())
        zf.writestr(f"{internal_id}_data.xml", b"<data/>".decode())
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _make_zip_multi(path: Path, internal_ids: list[str]) -> str:
    """複数の internal ID を含む ZIP を生成する。"""
    with zipfile.ZipFile(path, "w") as zf:
        for iid in internal_ids:
            zf.writestr(f"{iid}_data.xml", b"<data/>".decode())
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
    official_request_succeeded: bool = True,
    response_status: int = 200,
) -> TrustedProvenance:
    return TrustedProvenance(
        source=source,
        requested_disclosure_no=requested_id,
        requested_file_type="x",
        resolved_by_function="get_file_url",
        official_request_succeeded=official_request_succeeded,
        response_status=response_status,
        downloaded_size=100,
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
# Test A: 完全一致 → exact_document_id_match
# ================================================================
def test_a_exact_match(tmp_path):
    """requested ID と internal ID が一致する → 経路 A PASS。provenance 不要。"""
    req_id = "20260101000001"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, req_id)

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=None,  # provenance 不要
    )
    assert v.passed is True
    assert v.verdict == "exact_document_id_match"
    assert v.rejection_reason == ""
    assert v.internal_id == req_id


# ================================================================
# Test B: 公式関連 XBRL → official_linked_xbrl_match
# ================================================================
def test_b_official_linked_xbrl(tmp_path):
    """requested ID と internal ID が異なるが、全条件を満たす TrustedProvenance がある → 経路 B PASS。"""
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
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
    assert v.rejection_reason == ""
    assert v.internal_id == int_id


# ================================================================
# Test C: provenance なし、ID 不一致 → provenance_missing
# ================================================================
def test_c_provenance_missing(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"  # 不一致
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)

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
# Test D: hash 不一致 → provenance_hash_mismatch
# ================================================================
def test_d_hash_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256="aabbcc" + "00" * 29,  # 故意に違う SHA-256
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
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id="20260199999999",  # 別の requested ID
        internal_id=int_id,
        sha256=sha,
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
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TZZZ",  # 別 ticker
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
    assert v.rejection_reason == "ticker_mismatch"


# ================================================================
# Test G: 別期間 ZIP → period_mismatch
# ================================================================
def test_g_period_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        period="2026-03-31",  # 別期間
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
    assert v.rejection_reason == "period_mismatch"


# ================================================================
# Test H: 別 quarter ZIP → quarter_mismatch
# ================================================================
def test_h_quarter_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        quarter="2Q",  # 別 quarter
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
    assert v.rejection_reason == "quarter_mismatch"


# ================================================================
# Test I: 許可されていない document type → document_type_mismatch
# ================================================================
def test_i_document_type_mismatch(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        document_type="yuho_securities_report",  # 有価証券報告書など、許可外
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
# Test J: 複数 internal ID 混在 → multiple_internal_document_ids
# ================================================================
def test_j_multiple_internal_ids(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip_multi(zip_path, ["20260102111111", "20260102222222"])

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
    )
    assert v.passed is False
    assert v.rejection_reason == "multiple_internal_document_ids"


# ================================================================
# Test K: 0 バイト ZIP → zero_byte_zip
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
    )
    assert v.passed is False
    assert v.rejection_reason == "zero_byte_zip"


# ================================================================
# Test L: 破損 ZIP → broken_zip
# ================================================================
def test_l_broken_zip(tmp_path):
    req_id = "20260101000050"
    zip_path = tmp_path / f"{req_id}.zip"
    zip_path.write_bytes(b"NOT_A_ZIP_FILE_AT_ALL")

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
    )
    assert v.passed is False
    assert v.rejection_reason == "broken_zip"


# ================================================================
# Test M: cache provenance 再利用 → linked PASS (ネットワークなし)
# ================================================================
def test_m_cache_provenance_reuse(tmp_path):
    """保存済み provenance と現在 ZIP の hash が一致 → ネットワークなしで linked PASS。"""
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    # 保存済み provenance をシミュレート
    cached_prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
    )

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",
        expected_quarter="1Q",
        trusted_provenance=cached_prov,
    )
    assert v.passed is True
    assert v.verdict == "official_linked_xbrl_match"


# ================================================================
# Test N: cache 改ざん (ZIP bytes が変化) → provenance_hash_mismatch
# ================================================================
def test_n_cache_tampered(tmp_path):
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    original_sha = _sha256(zip_path)

    # ZIP を改ざん
    zip_path.write_bytes(zip_path.read_bytes() + b"TAMPERED")

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=original_sha,  # 変更前の hash
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
    assert v.rejection_reason in ("broken_zip", "provenance_hash_mismatch")


# ================================================================
# Test O: 9982 相当フィクスチャ (実 ID 使用はテスト内のみ)
# ================================================================
def test_o_9982_equivalent_fixture(tmp_path):
    """requested ID と internal ID が異なる公式関連 XBRL。linked PASS を確認。"""
    req_id = "20260709590450"   # summary disclosure_no (fixture 専用)
    int_id = "20260710399820"   # J-Quants 返却 internal ID (fixture 専用)
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="9982_fixture",
        period="2027-02-28",
        quarter="1Q",
        document_type="attachment_xbrl",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="9982_fixture",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is True
    assert v.verdict == "official_linked_xbrl_match"
    assert v.internal_id == int_id


# ================================================================
# Test P: 7601 相当フィクスチャ → exact PASS
# ================================================================
def test_p_7601_equivalent_fixture(tmp_path):
    """完全一致 ZIP (7601 相当)。exact PASS で既存動作不変を確認。"""
    req_id = "20260709590505"   # fixture 専用
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, req_id)  # 内部 ID = requested ID

    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="7601_fixture",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=None,  # exact match なので不要
    )
    assert v.passed is True
    assert v.verdict == "exact_document_id_match"


# ================================================================
# Test Q: 無関係な同一 ticker ZIP (別期間) → period_mismatch
# ================================================================
def test_q_same_ticker_different_period(tmp_path):
    """ticker だけ一致する別期間の ZIP → 拒否。"""
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        ticker="TXXX",  # ticker は一致
        period="2026-03-31",  # 別期間
        quarter="1Q",
    )
    v = verify_zip_identity(
        zip_path=str(zip_path),
        requested_disclosure_no=req_id,
        expected_ticker="TXXX",
        expected_period="2027-03-31",  # 期待期間と不一致
        expected_quarter="1Q",
        trusted_provenance=prov,
    )
    assert v.passed is False
    assert v.rejection_reason == "period_mismatch"


# ================================================================
# Test R: 来歴の偽装 (外部入力で作った provenance は source 不一致で拒否)
# ================================================================
def test_r_provenance_source_forgery(tmp_path):
    """外部入力で source を偽った provenance は untrusted_source で拒否。"""
    req_id = "20260101000050"
    int_id = "20260102399999"
    zip_path = tmp_path / f"{req_id}.zip"
    _make_zip(zip_path, int_id)
    sha = _sha256(zip_path)

    prov = _make_provenance(
        requested_id=req_id,
        internal_id=int_id,
        sha256=sha,
        source="external_input",  # 偽装: jquants でない
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
