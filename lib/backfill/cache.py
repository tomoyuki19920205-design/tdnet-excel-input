"""lib/backfill/cache.py — filing_id 単位のキャッシュ管理

原本 (PDF/XBRL) と中間成果物 (抽出結果 JSON) を統一ディレクトリで管理する。
worker が cache の存在を見て再ダウンロード回避できる。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("backfill.cache")
JST = timezone(timedelta(hours=9))


@dataclass
class CachePaths:
    """filing_id ごとのキャッシュパス群。"""
    cache_dir: Path
    metadata_json: Path
    source_pdf: Path
    xbrl_zip: Path
    extract_financials_result_json: Path
    extract_segments_result_json: Path
    quarantine_json: Path
    logs_jsonl: Path


def get_cache_dir(cache_root: str, filing_id: str) -> Path:
    """cache_root/{filing_id}/ を返す。"""
    return Path(cache_root) / filing_id


def ensure_cache_layout(cache_root: str, filing_id: str) -> CachePaths:
    """キャッシュディレクトリとパスを準備する。ディレクトリは自動作成。"""
    d = get_cache_dir(cache_root, filing_id)
    d.mkdir(parents=True, exist_ok=True)
    return CachePaths(
        cache_dir=d,
        metadata_json=d / "metadata.json",
        source_pdf=d / "source.pdf",
        xbrl_zip=d / "xbrl.zip",
        extract_financials_result_json=d / "extract_financials_result.json",
        extract_segments_result_json=d / "extract_segments_result.json",
        quarantine_json=d / "quarantine.json",
        logs_jsonl=d / "logs.jsonl",
    )


def write_metadata(paths: CachePaths, filing) -> None:
    """metadata.json を保存する。filing は FilingInfo または dict。"""
    if hasattr(filing, "__dataclass_fields__"):
        data = {
            "filing_id": filing.filing_id,
            "ticker": filing.ticker,
            "disclosure_date": filing.disclosure_date,
            "title": filing.title,
            "doc_type": filing.doc_type,
            "source_url": filing.doc_url,
            "xbrl_url": filing.xbrl_url,
            "listing_source": filing.listing_source,
            "has_xbrl": filing.has_xbrl,
            "xbrl_url_inferred": getattr(filing, "xbrl_url_inferred", False),
            "has_pdf": True,
            "created_at": datetime.now(JST).isoformat(),
        }
    else:
        data = dict(filing)
        data.setdefault("created_at", datetime.now(JST).isoformat())

    paths.metadata_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def has_pdf(paths: CachePaths) -> bool:
    return paths.source_pdf.exists() and paths.source_pdf.stat().st_size > 0


def has_xbrl(paths: CachePaths) -> bool:
    return paths.xbrl_zip.exists() and paths.xbrl_zip.stat().st_size > 0


def save_extract_result(path: Path, payload: dict | list) -> None:
    """抽出結果 JSON を保存。"""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def save_extract_financials_result(paths: CachePaths, payload: dict | list) -> None:
    save_extract_result(paths.extract_financials_result_json, payload)


def save_extract_segments_result(paths: CachePaths, payload: dict | list) -> None:
    save_extract_result(paths.extract_segments_result_json, payload)


def save_quarantine(paths: CachePaths, payload: dict) -> None:
    """quarantine 情報を保存。"""
    paths.quarantine_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def append_filing_log(paths: CachePaths, event: dict) -> None:
    """filing 単位ログに1行追記。"""
    event.setdefault("timestamp", datetime.now(JST).isoformat())
    with open(paths.logs_jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


# ============================================================
# XBRL archive → cache 連携
# ============================================================

_DEFAULT_XBRL_ARCHIVE = "data/xbrl_archive"


def resolve_xbrl_from_archive(
    ticker: str,
    paths: CachePaths,
    archive_root: str = _DEFAULT_XBRL_ARCHIVE,
) -> tuple[str | None, str]:
    """xbrl_archive から ticker 前方一致で ZIP を検索し、キャッシュにコピーする。

    Returns:
        (xbrl_path, xbrl_source)
        xbrl_source: "cache" | "archive" | None に対応する文字列
    """
    import shutil

    # 1. cache 内既存
    if has_xbrl(paths):
        logger.debug(f"[cache] xbrl cache hit: {paths.xbrl_zip}")
        return str(paths.xbrl_zip), "cache"

    # 2. archive 検索
    archive_dir = Path(archive_root)
    if not archive_dir.exists():
        return None, "none"

    prefix = f"{ticker}_"
    candidates = sorted(
        [f for f in archive_dir.iterdir()
         if f.name.startswith(prefix) and f.suffix == ".zip" and f.stat().st_size > 0],
        key=lambda p: p.name,
        reverse=True,  # 最新(日付降順)を優先
    )

    if not candidates:
        return None, "none"

    best = candidates[0]
    try:
        shutil.copy2(str(best), str(paths.xbrl_zip))
        logger.info(f"[cache] xbrl archive → cache: {best.name} → {paths.xbrl_zip}")
        return str(paths.xbrl_zip), "archive"
    except Exception as e:
        logger.warning(f"[cache] xbrl archive copy failed: {best.name} → {e}")
        return None, "none"
