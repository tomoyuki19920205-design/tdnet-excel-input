#!/usr/bin/env python3
# ============================================================
# backfill_source_doc_links.py
# — quarterly_results.source_doc_id バックフィル
# ============================================================
"""
data/docs/ 配下の PDF/ZIP ファイルから ticker, period, quarter を
抽出し、quarterly_results の source_doc_id が NULL の行に対して
対応する開示ドキュメントのリンクを補完する。

動作:
1. data/docs/ のファイルを走査
2. 各ファイルの SHA-256 を計算
3. ファイルからテキスト抽出 (PDF / ZIP内qualitative.htm)
4. ticker/period/quarter 抽出 (テキスト + ZIPファイル名フォールバック)
5. quarterly_results の NULL 行とマッチング
6. high confidence のみ自動更新
7. medium はログ + review CSV 出力
8. low はスキップ

集計値整合: scanned = already_linked + matched_high + matched_medium + unmatched
(ファイル単位。1ファイルの最良マッチ confidence で分類)

CLI:
  python tools/backfill_source_doc_links.py --dry-run
  python tools/backfill_source_doc_links.py --limit 20
  python tools/backfill_source_doc_links.py --csv data/review.csv
"""
from __future__ import annotations

import argparse
import csv as csv_mod
import hashlib
import io
import json as json_mod
import logging
import os
import re
import sqlite3
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.common_ticker import normalize_ticker

logger = logging.getLogger("backfill")

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore


# ============================================================
# データ構造
# ============================================================

@dataclass
class DocCandidate:
    """ファイルから抽出した候補情報"""
    filename: str
    filepath: str
    sha256: str
    ticker: str = ""
    period: str = ""
    quarter: str = ""
    title_snippet: str = ""
    match_confidence: str = ""  # high / medium / low
    match_note: str = ""


# ============================================================
# PDF/ZIP テキスト抽出
# ============================================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 3) -> str:
    """PDFバイト列からテキスト抽出"""
    if pdfplumber is None:
        return ""
    try:
        pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in pdf.pages[:max_pages]:
            t = page.extract_text()
            if t:
                parts.append(t)
        pdf.close()
        return "\n".join(parts)
    except Exception as e:
        logger.debug(f"PDF text extraction failed: {e}")
        return ""


def _strip_html_tags(html: str) -> str:
    """HTMLタグを除去してプレーンテキストにする"""
    import html as html_mod
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# iXBRLファイル名パターン
_IXBRL_FILENAME_RE = re.compile(r"tse-[qs]cedjps[my]-(\d{5})-(\d{8})")
_ATTACH_FILENAME_RE = re.compile(r"tse-[qs]cedjpfr-(\d{5})-(\d{4}-\d{2}-\d{2})-(\d{2})-")


def _extract_text_from_file(filepath: str) -> str:
    """PDF or ZIP(内部 qualitative.htm / PDF)からテキスト抽出"""
    raw = Path(filepath).read_bytes()

    if raw[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw), "r")
            for name in zf.namelist():
                if name.lower().endswith("qualitative.htm"):
                    htm_bytes = zf.read(name)
                    for enc in ("utf-8", "shift_jis", "cp932"):
                        try:
                            htm_text = htm_bytes.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        htm_text = htm_bytes.decode("utf-8", errors="replace")
                    text = _strip_html_tags(htm_text)
                    if text and len(text) > 100:
                        zf.close()
                        return text
            for name in zf.namelist():
                if "Summary" in name and name.lower().endswith(".htm"):
                    htm_bytes = zf.read(name)
                    for enc in ("utf-8", "shift_jis", "cp932"):
                        try:
                            htm_text = htm_bytes.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        htm_text = htm_bytes.decode("utf-8", errors="replace")
                    text = _strip_html_tags(htm_text)
                    if text and len(text) > 50:
                        zf.close()
                        return text
            for name in zf.namelist():
                if name.lower().endswith(".pdf"):
                    pdf_bytes = zf.read(name)
                    text = _extract_text_from_pdf_bytes(pdf_bytes)
                    if text:
                        zf.close()
                        return text
            zf.close()
        except Exception as e:
            logger.debug(f"ZIP read failed: {e}")
        return ""

    return _extract_text_from_pdf_bytes(raw)


# ============================================================
# テキストからメタデータ抽出
# ============================================================

