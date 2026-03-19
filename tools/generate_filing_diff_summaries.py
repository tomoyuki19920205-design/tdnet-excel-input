#!/usr/bin/env python3
# ============================================================
# generate_filing_diff_summaries.py
# — 決算短信AI差分要約 E2Eパイプライン
# ============================================================
"""
CLI:
  python tools/generate_filing_diff_summaries.py --since 2026-03-01
  python tools/generate_filing_diff_summaries.py --ticker 6623
  python tools/generate_filing_diff_summaries.py --doc-id xxx
  python tools/generate_filing_diff_summaries.py --dry-run --ticker 6623
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows UTF-8 強制
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker
from src.filing_diff.previous_doc_resolver import (
    find_previous_earnings_doc,
    resolve_comparison_target,
)
from src.filing_diff.text_extractor import (
    extract_disclosure_text,
)
from src.filing_diff.section_diff import diff_sections
from src.filing_diff.ai_summary import (
    generate_ai_diff_summary,
    validate_ai_summary_json,
    _DEFAULT_SUMMARY,
)

logger = logging.getLogger("filing_diff")
JST = timezone(timedelta(hours=9))

# ============================================================
# SQLite テーブル
# ============================================================

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS filing_diff_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT,
    current_doc_id TEXT,
    previous_doc_id TEXT,
    current_title TEXT,
    previous_title TEXT,
    disclosed_at TEXT,
    period TEXT,
    quarter TEXT,
    comparison_rule TEXT,
    comparison_confidence TEXT,
    extraction_status TEXT,
    diff_status TEXT,
    ai_status TEXT,
    summary_overall TEXT,
    demand_change TEXT,
    profit_factor_change TEXT,
    guidance_change TEXT,
    risk_change TEXT,
    new_keywords_json TEXT,
    notable_added_phrases_json TEXT,
    notable_removed_phrases_json TEXT,
    tone_change TEXT,
    confidence TEXT,
    caution_note TEXT,
    raw_diff_payload_json TEXT,
    raw_ai_response_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def _ensure_table(conn: sqlite3.Connection):
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


# ============================================================
# PDF 検索ヘルパー
# ============================================================

# SHA-256 → ファイルパス逆引きキャッシュ
_sha256_cache: dict[str, str] | None = None


def _build_sha256_cache(docs_dir: str) -> dict[str, str]:
    """docs_dir 内のファイルの SHA-256 → パス マップを構築"""
    global _sha256_cache
    if _sha256_cache is not None:
        return _sha256_cache

    import hashlib
    _sha256_cache = {}
    if not os.path.isdir(docs_dir):
        return _sha256_cache

    for fname in os.listdir(docs_dir):
        if not (fname.endswith(".pdf") or fname.endswith(".zip")):
            continue
        fpath = os.path.join(docs_dir, fname)
        h = hashlib.sha256()
        with open(fpath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        _sha256_cache[h.hexdigest()] = fpath
    return _sha256_cache


def _find_pdf_path(doc_id: str, docs_dir: str) -> str | None:
    """source_doc_id から PDF/ZIP ファイルパスを探す"""
    if not doc_id or not os.path.isdir(docs_dir):
        return None

    # 1. SHA-256 ハッシュによる逆引き（バックフィル済みデータ用）
    cache = _build_sha256_cache(docs_dir)
    if doc_id in cache:
        return cache[doc_id]

    # 2. ファイル名パターンマッチ（TDnet開示番号ベース）
    for fname in os.listdir(docs_dir):
        if not (fname.endswith(".pdf") or fname.endswith(".zip")):
            continue
        base = os.path.splitext(fname)[0]
        if doc_id in base or base in doc_id:
            return os.path.join(docs_dir, fname)

    return None


def _find_pdf_by_source_url(source_url: str, docs_dir: str) -> str | None:
    """source_url から開示番号を抽出してPDFを検索"""
    if not source_url:
        return None
    # URL例: https://www.release.tdnet.info/inbs/140120260304575305.pdf
    import re
    m = re.search(r"(\d{18})\.pdf", source_url)
    if m:
        tdnet_id = m.group(1)
        pdf_path = os.path.join(docs_dir, f"{tdnet_id}.pdf")
        if os.path.exists(pdf_path):
            return pdf_path
    return None


# ============================================================
# 1件処理
# ============================================================

def _process_single_doc(
    row: dict,
    db_conn: sqlite3.Connection,
    docs_dir: str,
    dry_run: bool = False,
    skip_ai: bool = False,
) -> dict:
    """1件の決算短信について差分要約を生成する"""
    ticker_raw = row["company_code"]
    ticker = normalize_ticker(ticker_raw)
    period = row["fiscal_year_end"]
    quarter = row["quarter"]
    doc_id = row.get("source_doc_id") or ""
    source_url = row.get("source_url") or ""

    result = {
        "ticker": ticker,
        "period": period,
        "quarter": quarter,
        "status": "started",
    }

    # ---- Phase 1: 比較対象特定 ----
    prev = find_previous_earnings_doc(ticker_raw, period, quarter, db_conn)
    if prev is None or prev.previous_doc_id is None:
        # 原因を分類: 論理的に対象がない vs リンク欠損
        reason = resolve_comparison_target(period, quarter)
        if reason is None:
            # 比較対象Qが存在しない（例: 異常なquarter値）
            status_code = "no_previous_doc_logical"
        elif prev and prev.comparison_confidence == "low" and "見つかりません" in prev.comparison_note:
            # 比較対象行が存在するはずだが source_doc_id が NULL
            status_code = "no_previous_doc_link_missing"
        else:
            status_code = "no_previous_doc_logical"

        result["status"] = status_code
        logger.info(
            f"[DIFF] ticker={ticker} {period} {quarter} "
            f"status={status_code}"
        )
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         extraction_status="skipped",
                         diff_status=status_code,
                         ai_status="skipped",
                         comparison_rule=prev.comparison_rule if prev else "",
                         comparison_confidence=prev.comparison_confidence if prev else "low")
        return result

    logger.info(
        f"[DIFF] ticker={ticker} {period} {quarter} "
        f"comparison={prev.comparison_rule} "
        f"confidence={prev.comparison_confidence}"
    )

    # ---- Phase 2: 本文抽出 ----
    # 現在PDF
    curr_pdf = _find_pdf_by_source_url(source_url, docs_dir)
    if not curr_pdf:
        curr_pdf = _find_pdf_path(doc_id, docs_dir)

    # 前回PDF
    prev_row = db_conn.execute(
        "SELECT source_url FROM quarterly_results "
        "WHERE source_doc_id=? LIMIT 1",
        (prev.previous_doc_id,)
    ).fetchone()
    prev_url = prev_row[0] if prev_row else ""
    prev_pdf = _find_pdf_by_source_url(prev_url, docs_dir)
    if not prev_pdf:
        prev_pdf = _find_pdf_path(prev.previous_doc_id, docs_dir)

    if not curr_pdf:
        result["status"] = "extraction_failed_current"
        logger.warning(f"[DIFF] ticker={ticker} current PDF not found")
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         previous_doc_id=prev.previous_doc_id,
                         extraction_status="failed_current_pdf",
                         diff_status="skipped", ai_status="skipped",
                         comparison_rule=prev.comparison_rule,
                         comparison_confidence=prev.comparison_confidence)
        return result

    if not prev_pdf:
        result["status"] = "extraction_failed_previous"
        logger.warning(f"[DIFF] ticker={ticker} previous PDF not found")
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         previous_doc_id=prev.previous_doc_id,
                         extraction_status="failed_previous_pdf",
                         diff_status="skipped", ai_status="skipped",
                         comparison_rule=prev.comparison_rule,
                         comparison_confidence=prev.comparison_confidence)
        return result

    curr_text = extract_disclosure_text(curr_pdf)
    prev_text = extract_disclosure_text(prev_pdf)

    logger.info(
        f"[DIFF] extract current={curr_text.extraction_status} "
        f"({len(curr_text.sections)} sections) "
        f"previous={prev_text.extraction_status} "
        f"({len(prev_text.sections)} sections)"
    )

    if curr_text.extraction_status != "ok":
        result["status"] = "extraction_failed"
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         previous_doc_id=prev.previous_doc_id,
                         extraction_status="failed",
                         diff_status="skipped", ai_status="skipped",
                         comparison_rule=prev.comparison_rule,
                         comparison_confidence=prev.comparison_confidence)
        return result

    # ---- Phase 3+4: セクション差分 ----
    sections_diff: list[dict] = []
    all_keywords: list[str] = []

    # マッチするセクション同士を比較
    prev_sections_map = {
        s.section_name_normalized: s for s in prev_text.sections
    }

    for curr_sec in curr_text.sections:
        prev_sec = prev_sections_map.get(curr_sec.section_name_normalized)
        if prev_sec is None:
            # 前回にないセクション → 全文added
            sections_diff.append({
                "section_name": curr_sec.section_name_normalized,
                "added": [curr_sec.section_text],
                "removed": [],
                "changed": [],
                "keywords": [],
                "diff_score": 1.0,
            })
            continue

        diff = diff_sections(
            prev_sec.section_text,
            curr_sec.section_text,
            section_name=curr_sec.section_name_normalized,
        )
        sections_diff.append({
            "section_name": diff.section_name,
            "added": diff.added_sentences,
            "removed": diff.removed_sentences,
            "changed": [(p, c) for p, c in diff.changed_pairs],
            "keywords": diff.keywords,
            "diff_score": diff.diff_score,
        })
        all_keywords.extend(diff.keywords)

    total_diff_score = (
        sum(sd["diff_score"] for sd in sections_diff) / max(len(sections_diff), 1)
    )

    logger.info(
        f"[DIFF] sections matched={len(sections_diff)} "
        f"avg_diff_score={total_diff_score:.3f} "
        f"keywords={list(set(all_keywords))}"
    )

    # 差分がほぼゼロ
    if total_diff_score < 0.01 and len(sections_diff) > 0:
        result["status"] = "diff_empty"
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         previous_doc_id=prev.previous_doc_id,
                         extraction_status="ok", diff_status="diff_empty",
                         ai_status="skipped",
                         comparison_rule=prev.comparison_rule,
                         comparison_confidence=prev.comparison_confidence,
                         raw_diff_payload=json.dumps(sections_diff,
                                                     ensure_ascii=False))
        return result

    # ---- Phase 5: AI要約 ----
    diff_payload = {
        "ticker": ticker,
        "company_name": "",
        "current_title": f"{period} {quarter} 決算短信",
        "previous_title": f"{prev.comparison_note}",
        "comparison_confidence": prev.comparison_confidence,
        "sections_diff": sections_diff,
    }

    if skip_ai or dry_run:
        logger.info(
            f"[DIFF] AI要約スキップ (dry_run={dry_run}, skip_ai={skip_ai})"
        )
        result["status"] = "diff_generated"
        result["sections_diff"] = sections_diff
        if not dry_run:
            _save_result(db_conn, ticker, period, quarter, doc_id,
                         previous_doc_id=prev.previous_doc_id,
                         extraction_status="ok", diff_status="completed",
                         ai_status="skipped",
                         comparison_rule=prev.comparison_rule,
                         comparison_confidence=prev.comparison_confidence,
                         raw_diff_payload=json.dumps(sections_diff,
                                                     ensure_ascii=False))
        return result

    ai_result = generate_ai_diff_summary(diff_payload, max_retries=1)
    ai_status = ai_result.pop("_ai_status", "unknown")
    raw_ai_response = ai_result.pop("_raw_response", "")
    ai_error = ai_result.pop("_error", "")

    logger.info(
        f"[DIFF] AI summary ticker={ticker} status={ai_status}"
    )

    if not dry_run:
        _save_result(
            db_conn, ticker, period, quarter, doc_id,
            previous_doc_id=prev.previous_doc_id,
            extraction_status="ok",
            diff_status="completed",
            ai_status=ai_status,
            comparison_rule=prev.comparison_rule,
            comparison_confidence=prev.comparison_confidence,
            raw_diff_payload=json.dumps(sections_diff, ensure_ascii=False),
            raw_ai_response=raw_ai_response,
            ai_summary=ai_result,
        )

    result["status"] = ai_status
    result["ai_summary"] = ai_result
    return result


# ============================================================
# 保存
# ============================================================

def _save_result(
    conn: sqlite3.Connection,
    ticker: str, period: str, quarter: str,
    current_doc_id: str,
    previous_doc_id: str | None = None,
    extraction_status: str = "",
    diff_status: str = "",
    ai_status: str = "",
    comparison_rule: str = "",
    comparison_confidence: str = "",
    raw_diff_payload: str = "",
    raw_ai_response: str = "",
    ai_summary: dict | None = None,
):
    now = datetime.now(JST).isoformat()
    s = ai_summary or {}
    conn.execute(
        """INSERT INTO filing_diff_summaries (
            ticker, current_doc_id, previous_doc_id,
            period, quarter,
            comparison_rule, comparison_confidence,
            extraction_status, diff_status, ai_status,
            summary_overall, demand_change,
            profit_factor_change, guidance_change,
            risk_change, new_keywords_json,
            notable_added_phrases_json,
            notable_removed_phrases_json,
            tone_change, confidence, caution_note,
            raw_diff_payload_json, raw_ai_response_json,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )""",
        (
            ticker, current_doc_id, previous_doc_id,
            period, quarter,
            comparison_rule, comparison_confidence,
            extraction_status, diff_status, ai_status,
            s.get("summary_overall", ""),
            s.get("demand_change", ""),
            s.get("profit_factor_change", ""),
            s.get("guidance_change", ""),
            s.get("risk_change", ""),
            json.dumps(s.get("new_keywords", []), ensure_ascii=False),
            json.dumps(s.get("notable_added_phrases", []), ensure_ascii=False),
            json.dumps(s.get("notable_removed_phrases", []), ensure_ascii=False),
            s.get("tone_change", ""),
            s.get("confidence", ""),
            s.get("caution_note", ""),
            raw_diff_payload, raw_ai_response,
            now, now,
        ),
    )
    conn.commit()


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="決算短信AI差分要約 生成ツール",
    )
    parser.add_argument("--ticker", help="特定tickerのみ処理")
    parser.add_argument("--since", help="この日付以降のデータ (YYYY-MM-DD)")
    parser.add_argument("--doc-id", help="特定doc_idのみ")
    parser.add_argument("--limit", type=int, default=50, help="処理上限")
    parser.add_argument("--dry-run", action="store_true", help="DB保存・AI呼び出しスキップ")
    parser.add_argument("--skip-ai", action="store_true", help="AI呼び出しのみスキップ")
    parser.add_argument("--db", default="decision_db.db", help="SQLiteパス")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = args.db
    if not os.path.isabs(db_path):
        db_path = os.path.join(_PROJECT_ROOT, db_path)

    docs_dir = os.path.join(os.path.dirname(db_path), "docs") \
        if "data" in db_path else os.path.join(_PROJECT_ROOT, "data", "docs")
    # 標準的な docs_dir
    if not os.path.isdir(docs_dir):
        docs_dir = os.path.join(_PROJECT_ROOT, "data", "docs")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)

    # ---- 対象行を取得 ----
    where_clauses = ["source_doc_id IS NOT NULL"]
    params: list = []

    if args.ticker:
        # 4桁/5桁対応
        t = args.ticker
        variants = [t]
        if len(t) == 4:
            variants.append(t + "0")
        elif len(t) == 5 and t.endswith("0"):
            variants.append(t[:-1])
        placeholders = ",".join(["?"] * len(variants))
        where_clauses.append(f"company_code IN ({placeholders})")
        params.extend(variants)

    if args.since:
        where_clauses.append("updated_at >= ?")
        params.append(args.since)

    if args.doc_id:
        where_clauses.append("source_doc_id = ?")
        params.append(args.doc_id)

    where = " AND ".join(where_clauses)
    query = f"""
        SELECT DISTINCT company_code, fiscal_year_end, quarter,
               source_doc_id, source_url
        FROM quarterly_results
        WHERE {where}
        ORDER BY fiscal_year_end DESC, quarter DESC
        LIMIT ?
    """
    params.append(args.limit)

    rows = conn.execute(query, params).fetchall()
    logger.info(f"[DIFF] 対象: {len(rows)} 件")

    # ---- 処理 ----
    stats = {
        "scanned": len(rows),
        "completed": 0,
        "no_prev_logical": 0,
        "no_prev_link_missing": 0,
        "extraction_failed": 0,
        "diff_empty": 0,
        "ai_failed": 0,
        "errors": 0,
    }

    for row in rows:
        try:
            result = _process_single_doc(
                dict(row), conn, docs_dir,
                dry_run=args.dry_run,
                skip_ai=args.skip_ai,
            )
            status = result.get("status", "unknown")
            if status in ("completed", "diff_generated"):
                stats["completed"] += 1
            elif status == "no_previous_doc_logical":
                stats["no_prev_logical"] += 1
            elif status == "no_previous_doc_link_missing":
                stats["no_prev_link_missing"] += 1
            elif "extraction_failed" in status:
                stats["extraction_failed"] += 1
            elif status == "diff_empty":
                stats["diff_empty"] += 1
            elif status == "ai_failed":
                stats["ai_failed"] += 1

        except Exception as e:
            logger.error(
                f"[DIFF] 処理エラー ticker={dict(row).get('company_code')} "
                f"error={e}"
            )
            stats["errors"] += 1

    conn.close()

    # ---- サマリ ----
    print()
    print("=" * 55)
    print("  決算短信AI差分要約 - 処理結果")
    print("=" * 55)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    print("=" * 55)


if __name__ == "__main__":
    main()
