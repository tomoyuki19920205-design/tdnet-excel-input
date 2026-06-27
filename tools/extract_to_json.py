#!/usr/bin/env python3
# ============================================================
# extract_to_json.py — ローカルZIP/PDFから決算データをJSON出力
# ============================================================
#
# 使い方:
#   python -m tools.extract_to_json --input data/docs --output results/
#   python -m tools.extract_to_json --input data/docs/081220260213561316.zip --output results/
#
# data/docs 内の ZIP ファイルから XBRL/iXBRL を抽出し、
# load_results_to_db.py が読める JSON を results/ に出力する。
#
# ============================================================
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.extractor import extract_financials, _extract_from_xbrl
from src.utils import parse_scale_unit
from src.year_parser import parse_reiwa, extract_fiscal_info

import calendar

logger = logging.getLogger("extract")

# ============================================================
# ユーティリティ
# ============================================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _ticker_from_filename(filename: str) -> str | None:
    """
    ファイル名から企業コードを抽出する（fallback用）。
    例: "081220260213561316.zip" → "0812"
    """
    basename = os.path.basename(filename)
    m = re.match(r"^(\d{4})", basename)
    return m.group(1) if m else None


def _extract_ticker_from_xbrl(file_path: str) -> tuple[str | None, str]:
    """
    ZIP内のXBRL/iXBRLコンテンツから証券コード(ticker_code)を抽出する。

    3段階の優先順位:
      (a) SecuritiesCode タグ（iXBRL Summary内） — 最優先
      (b) XBRLファイル名の5桁TDnetコード先頭4桁 — 次善
      (c) ZIPファイル名先頭4桁 — fallback（WARN出力）

    Returns:
        (ticker_code, source) where source is "SecuritiesCode"|"XBRL_FILENAME"|"ZIP_FILENAME"
    """
    import zipfile as _zf

    ticker = None
    source = ""

    try:
        with _zf.ZipFile(file_path, "r") as zf:
            for name in zf.namelist():
                if "Summary" not in name or not name.endswith("-ixbrl.htm"):
                    continue

                # --- (a) SecuritiesCode タグ ---
                content = zf.read(name).decode("utf-8", errors="replace")
                m = re.search(r"SecuritiesCode[^>]*>(\d{4})\D", content)
                if m:
                    ticker = m.group(1)
                    source = "SecuritiesCode"
                    return ticker, source

                # --- (b) XBRLファイル名の5桁TDnetコード ---
                m2 = re.search(r"-(\d{5})-\d{8}", name)
                if m2:
                    ticker = m2.group(1)[:4]
                    source = "XBRL_FILENAME"
                    return ticker, source

            # Summaryが見つからない場合、他のixbrlファイルから試行
            for name in zf.namelist():
                if not name.endswith("-ixbrl.htm"):
                    continue
                m2 = re.search(r"-(\d{5})-\d{4}-\d{2}-\d{2}", name)
                if m2:
                    ticker = m2.group(1)[:4]
                    source = "XBRL_FILENAME"
                    return ticker, source

    except Exception as e:
        logger.debug(f"XBRL ticker extraction error: {e}")

    # --- (c) fallback: ZIPファイル名 ---
    ticker = _ticker_from_filename(file_path)
    if ticker:
        source = "ZIP_FILENAME"
        logger.warning(
            f"[EXTRACT] ticker fallback: ZIPファイル名から取得 {ticker} "
            f"({os.path.basename(file_path)})"
        )
    return ticker, source



_PL_PERIODS_CACHE = {}

def get_true_fiscal_year_end(ticker: str, extracted_date: str) -> str | None:
    if not extracted_date or not ticker: return extracted_date
    if ticker not in _PL_PERIODS_CACHE:
        from lib.pipeline.db import get_supabase_read_config, supabase_select, load_env
        load_env()
        cr = get_supabase_read_config()
        res = supabase_select("canonical_financials", params={"ticker": f"eq.{ticker}", "select": "period"}, config=cr)
        _PL_PERIODS_CACHE[ticker] = sorted(list(set(r["period"] for r in (res or []))))
        
    periods = _PL_PERIODS_CACHE[ticker]
    valid = [p for p in periods if p >= extracted_date]
    if valid:
        return min(valid)
    return None

def _reiwa_to_fiscal_year_end(r_str: str) -> str | None:
    """R表記 → fiscal_year_end (YYYY-MM-DD)"""
    parsed = parse_reiwa(r_str)
    if parsed is None:
        return None
    ad_year, month = parsed
    last_day = calendar.monthrange(ad_year, month)[1]
    return f"{ad_year:04d}-{month:02d}-{last_day:02d}"


def _quarter_str_to_int(q: str) -> int | None:
    """'1Q' → 1, '2Q' → 2, ..."""
    m = re.match(r"(\d)Q", q)
    return int(m.group(1)) if m else None


def _detect_doc_type_from_title(title: str) -> str:
    """タイトルから doc_type を推定"""
    if "決算短信" in title:
        return "TANSHIN"
    if "修正" in title or "差異" in title:
        return "REVISION"
    if "説明" in title or "プレゼン" in title:
        return "PRESENTATION"
    if "質疑" in title or "Q&A" in title:
        return "QA"
    return "OTHER"


