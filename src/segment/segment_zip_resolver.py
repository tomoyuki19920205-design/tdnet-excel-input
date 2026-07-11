import os
import glob
import time
import json
import hashlib
import tempfile
import shutil
import requests
import logging
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from datetime import datetime, timezone
from src.jquants.adapter import get_file_url
from src.segment.zip_identity_verifier import (
    TrustedProvenance,
    extract_actual_metadata_from_zip,
    PROVENANCE_VERSION,
)

logger = logging.getLogger(__name__)

@dataclass
class ZipResolveResult:
    zip_path: Optional[str]
    source: str
    status: str
    error_reason: str
    cache_hit: bool
    downloaded: bool
    # 新規追加フィールド (指示書 §7.3 準拠)
    requested_disclosure_no: str = ""
    zip_sha256: str = ""
    trusted_provenance: Optional[TrustedProvenance] = None
    resolution_kind: str = ""  # "exact_cache", "official_download", "verified_linked_cache"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_sidecar_provenance(zip_path: str) -> Optional[TrustedProvenance]:
    """sidecar (方式B) ファイルから TrustedProvenance を復元する。

    ZIP 実体との完全一致検証 (schema, hash, size, ticker, period, quarter, internal_id) を行う。
    """
    sidecar_path = zip_path + ".provenance.json"
    if not os.path.exists(sidecar_path):
        return None

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 最低限の必須項目照合
        if data.get("schema_version") != PROVENANCE_VERSION:
            logger.warning("[RESOLVER] Sidecar schema mismatch: %s", data.get("schema_version"))
            return None
        if data.get("source") != "jquants":
            logger.warning("[RESOLVER] Sidecar source mismatch: %s", data.get("source"))
            return None

        # 現在の ZIP 実体の hash と size 照合
        curr_hash = _sha256_file(zip_path)
        curr_size = os.path.getsize(zip_path)
        if data.get("zip_sha256") != curr_hash or data.get("downloaded_size") != curr_size:
            logger.warning("[RESOLVER] Sidecar zip hash/size mismatch")
            return None

        # ZIP から抽出した実メタデータと sidecar の照合
        meta = extract_actual_metadata_from_zip(zip_path)
        if (data.get("internal_document_id") != meta["internal_document_id"] or
                data.get("ticker") != meta["ticker"] or
                data.get("period") != meta["period"] or
                data.get("quarter") != meta["quarter"] or
                data.get("document_type") != meta["document_type"]):
            logger.warning("[RESOLVER] Sidecar metadata mismatch with ZIP content")
            return None

        # 信頼に足る provenance を構築して返却
        return TrustedProvenance(
            source=data["source"],
            requested_disclosure_no=data["requested_disclosure_no"],
            requested_file_type=data.get("requested_file_type", "x"),
            resolved_by_function=data.get("resolved_by_function", "get_file_url"),
            official_request_succeeded=True,
            response_status=200,
            downloaded_size=curr_size,
            downloaded_sha256=curr_hash,
            internal_document_id=meta["internal_document_id"],
            ticker=meta["ticker"],
            period=meta["period"],
            quarter=meta["quarter"],
            document_type=meta["document_type"],
            resolved_at=data.get("fetched_at", ""),
            provenance_version=data["schema_version"],
        )
    except Exception as e:
        logger.warning("[RESOLVER] Failed to load sidecar provenance: %s", e)
        return None