_TICKER_RE = re.compile(
    r"(?:コード番号|証券コード|コ\s*ー\s*ド)\s*[：:\s]*(\d{4,5})"
)
_PERIOD_RE = re.compile(r"(20\d{2})年(\d{1,2})月期")
_QUARTER_PATTERNS = [
    (re.compile(r"第([１２３1-3])四半期"), None),
    (re.compile(r"通期"), "FY"),
]


def _extract_metadata_from_text(text: str) -> dict:
    """テキストから ticker, period, quarter を抽出"""
    result: dict = {"ticker": "", "period": "", "quarter": "", "title_snippet": ""}

    m = _TICKER_RE.search(text[:800])
    if m:
        result["ticker"] = normalize_ticker(m.group(1))

    m = _PERIOD_RE.search(text[:800])
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        result["period"] = f"{year}-{month:02d}-{last_day:02d}"

    for pat, fixed_q in _QUARTER_PATTERNS:
        qm = pat.search(text[:800])
        if qm:
            if fixed_q:
                result["quarter"] = fixed_q
            else:
                q_map = {"１": "1Q", "２": "2Q", "３": "3Q", "1": "1Q", "2": "2Q", "3": "3Q"}
                result["quarter"] = q_map.get(qm.group(1), "")
            break

    if not result["quarter"] and "通期" in text[:800]:
        result["quarter"] = "4Q"

    for line in text.split("\n")[:10]:
        stripped = line.strip()
        if "決算短信" in stripped and len(stripped) < 80:
            result["title_snippet"] = stripped
            break

    return result


def _extract_metadata_from_zip_filenames(filepath: str) -> dict:
    """ZIPファイル名からticker/period/quarterを抽出(フォールバック)"""
    result: dict = {"ticker": "", "period": "", "quarter": ""}
    if not filepath.endswith(".zip"):
        return result
    try:
        raw = Path(filepath).read_bytes()
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
        for name in zf.namelist():
            m = _ATTACH_FILENAME_RE.search(name)
            if m:
                result["ticker"] = normalize_ticker(m.group(1))
                result["period"] = m.group(2)
                q_num = m.group(3)
                q_map = {"01": "1Q", "02": "2Q", "03": "3Q"}
                if q_num in q_map:
                    result["quarter"] = q_map[q_num]
                elif "scedjp" in name:
                    result["quarter"] = "4Q"
                break
            m2 = _IXBRL_FILENAME_RE.search(name)
            if m2 and not result["ticker"]:
                result["ticker"] = normalize_ticker(m2.group(1))
                if "scedjp" in name:
                    result["quarter"] = "4Q"
        zf.close()
    except Exception as e:
        logger.debug(f"ZIP filename parse failed: {e}")
    return result


# ============================================================
# マッチング
# ============================================================