def _quality_from_source(source_unit: str, confidence: str) -> str:
    """抽出品質からquality値を決定"""
    if confidence == "high":
        return "IXBRL"
    if confidence == "medium":
        return "PDF"
    return "MANUAL"


def _parse_fiscal_info_from_zip(file_path: str) -> tuple[str | None, int | None]:
    """
    ZIP内のXBRLファイル名からfiscal_year_endとquarterを推定する。

    例: tse-qcedjpfr-54610-2025-12-31-02-2026-02-25-ixbrl.htm
        → fiscal_year_end = "2025-12-31", quarter = 2

    ファイル名パターン: ...-{ticker}-{YYYY-MM-DD}-{QQ}-{filing_date}-ixbrl.htm
    """
    import zipfile as _zf

    try:
        with _zf.ZipFile(file_path, "r") as zf:
            for name in zf.namelist():
                # Attachment内のixbrlファイルにfiscal info が含まれる
                # パターン: ...-YYYY-MM-DD-QQ-YYYY-MM-DD-ixbrl.htm
                m = re.search(
                    r"-(\d{4}-\d{2}-\d{2})-(\d{2})-\d{4}-\d{2}-\d{2}-ixbrl\.htm$",
                    name,
                )
                if m:
                    fiscal_year_end = m.group(1)
                    quarter = int(m.group(2))
                    if 1 <= quarter <= 4:
                        return fiscal_year_end, quarter

            # フォールバック: XSD/プレファイルからも探す
            for name in zf.namelist():
                m = re.search(
                    r"-(\d{4}-\d{2}-\d{2})-(\d{2})-\d{4}-\d{2}-\d{2}[.-]",
                    name,
                )
                if m:
                    fiscal_year_end = m.group(1)
                    quarter = int(m.group(2))
                    if 1 <= quarter <= 4:
                        return fiscal_year_end, quarter
    except Exception as e:
        logger.debug(f"ZIP fiscal info parse error: {e}")

    return None, None


def _convert_value_to_source_unit(
    value: int | None, source_unit: str
) -> int | float | None:
    """
    抽出値はextractorが内部的にscale適用済みの場合がある。
    source_unit="円" なら素の値（既に円）。
    source_unit="百万円" でscale=6適用済みなら既に円相当。
    ここではJSONの source_unit に応じた値を出力する。

    extractorの挙動:
    - XBRL従来モード: normalize_number → そのまま（XBRLの表記値=円）
    - iXBRLモード: _apply_ixbrl_scale → scale適用済み = 円
    - PDFモード: normalize_number → 素の数値(scale適用前)

    統一方針: extractorの出力は全て「円」。JSONにはsource_unit="円"として出力。
    """
    return value


# ============================================================
# 単一ファイルの処理
# ============================================================