def _write_sidecar_provenance(zip_path: str, prov: TrustedProvenance) -> None:
    """provenance を sidecar (方式B) として保存する。

    temp file へ書込み、fsync を行い、atomic replace。
    """
    sidecar_path = zip_path + ".provenance.json"
    data = {
        "schema_version": prov.provenance_version,
        "source": prov.source,
        "requested_disclosure_no": prov.requested_disclosure_no,
        "requested_file_type": prov.requested_file_type,
        "internal_document_id": prov.internal_document_id,
        "zip_sha256": prov.downloaded_sha256,
        "downloaded_size": prov.downloaded_size,
        "ticker": prov.ticker,
        "period": prov.period,
        "quarter": prov.quarter,
        "document_type": prov.document_type,
        "fetched_at": prov.resolved_at,
        "resolved_by_function": prov.resolved_by_function,
    }

    temp_dir = os.path.dirname(sidecar_path)
    os.makedirs(temp_dir, exist_ok=True)
    
    fd, temp_path = tempfile.mkstemp(dir=temp_dir, suffix=".provenance_tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        # atomic replace
        if os.path.exists(sidecar_path):
            os.remove(sidecar_path)
        os.rename(temp_path, sidecar_path)
    except Exception as e:
        logger.error("[RESOLVER] Failed to write sidecar provenance: %s", e)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def resolve_xbrl_zip(
    doc_id: str,
    ticker: str,
    expected_quarter: str,
    expected_period: str = "",
    local_archive_dir: str = "data/xbrl_archive",
    cache_dir: str = "data/tdnet_cache",
    allow_jquants_fetch: bool = True,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    timeout_seconds: float = 15.0,
    persist_provenance: bool = True,
) -> ZipResolveResult:
    """Finds or downloads an XBRL zip file for a given doc_id.

    1. Checks local_archive_dir (data/xbrl_archive)
    2. Checks cache_dir (data/tdnet_cache)
    3. Fetches from J-Quants API if allow_jquants_fetch is True
    """
    # 探索対象候補パスのリスト作成
    candidate_paths = []
    
    # 1. local_archive 内のパターン
    archive_pattern1 = os.path.join(local_archive_dir, f"{ticker}_*", f"xbrl_{doc_id}.zip")
    archive_pattern2 = os.path.join(local_archive_dir, f"{ticker}_*", f"*_{doc_id}.zip")
    
    for m in glob.glob(archive_pattern1) + glob.glob(archive_pattern2):
        if m.endswith(f"{doc_id}.zip") or f"xbrl_{doc_id}.zip" in m:
            if os.path.exists(m) and os.path.getsize(m) > 0:
                candidate_paths.append((m, "local_archive"))
                
    # 2. tdnet_cache 内のパス
    cache_path_dir = os.path.join(cache_dir, str(doc_id))
    cache_zip = os.path.join(cache_path_dir, "xbrl.zip")
    if os.path.exists(cache_zip) and os.path.getsize(cache_zip) > 0:
        candidate_paths.append((cache_zip, "tdnet_cache"))

    # 既存の候補パスを評価
    for path, src in candidate_paths:
        try:
            meta = extract_actual_metadata_from_zip(path)
            # Path A: exact match cache
            if meta.get("internal_document_id") == doc_id:
                return ZipResolveResult(
                    zip_path=path,
                    source=src,
                    status="FOUND_CACHE",
                    error_reason="",
                    cache_hit=True,
                    downloaded=False,
                    requested_disclosure_no=doc_id,
                    zip_sha256=_sha256_file(path),
                    trusted_provenance=None,
                    resolution_kind="exact_cache",
                )
            
            # Path B: linked cache (sidecar 読み込み検証)
            prov = _load_sidecar_provenance(path)
            if prov:
                return ZipResolveResult(
                    zip_path=path,
                    source=src,
                    status="FOUND_CACHE_LINKED",
                    error_reason="",
                    cache_hit=True,
                    downloaded=False,
                    requested_disclosure_no=doc_id,
                    zip_sha256=prov.downloaded_sha256,
                    trusted_provenance=prov,
                    resolution_kind="verified_linked_cache",
                )
        except Exception as e:
            logger.warning("[RESOLVER] Error evaluating existing file %s: %s", path, e)

    # 3. Fetch from J-Quants (新規公式ダウンロード、または linked cache で sidecar なし時の再ダウンロード検証)
    if not allow_jquants_fetch:
        return ZipResolveResult(
            zip_path=None,
            source="not_found",
            status="SKIPPED_NO_FETCH_ALLOWED",
            error_reason="Not found locally and fetch disabled",
            cache_hit=False,
            downloaded=False,
        )

    # J-Quants APIからの取得試行
    for attempt in range(max_retries):
        try:
            url_data = get_file_url(doc_id, 'x', timeout_sec=timeout_seconds)
            if not url_data or 'xbrl' not in url_data:
                return ZipResolveResult(
                    zip_path=None,
                    source="not_found",
                    status="JQUANTS_URL_NOT_FOUND",
                    error_reason="No xbrl URL returned from J-Quants",
                    cache_hit=False,
                    downloaded=False,
                )

            url = url_data['xbrl']
            r = requests.get(url, stream=True, timeout=timeout_seconds)
            if r.status_code == 200:
                # ダウンロード用に一時ディレクトリを作成
                tmp_download_dir = tempfile.mkdtemp(prefix="jquants_download_")
                tmp_zip_path = os.path.join(tmp_download_dir, "downloaded.zip")
                try:
                    with open(tmp_zip_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    # 正常性検証とメタデータ抽出
                    meta = extract_actual_metadata_from_zip(tmp_zip_path)
                    if (not meta["ticker"] or not meta["period"] or not meta["quarter"] or
                            not meta["document_type"] or not meta["internal_document_id"]):
                        raise ValueError(
                            "STOP_PHASE9FR_ACTUAL_DOCUMENT_METADATA_UNRESOLVED: "
                            f"Failed to extract metadata from official download. Got: {meta}"
                        )
                        
                    sha = _sha256_file(tmp_zip_path)
                    prov = TrustedProvenance(
                        source="jquants",
                        requested_disclosure_no=doc_id,
                        requested_file_type="x",
                        resolved_by_function="get_file_url",
                        official_request_succeeded=True,
                        response_status=200,
                        downloaded_size=os.path.getsize(tmp_zip_path),
                        downloaded_sha256=sha,
                        internal_document_id=meta["internal_document_id"],
                        ticker=meta["ticker"],
                        period=meta["period"],
                        quarter=meta["quarter"],
                        document_type=meta["document_type"],
                        resolved_at=datetime.now(timezone.utc).isoformat(),
                    )
                    
                    # 既存キャッシュがある場合は、ハッシュ照合による verification
                    existing_path = None
                    if os.path.exists(cache_zip):
                        existing_path = cache_zip
                    else:
                        # archive の fallback
                        archive_pattern = os.path.join(local_archive_dir, f"{ticker}_*", f"xbrl_{doc_id}.zip")
                        archive_matches = glob.glob(archive_pattern)
                        if archive_matches:
                            existing_path = archive_matches[0]
                    
                    if existing_path:
                        # 既存 ZIP がある場合の hash 照合 (linked cache の validation)
                        existing_sha = _sha256_file(existing_path)
                        if existing_sha == sha:
                            # 既存 cache とハッシュが完全一致 -> linked cache PASS
                            # sidecar を保存 (persist_provenance=True 時のみ)
                            if persist_provenance:
                                _write_sidecar_provenance(existing_path, prov)
                                
                            shutil.rmtree(tmp_download_dir, ignore_errors=True)
                            return ZipResolveResult(
                                zip_path=existing_path,
                                source="tdnet_cache" if existing_path == cache_zip else "local_archive",
                                status="FOUND_CACHE_LINKED_VERIFIED",
                                error_reason="",
                                cache_hit=True,
                                downloaded=False,
                                requested_disclosure_no=doc_id,
                                zip_sha256=existing_sha,
                                trusted_provenance=prov,
                                resolution_kind="verified_linked_cache",
                            )
                        else:
                            # ハッシュ不一致 -> 拒否
                            shutil.rmtree(tmp_download_dir, ignore_errors=True)
                            return ZipResolveResult(
                                zip_path=None,
                                source="fetch_failed",
                                status="ZIP_HASH_MISMATCH",
                                error_reason="Downloaded ZIP hash does not match existing local cache",
                                cache_hit=False,
                                downloaded=False,
                            )
                    
                    # 新規ダウンロード保存
                    os.makedirs(cache_path_dir, exist_ok=True)
                    shutil.move(tmp_zip_path, cache_zip)
                    if persist_provenance:
                        _write_sidecar_provenance(cache_zip, prov)
                        
                    shutil.rmtree(tmp_download_dir, ignore_errors=True)
                    return ZipResolveResult(
                        zip_path=cache_zip,
                        source="jquants_download",
                        status="DOWNLOADED_FROM_JQUANTS",
                        error_reason="",
                        cache_hit=False,
                        downloaded=True,
                        requested_disclosure_no=doc_id,
                        zip_sha256=sha,
                        trusted_provenance=prov,
                        resolution_kind="official_download",
                    )
                except Exception as ex:
                    shutil.rmtree(tmp_download_dir, ignore_errors=True)
                    raise ex

            elif r.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(backoff_base_seconds * (2 ** attempt))
                    continue
                else:
                    return ZipResolveResult(
                        zip_path=None,
                        source="fetch_failed",
                        status="JQUANTS_RATE_LIMIT_RETRY_EXHAUSTED",
                        error_reason="Rate limit exceeded max retries",
                        cache_hit=False,
                        downloaded=False,
                    )
            else:
                return ZipResolveResult(
                    zip_path=None,
                    source="fetch_failed",
                    status="JQUANTS_FETCH_FAILED",
                    error_reason=f"HTTP {r.status_code}",
                    cache_hit=False,
                    downloaded=False,
                )

        except Exception as e:
            if "STOP_PHASE9FR_ACTUAL_DOCUMENT_METADATA_UNRESOLVED" in str(e):
                raise e
            if '429' in str(e):
                if attempt < max_retries - 1:
                    time.sleep(backoff_base_seconds * (2 ** attempt))
                    continue
                else:
                    return ZipResolveResult(
                        zip_path=None,
                        source="fetch_failed",
                        status="JQUANTS_RATE_LIMIT_RETRY_EXHAUSTED",
                        error_reason="Rate limit exceeded max retries",
                        cache_hit=False,
                        downloaded=False,
                    )
            return ZipResolveResult(
                zip_path=None,
                source="fetch_failed",
                status="JQUANTS_FETCH_FAILED",
                error_reason=str(e),
                cache_hit=False,
                downloaded=False,
            )

    return ZipResolveResult(
        zip_path=None,
        source="fetch_failed",
        status="JQUANTS_FETCH_FAILED",
        error_reason="Unknown error during fetch",
        cache_hit=False,
        downloaded=False,
    )