def _find_matching_row(
    ticker: str,
    period: str,
    quarter: str,
    db_conn: sqlite3.Connection,
) -> list[dict]:
    """
    quarterly_results で source_doc_id IS NULL かつ
    ticker/quarter が一致する行を探す。
    period は完全一致ではなく ±400日 の範囲で候補取得し、
    score 計算で年度内整合を判定する。
    """
    ticker_variants = [ticker]
    if len(ticker) == 4:
        ticker_variants.append(ticker + "0")
    elif len(ticker) == 5 and ticker.endswith("0"):
        ticker_variants.append(ticker[:-1])

    quarter_variants = [quarter]
    if quarter == "4Q":
        quarter_variants.append("FY")
    elif quarter == "FY":
        quarter_variants.append("4Q")

    placeholders_t = ",".join(["?"] * len(ticker_variants))
    placeholders_q = ",".join(["?"] * len(quarter_variants))

    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(period, "%Y-%m-%d")
        min_period = (dt - timedelta(days=400)).strftime("%Y-%m-%d")
        max_period = (dt + timedelta(days=400)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        min_period = "2020-01-01"
        max_period = "2030-12-31"

    query = f"""
        SELECT id, company_code, fiscal_year_end, quarter,
               source_doc_id, source_url, zip_hash
        FROM quarterly_results
        WHERE company_code IN ({placeholders_t})
          AND quarter IN ({placeholders_q})
          AND fiscal_year_end BETWEEN ? AND ?
          AND source_doc_id IS NULL
        ORDER BY fiscal_year_end DESC
        LIMIT 5
    """
    params = [*ticker_variants, *quarter_variants, min_period, max_period]
    rows = db_conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# スコア配点マップ（breakdownでも使用）
_SCORE_MAP = {
    "ticker_exact": 3, "ticker_partial": 2,
    "period_exact": 3, "period_within_fy": 2, "period_near": 1,
    "quarter_exact": 2, "quarter_fy_alias": 1,
    "tanshin_title": 1,
}


def _compute_match_score(
    candidate: DocCandidate,
    row: dict,
) -> tuple[str, str]:
    """マッチ信頼度を計算。Returns: (confidence, note)"""
    score = 0
    notes: list[str] = []

    row_ticker = normalize_ticker(row["company_code"])
    if candidate.ticker == row_ticker:
        score += 3; notes.append("ticker_exact")
    elif candidate.ticker and row_ticker:
        if candidate.ticker in row_ticker or row_ticker in candidate.ticker:
            score += 2; notes.append("ticker_partial")

    fye = row["fiscal_year_end"]
    cand_period = candidate.period
    if cand_period == fye:
        score += 3; notes.append("period_exact")
    elif cand_period and fye:
        try:
            from datetime import datetime, timedelta
            fye_dt = datetime.strptime(fye, "%Y-%m-%d")
            cand_dt = datetime.strptime(cand_period, "%Y-%m-%d")
            fy_start = fye_dt - timedelta(days=365)
            if fy_start < cand_dt <= fye_dt:
                score += 2; notes.append("period_within_fy")
            elif abs((fye_dt - cand_dt).days) < 400:
                score += 1; notes.append("period_near")
        except (ValueError, TypeError):
            pass

    cq, rq = candidate.quarter, row["quarter"]
    if cq == rq:
        score += 2; notes.append("quarter_exact")
    elif cq in ("4Q", "FY") and rq in ("4Q", "FY"):
        score += 1; notes.append("quarter_fy_alias")

    if candidate.title_snippet and "決算短信" in candidate.title_snippet:
        score += 1; notes.append("tanshin_title")

    if score >= 7:
        confidence = "high"
    elif score >= 5:
        confidence = "medium"
    else:
        confidence = "low"

    return confidence, " ".join(notes)


def _build_breakdown(note: str) -> tuple[int, dict]:
    """note文字列から score_breakdown を再構築"""
    breakdown: dict[str, int] = {}
    total = 0
    for part in note.split():
        pts = _SCORE_MAP.get(part, 0)
        total += pts
        breakdown[part] = pts
    return total, breakdown


# ============================================================
# メイン処理
# ============================================================

def _update_source_doc_id(
    db_conn: sqlite3.Connection,
    row_id: int,
    sha256: str,
    source_url: str,
    zip_hash: str,
):
    db_conn.execute(
        """UPDATE quarterly_results
           SET source_doc_id = ?, source_url = ?, zip_hash = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (sha256, source_url, zip_hash, row_id),
    )


def main():
    parser = argparse.ArgumentParser(
        description="quarterly_results.source_doc_id backfill",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="File scan limit (0=all)")
    parser.add_argument("--db", default="decision_db.db")
    parser.add_argument("--docs-dir", default="")
    parser.add_argument("--csv", default="", help="Review CSV output path")
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

    docs_dir = args.docs_dir or os.path.join(_PROJECT_ROOT, "data", "docs")

    if not os.path.isdir(docs_dir):
        logger.error(f"docs dir not found: {docs_dir}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    files = sorted(os.listdir(docs_dir))
    if args.limit > 0:
        files = files[:args.limit]

    # 既にリンク済みの SHA-256 を取得
    existing_hashes = set()
    for r in conn.execute(
        "SELECT DISTINCT source_doc_id FROM quarterly_results WHERE source_doc_id IS NOT NULL"
    ).fetchall():
        existing_hashes.add(r[0])
    for r in conn.execute(
        "SELECT DISTINCT zip_hash FROM quarterly_results WHERE zip_hash IS NOT NULL"
    ).fetchall():
        existing_hashes.add(r[0])

    # 集計値: scanned = already_linked + matched_high + matched_medium + unmatched
    stats = {
        "scanned": 0,
        "already_linked": 0,
        "matched_high": 0,
        "matched_medium": 0,
        "unmatched": 0,
        "updated_rows": 0,
    }

    csv_rows: list[dict] = []

    for fname in files:
        if not (fname.endswith(".pdf") or fname.endswith(".zip")):
            continue

        filepath = os.path.join(docs_dir, fname)
        stats["scanned"] += 1

        file_hash = _sha256_file(filepath)
        if file_hash in existing_hashes:
            stats["already_linked"] += 1
            logger.debug(f"[SKIP] already linked: {fname}")
            continue

        text = _extract_text_from_file(filepath)
        if not text:
            logger.debug(f"[SKIP] no text: {fname}")
            stats["unmatched"] += 1
            continue

        meta = _extract_metadata_from_text(text)
        if fname.endswith(".zip"):
            zip_meta = _extract_metadata_from_zip_filenames(filepath)
            for key in ("ticker", "period", "quarter"):
                if not meta.get(key) and zip_meta.get(key):
                    meta[key] = zip_meta[key]
                    logger.debug(f"  {key} filled from filename: {zip_meta[key]}")
        if not meta["ticker"] or not meta["period"]:
            logger.debug(
                f"[SKIP] insufficient metadata: {fname} "
                f"ticker={meta['ticker']} period={meta['period']}"
            )
            stats["unmatched"] += 1
            continue

        candidate = DocCandidate(
            filename=fname, filepath=filepath, sha256=file_hash,
            ticker=meta["ticker"], period=meta["period"],
            quarter=meta["quarter"], title_snippet=meta["title_snippet"],
        )

        matching_rows = _find_matching_row(
            candidate.ticker, candidate.period, candidate.quarter, conn
        )

        if not matching_rows:
            stats["unmatched"] += 1
            logger.info(
                f"[NO_MATCH] {fname} "
                f"ticker={candidate.ticker} "
                f"period={candidate.period} "
                f"quarter={candidate.quarter}"
            )
            continue

        # スコア計算 → ファイル単位で最良 confidence で分類
        best_confidence = "low"
        rank_map = {"high": 3, "medium": 2, "low": 1}

        for row in matching_rows:
            confidence, note = _compute_match_score(candidate, row)
            score_val, breakdown = _build_breakdown(note)

            if confidence in ("high", "medium"):
                csv_rows.append({
                    "source_path": fname,
                    "inferred_ticker": candidate.ticker,
                    "inferred_period": candidate.period,
                    "inferred_quarter": candidate.quarter,
                    "candidate_row_id": row["id"],
                    "db_ticker": normalize_ticker(row["company_code"]),
                    "db_fye": row["fiscal_year_end"],
                    "db_quarter": row["quarter"],
                    "confidence": confidence,
                    "score": score_val,
                    "score_breakdown_json": json_mod.dumps(breakdown, ensure_ascii=False),
                    "title_snippet": candidate.title_snippet,
                })

            if rank_map.get(confidence, 0) > rank_map.get(best_confidence, 0):
                best_confidence = confidence

            if confidence == "high":
                if not args.dry_run:
                    _update_source_doc_id(
                        conn, row["id"], file_hash,
                        source_url="",
                        zip_hash=file_hash if fname.endswith(".zip") else "",
                    )
                    stats["updated_rows"] += 1
                    conn.commit()
                logger.info(
                    f"[HIGH] {fname} -> "
                    f"row_id={row['id']} "
                    f"ticker={normalize_ticker(row['company_code'])} "
                    f"{row['fiscal_year_end']} {row['quarter']} "
                    f"score={score_val} "
                    f"breakdown={json_mod.dumps(breakdown)}"
                )
            elif confidence == "medium":
                logger.info(
                    f"[MEDIUM] {fname} -> "
                    f"ticker={normalize_ticker(row['company_code'])} "
                    f"{row['fiscal_year_end']} {row['quarter']} "
                    f"score={score_val} "
                    f"breakdown={json_mod.dumps(breakdown)} -- NOT UPDATED"
                )

        # ファイル単位で1カウント
        if best_confidence == "high":
            stats["matched_high"] += 1
        elif best_confidence == "medium":
            stats["matched_medium"] += 1
        else:
            stats["unmatched"] += 1

    conn.close()

    # Review CSV 出力
    csv_path = args.csv or os.path.join(_PROJECT_ROOT, "data", "backfill_review.csv")
    if csv_rows:
        fieldnames = [
            "source_path", "inferred_ticker", "inferred_period",
            "inferred_quarter", "candidate_row_id", "db_ticker",
            "db_fye", "db_quarter", "confidence", "score",
            "score_breakdown_json", "title_snippet",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        logger.info(f"[CSV] review saved: {csv_path} ({len(csv_rows)} rows)")

    # サマリ
    print()
    print("=" * 55)
    print("  source_doc_id backfill - results")
    print("=" * 55)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    check = stats["already_linked"] + stats["matched_high"] + stats["matched_medium"] + stats["unmatched"]
    ok = "OK" if check == stats["scanned"] else "MISMATCH!"
    print(f"  {'check_total':20s}: {check} ({ok})")
    print("=" * 55)


if __name__ == "__main__":
    main()