def extract_single(file_path: str, title_hint: str = "") -> dict | None:
    """
    単一のZIP/PDFから決算データを抽出してJSON dictを生成する。

    Returns:
        load_results_to_db.py が読めるJSON dict, or None
    """
    ext = os.path.splitext(file_path)[1].lower()

    # ticker取得: XBRL内容ベース（ZIPの場合）
    if ext == ".zip":
        ticker, ticker_source = _extract_ticker_from_xbrl(file_path)
    else:
        # PDF等はファイル名からのみ
        ticker = _ticker_from_filename(file_path)
        ticker_source = "ZIP_FILENAME" if ticker else ""

    if ticker is None:
        logger.warning(f"[EXTRACT] ticker特定不可: {file_path}")
        return None



    # タイトルhint（なければ汎用的なタイトルを使用）
    title = title_hint or f"決算短信 ({os.path.basename(file_path)})"

    # 抽出
    if ext == ".zip":
        # ZIPはXBRL/iXBRL抽出を試行
        result = _extract_from_xbrl(file_path)
        if result is None:
            logger.info(f"[EXTRACT] XBRL抽出失敗、PDFフォールバック不可(ZIP): {file_path}")
            return None

        # fiscal_year_end + quarter: ZIPエントリ名 → iXBRL検出 → title推定 の順で決定
        zip_fye, zip_q = _parse_fiscal_info_from_zip(file_path)

        if zip_fye:
            fiscal_year_end = zip_fye
        else:
            # extractorのR表記からの変換を試行
            fiscal_year_end = None
            if result.fiscal_year:
                fiscal_year_end = _reiwa_to_fiscal_year_end(result.fiscal_year)
            if not fiscal_year_end:
                # titleからの推定
                fy, _ = extract_fiscal_info(title, "")
                if fy:
                    fiscal_year_end = _reiwa_to_fiscal_year_end(fy)

        # quarter: ZIPエントリ名 → iXBRL contextRef → title推定
        quarter = zip_q
        if quarter is None:
            quarter = _quarter_str_to_int(result.quarter) if result.quarter else None
        if quarter is None:
            _, q_str = extract_fiscal_info(title, "")
            quarter = _quarter_str_to_int(q_str) if q_str else None

    elif ext == ".pdf":
        # PDF抽出
        financials, error = extract_financials(
            doc_path=file_path, title=title, xbrl_path=None,
        )
        if financials is None:
            logger.info(f"[EXTRACT] PDF抽出失敗: {file_path} ({error})")
            return None
        result = financials
        fiscal_year_end = None
        if result.fiscal_year:
            fiscal_year_end = _reiwa_to_fiscal_year_end(result.fiscal_year)
        quarter = _quarter_str_to_int(result.quarter) if result.quarter else None
    else:
        logger.debug(f"[EXTRACT] 非対応拡張子: {file_path}")
        return None

    if not fiscal_year_end:
        logger.warning(f"[EXTRACT] fiscal_year_end 推定不可: {file_path}")
        return None

    if quarter is None:
        logger.warning(f"[EXTRACT] quarter 推定不可: {file_path}")
        return None

    sha256 = _sha256_file(file_path)

    # --- extractorの出力値は既に円 ---
    # (XBRLモード: scale適用済み=円, iXBRLモード: scale適用済み=円)
    # よって source_unit = "円"
    source_unit = "円"

    values = {}
    if result.sales is not None:
        values["sales"] = result.sales
    if result.gross_profit is not None:
        values["gross_profit"] = result.gross_profit
    if result.operating_profit is not None:
        values["op_income"] = result.operating_profit

    if not values:
        logger.warning(f"[EXTRACT] 有効な数値なし: {file_path}")
        return None

    doc_type = _detect_doc_type_from_title(title)
    quality = _quality_from_source(result.source_unit, result.confidence)

    return {
        "ticker_code": ticker,
        "company_name": None,  # ZIPからは不明
        "title": title,
        "disclosed_at": datetime.fromtimestamp(
            os.path.getmtime(file_path)
        ).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "url": None,
        "sha256": sha256,
        "doc_type": doc_type,
        "source": "TDNET",
        "fiscal_year_end": fiscal_year_end,
        "quarter": quarter,
        "source_unit": source_unit,
        "values": values,
        "scope": "CONSOLIDATED",
        "metric_type": "actual",
        "quality": quality,
        "_source_file": os.path.basename(file_path),
        "_extractor_unit": result.source_unit,
        "_ticker_source": ticker_source,
    }


# ============================================================
# バッチ抽出
# ============================================================

def extract_batch(input_path: str, output_dir: str) -> dict:
    """
    入力ファイル/ディレクトリからJSONを生成して output_dir に保存する。

    Returns:
        {"total": int, "extracted": int, "skipped": int, "files": list[str]}
    """
    os.makedirs(output_dir, exist_ok=True)

    p = Path(input_path)
    if p.is_file():
        files = [str(p)]
    elif p.is_dir():
        files = sorted(
            str(f) for f in p.iterdir()
            if f.suffix.lower() in (".zip",)  # ZIPのみ（PDFは決算短信タイトル判定必要）
        )
    else:
        return {"total": 0, "extracted": 0, "skipped": 0, "files": []}

    summary = {"total": len(files), "extracted": 0, "skipped": 0, "files": []}

    for file_path in files:
        result = extract_single(file_path)
        if result is None:
            summary["skipped"] += 1
            continue

        # JSON出力ファイル名: {ticker}_{basename}.json
        basename = Path(file_path).stem
        json_name = f"{basename}.json"
        json_path = os.path.join(output_dir, json_name)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        summary["extracted"] += 1
        summary["files"].append(json_name)
        logger.info(
            f"[EXTRACT] {os.path.basename(file_path)} → {json_name} "
            f"(ticker={result['ticker_code']} "
            f"fy={result['fiscal_year_end']} Q{result['quarter']} "
            f"sales={result['values'].get('sales')})"
        )

    logger.info(
        f"[EXTRACT] 完了: total={summary['total']} "
        f"extracted={summary['extracted']} skipped={summary['skipped']}"
    )

    return summary


# ============================================================
# CLI
# ============================================================

def main():
    # Windows cp932 対策
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    parser = argparse.ArgumentParser(
        description="ローカルZIP/PDFから決算データをJSON出力"
    )
    parser.add_argument(
        "--input", type=str, required=True,
        help="入力ファイル or ディレクトリ (data/docs等)",
    )
    parser.add_argument(
        "--output", type=str, default="results",
        help="出力ディレクトリ (デフォルト: results/)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="詳細ログ出力",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("  XBRL/iXBRL 抽出 → JSON出力")
    print("=" * 60)
    print(f"  入力  : {args.input}")
    print(f"  出力  : {args.output}")
    print()

    summary = extract_batch(args.input, args.output)

    print("=" * 60)
    print("  結果サマリ")
    print("=" * 60)
    print(f"  対象ファイル : {summary['total']}")
    print(f"  抽出成功     : {summary['extracted']}")
    print(f"  スキップ     : {summary['skipped']}")
    print()

    if summary["files"]:
        print("[生成ファイル]")
        for f in summary["files"]:
            print(f"  {f}")
        print()

    sys.exit(0 if summary["extracted"] > 0 else 1)


if __name__ == "__main__":
    main()
