"""lib/backfill/worker_v4.py — V4 セグメント抽出 worker

XBRL first: XBRL が取れればそれを採用。
XBRL facts なし or XBRL なし → run_segment_detection_v4 で PDF 抽出。
V1 fallback は呼ばない。

エントリポイント:
  - process_one_filing_v4()   XBRL-first + V4 PDF fallback
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from lib.backfill.worker import (
    _ensure_imports,
    _download_originals,
    _extract_financials_data,
    _normalize_segment_name_conservative,
    _classify_row_type,
    _update_state,
    compute_result_fingerprint,
)
from lib.backfill.worker_v2 import (
    FilingResultV2,
    SourceCandidate,
    _try_xbrl_source,
    _build_debug_log,
    validator_status_to_worker,
)

from lib.backfill.segment_ai_fallback import (
    extract_segments_with_ai,
    _is_ai_fallback_applicable,
    resolve_ai_period_context,
    _extract_text_from_pdf,
)

logger = logging.getLogger("backfill.worker_v4")

# ============================================================
# 正常スキップ判定
# ============================================================

# 単一セグメント・省略系キーワード
_SINGLE_SEGMENT_KEYWORDS = [
    "単一セグメント",
    "単一のセグメント",
    "当社グループは単一セグメント",
    "当社は単一セグメント",
]
_OMISSION_KEYWORDS = [
    "セグメント情報の記載を省略",
    "セグメント情報の開示は省略",
    "セグメント別の記載を省略",
    "重要性が乏しいため記載を省略",
]
# ETF 等の非対象判定（filing.title / PDF テキスト対象）
_ETF_KEYWORDS = ["ETF", "上場投資信託", "Exchange Traded Fund"]

# PRE-AI normal skip 用固定ティッカー（title/PDF キーワードでは捕捉できない ETF/上場投信系）
_PRE_AI_ETF_FIXED_TICKERS = {"2081", "2082", "2524", "2525", "2526", "2567", "349A", "459A"}


def _detect_normal_skip(
    pdf_path: str | None,
    ticker: str = "",
    title: str = "",
) -> str:
    """PDF または title から正常スキップ理由を判定して返す。

    Returns:
        "single_segment_omitted" | "segment_disclosure_omitted"
        | "non_operating_target_etf" | ""  (空文字 = スキップ非該当)
    """
    # 固定ティッカー判定（title/PDF より先に確認）
    if ticker and ticker in _PRE_AI_ETF_FIXED_TICKERS:
        return "non_operating_target_etf"

    # ETF 判定 (title が使えれば PDF 読まず判定)
    _title_upper = (title or "").upper()
    if any(kw.upper() in _title_upper for kw in _ETF_KEYWORDS):
        return "non_operating_target_etf"

    if not pdf_path:
        return ""

    # PDF テキスト抽出（先頭 30 ページまで、失敗時は空文字で続行）
    try:
        import fitz  # type: ignore
        text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc[:30]:
                text += page.get_text()
                if len(text) > 50_000:
                    break
    except Exception:
        return ""

    # ETF 再判定（PDF 本文）
    if any(kw in text for kw in _ETF_KEYWORDS):
        return "non_operating_target_etf"

    # 単一セグメント判定（優先）
    if any(kw in text for kw in _SINGLE_SEGMENT_KEYWORDS):
        return "single_segment_omitted"

    # 省略一般判定
    if any(kw in text for kw in _OMISSION_KEYWORDS):
        return "segment_disclosure_omitted"

    return ""


# ============================================================
# ETF/ETN タイトル判定（XBRL失敗後に PDF/AI へ流さない用）
# ============================================================

_ETF_LIKE_TITLE_PATTERNS = [
    "ETF",
    "ETN",
    "上場投信",
    "上場投資信託",
    "指数連動",
    "連動型上場投資信託",
    "iFreeETF",
    "iシェアーズ",
    "グローバルＸ",
    "グローバルX",
    "NEXT FUNDS",
    "MAXIS",
    "上場インデックスファンド",
    "インデックスファンド",
    "ブル2倍",
    "ベア2倍",
    "インバース",
    "レバレッジ",
]


def _is_etf_like_title(title: str) -> bool:
    """開示タイトルが ETF/ETN 系かどうかを判定する。"""
    s = (title or "").replace("\u3000", " ").strip()
    if not s:
        return False
    s_upper = s.upper()
    for p in _ETF_LIKE_TITLE_PATTERNS:
        if p == "ETF" or p == "ETN":
            if p in s_upper:
                return True
        elif p in s:
            return True
    return False


_REIT_LIKE_TITLE_PATTERNS = [
    "投資法人",
    "リート",
    "REIT",
    "投資証券",
    "インフラファンド",
]


def _is_reit_like_title(title: str) -> bool:
    """開示タイトルが REIT / 投資法人 / インフラファンド系かどうかを判定する。"""
    s = (title or "").replace("\u3000", " ").strip()
    if not s:
        return False
    s_upper = s.upper()
    for p in _REIT_LIKE_TITLE_PATTERNS:
        if p == "REIT":
            if p in s_upper:
                return True
        elif p in s:
            return True
    return False



# ============================================================
# AI fallback 前: セグメントページ存在チェック
# ============================================================

_SEGMENT_PAGE_SIGNALS: list[str] = [
    # ページ見出し系
    "セグメント情報",
    "報告セグメント",
    "事業セグメント",
    "セグメントの概況",
    "セグメント業績",
    "セグメント別",
    # 表内行ラベル系（セグメント表があれば必ず登場）
    "セグメント損益",
    "セグメント利益",
    "セグメント資産",
    "報告セグメントごとの",
    "外部顧客への売上高",
    "セグメント間の内部売上高",
    "セグメント間の内部売上高又は振替高",
    "セグメント利益又は損失",
]

# signal hit ページでもこれらがあれば単一セグメント / 省略扱い → False
_SEGMENT_PAGE_EXCLUDE_TERMS: list[str] = [
    "単一セグメント",
    "セグメント情報の記載を省略",
    "セグメント別売上高のみ",
    "報告セグメントはありません",
]


def _has_segment_page_signal(pdf_path: str, ticker: str = "") -> bool:
    """セグメント情報ページが PDF 内に存在するか確認する。

    AI fallback 直前に呼び出し、「読む対象自体がない」案件を事前打ち切る。
    安全側設計: pdf_path が空 / 例外発生時は True を返す（AI に任せる）。
    """
    if not pdf_path:
        return True  # パスなし → AI に任せる
    try:
        import fitz  # type: ignore
        with fitz.open(pdf_path) as doc:
            for page in doc[:50]:
                text = page.get_text()
                if any(kw in text for kw in _SEGMENT_PAGE_SIGNALS):
                    import re as _re
                    _exclude_hit = any(ex in text for ex in _SEGMENT_PAGE_EXCLUDE_TERMS)
                    _num_count = len(_re.findall(r"\d[\d,]*", text))
                    _preview = text[:400].replace("\n", " ").replace("\r", " ")
                    logger.debug(
                        "[segment_page_signal] ticker=%s page=%d signal_hit=True "
                        "exclude_hit=%s numeric_count=%d preview=%r",
                        ticker, page.number + 1, _exclude_hit, _num_count, _preview,
                    )
                    if not _exclude_hit and _num_count >= 3:
                        return True
    except Exception:
        return True  # 開けない / 例外 → AI に任せる
    return False


# ============================================================
# V4 PDF 抽出ヘルパー
# ============================================================

# 単位検出: 優先順位付き（高精度 → 低精度の順）
# 百万円を千円より先に検索して誤一致を防ぐ
_UNIT_DETECT_PATTERNS: list[tuple[str, str, int]] = [
    # 億円
    ("単位：億円", "億円", 100_000_000),
    ("単位:億円", "億円", 100_000_000),
    ("（億円）", "億円", 100_000_000),
    ("(億円)", "億円", 100_000_000),
    ("HundredMillion", "億円", 100_000_000),
    # 百万円
    ("単位：百万円", "百万円", 1_000_000),
    ("単位:百万円", "百万円", 1_000_000),
    ("（百万円）", "百万円", 1_000_000),
    ("(百万円)", "百万円", 1_000_000),
    ("Million Yen", "百万円", 1_000_000),
    ("million yen", "百万円", 1_000_000),
    # 千円（最後に評価）
    ("単位：千円", "千円", 1_000),
    ("単位:千円", "千円", 1_000),
    ("（千円）", "千円", 1_000),
    ("(千円)", "千円", 1_000),
    ("Thousand Yen", "千円", 1_000),
    ("thousand yen", "千円", 1_000),
]


def _detect_pdf_unit(doc_path: str) -> tuple[str | None, int | None]:
    """PDFページテキストから単位（千円/百万円/億円）を検出する。

    優先順位付きパターン一致を使用し、最初の30ページを走査する。
    検出できない場合は (None, None) を返す。
    """
    if not doc_path:
        return None, None
    try:
        import fitz  # type: ignore
        with fitz.open(doc_path) as doc:
            for page in doc[:30]:
                text = page.get_text()
                for marker, unit_raw, unit_mult in _UNIT_DETECT_PATTERNS:
                    if marker in text:
                        return unit_raw, unit_mult
    except Exception:
        pass
    return None, None


def _try_pdf_source_v4(
    doc_path: str,
    filing,
    financials_data: dict | None,
    fid: str,
    metrics: dict,
) -> SourceCandidate:
    """run_segment_detection_v4 でセグメント抽出し SourceCandidate を返す。"""
    if not doc_path:
        return SourceCandidate(
            source="pdf", attempted=False, available=False,
            skip_reason="not_available",
        )

    from src.segment.extraction_result_validator import validate_extraction_result

    t = time.monotonic()
    try:
        from src.analysis.segment_detection_v4 import run_segment_detection_v4
        v4_result = run_segment_detection_v4(doc_path, ticker=filing.ticker)
    except Exception as e:
        logger.warning(f"[v4] run_segment_detection_v4 error: fid={fid} err={e}")
        metrics["v4_segment_ms"] = int((time.monotonic() - t) * 1000)
        return SourceCandidate(
            source="pdf", attempted=True, available=True,
            error=f"v4_exception:{str(e)[:150]}",
        )
    metrics["v4_segment_ms"] = int((time.monotonic() - t) * 1000)

    if not (
        v4_result.success
        or getattr(v4_result, "segments", None)
        or getattr(v4_result, "extracted_periods", None)
    ):
        reason = v4_result.quarantine_reason or "v4_no_segments"
        # AI フォールバック対象の失敗理由は normal_skip 判定をスキップ
        # （ここで normal_skip にすると AI 呼び出しに到達できなくなるため）
        if not _is_ai_fallback_applicable(reason):
            _skip = _detect_normal_skip(doc_path, filing.ticker, getattr(filing, "title", ""))
            if _skip:
                return SourceCandidate(
                    source="pdf", attempted=True, available=True,
                    error=f"normal_skip:{_skip}",
                    rule_trace=getattr(v4_result, "rule_trace", []),
                )
        logger.debug(f"[v4] PDF extraction failed: fid={fid} reason={reason}")
        return SourceCandidate(
            source="pdf", attempted=True, available=True,
            error=reason,
            rule_trace=getattr(v4_result, "rule_trace", []),
        )

    # period / quarter 解決
    period = (financials_data or {}).get("period", "")
    quarter = (financials_data or {}).get("quarter", "")
    if not period or not quarter:
        from src.year_parser import extract_fiscal_info
        title_fy, title_q = extract_fiscal_info(filing.title)
        if not period and title_fy:
            period = title_fy
        if not quarter and title_q:
            quarter = title_q
    # V4 result から period/quarter を取得できる場合も試みる
    if v4_result.extracted_periods:
        pr = v4_result.extracted_periods[0]
        if not period and getattr(pr, "period", None):
            period = pr.period
        if not quarter and getattr(pr, "quarter", None):
            quarter = pr.quarter

    # --- FY フォールバック (PDF経路のみ): 年次決算短信で quarter が未解決の場合 ---
    # 「○年○月期 決算短信」のように四半期表記が一切ないタイトルを救済する。
    if not quarter:
        _title = getattr(filing, "title", "") or ""
        _ANNUAL_KW = ("決算短信", "有価証券報告書", "Annual Report")
        _QUARTER_KW = (
            "第1四半期", "第２四半期", "第2四半期",
            "第３四半期", "第3四半期",
            "中間", "1st Quarter", "2nd Quarter", "3rd Quarter",
        )
        if any(x in _title for x in _ANNUAL_KW) and not any(x in _title for x in _QUARTER_KW):
            quarter = "FY"

    if not period or not quarter:
        return SourceCandidate(
            source="pdf", attempted=True, available=True,
            error=f"period_quarter_unresolved:period={period!r},quarter={quarter!r}",
            rule_trace=getattr(v4_result, "rule_trace", []),
        )

    # SegmentRecordV4 → dict レコードに変換
    records = []
    _TRACE_TICKERS = {"2901", "2936"}

    # ── ローカルヘルパー: previous ブロックの period を1年前に計算 ──
    def _derive_period_for_block(base_period: str, period_type: str) -> str:
        """period_type が "previous" のとき base_period の年を -1 する。"""
        if period_type != "previous" or not base_period or len(base_period) < 4:
            return base_period
        try:
            prev_year = int(base_period[:4]) - 1
            return f"{prev_year}{base_period[4:]}"
        except ValueError:
            return base_period

    # PDF全体から単位を検出（セグメント側の unit_raw が全て None の場合のfallback）
    # unit 判定のため、全セグを集める（extracted_periods / segments 両方に対応）
    _all_segs_for_unit = []
    if v4_result.extracted_periods:
        for _ep in v4_result.extracted_periods:
            _all_segs_for_unit.extend(getattr(_ep, "segments", []))
    else:
        _all_segs_for_unit = list(v4_result.segments)

    _pdf_unit_raw: str | None = None
    _pdf_unit_mult: int | None = None
    _all_units_missing = all(
        getattr(s, "unit_raw", None) is None
        and getattr(s, "unit_multiplier", None) is None
        for s in _all_segs_for_unit
    )
    if _all_units_missing:
        _pdf_unit_raw, _pdf_unit_mult = _detect_pdf_unit(doc_path)
        if _pdf_unit_raw:
            logger.info(
                "[UNIT_DETECT] ticker=%s unit_raw=%r mult=%s (pdf_text_fallback)",
                filing.ticker, _pdf_unit_raw, _pdf_unit_mult,
            )
        else:
            logger.warning(
                "[UNIT_DETECT] ticker=%s unit not detected from PDF text",
                filing.ticker,
            )

    # ── extracted_periods があればそれを全件 records 化（current + previous） ──
    if v4_result.extracted_periods:
        _n_curr = sum(1 for ep in v4_result.extracted_periods if getattr(ep, "period_type", "") == "current")
        _n_prev = sum(1 for ep in v4_result.extracted_periods if getattr(ep, "period_type", "") == "previous")
        _n_unk  = sum(1 for ep in v4_result.extracted_periods if getattr(ep, "period_type", "") == "unknown")
        logger.info(
            "[V4] extracted_periods used: ticker=%s current=%d previous=%d unknown=%d",
            filing.ticker, _n_curr, _n_prev, _n_unk,
        )
        for ep in v4_result.extracted_periods:
            ep_period_type = getattr(ep, "period_type", "unknown")
            ep_period = _derive_period_for_block(period, ep_period_type)
            for seg in getattr(ep, "segments", []):
                seg_name = getattr(seg, "segment_name", "") or ""
                _unit_raw  = getattr(seg, "unit_raw", None) or _pdf_unit_raw
                _unit_mult = getattr(seg, "unit_multiplier", None) or _pdf_unit_mult

                if _unit_raw is None and _unit_mult is None:
                    logger.warning(
                        "[SEG_UNIT_TRACE] unit missing: ticker=%s seg=%s sales=%s period_type=%s",
                        filing.ticker, seg_name, getattr(seg, "segment_sales", None), ep_period_type,
                    )
                elif str(filing.ticker) in _TRACE_TICKERS:
                    logger.info(
                        "[SEG_UNIT_TRACE] ticker=%s segment=%s unit_raw=%r "
                        "unit_multiplier=%s sales_before=%s period_type=%s",
                        filing.ticker, seg_name, _unit_raw, _unit_mult,
                        getattr(seg, "segment_sales", None), ep_period_type,
                    )

                records.append({
                    "ticker":           filing.ticker,
                    "period":           ep_period,
                    "quarter":          quarter,
                    "segment_name":     seg_name,
                    "segment_order":    getattr(seg, "segment_order", 0),
                    "segment_sales":    getattr(seg, "segment_sales", None),
                    "segment_profit":   getattr(seg, "segment_profit", None),
                    "unit_raw":         _unit_raw,
                    "unit_multiplier":  _unit_mult,
                    "raw_profit_label": getattr(seg, "raw_profit_label", ""),
                    "source":           "backfill_v4_pdf",
                    "segment_name_norm": _normalize_segment_name_conservative(seg_name),
                    "extractor_route":  f"v4_{getattr(seg, 'extraction_engine', 'pdf')}",
                    "source_doc_type":  "earnings_summary",
                    "disclosure_date":  filing.disclosure_date,
                    "tdnet_doc_id":     fid,
                    "row_type":         _classify_row_type(seg_name),
                })
    else:
        # extracted_periods が空の場合は従来通り v4_result.segments を使う
        for seg in v4_result.segments:
            seg_name = getattr(seg, "segment_name", "") or ""
            _unit_raw  = getattr(seg, "unit_raw", None) or _pdf_unit_raw
            _unit_mult = getattr(seg, "unit_multiplier", None) or _pdf_unit_mult

            if _unit_raw is None and _unit_mult is None:
                logger.warning(
                    "[SEG_UNIT_TRACE] unit missing: ticker=%s seg=%s sales=%s",
                    filing.ticker, seg_name, getattr(seg, "segment_sales", None),
                )
            elif str(filing.ticker) in _TRACE_TICKERS:
                logger.info(
                    "[SEG_UNIT_TRACE] ticker=%s segment=%s unit_raw=%r "
                    "unit_multiplier=%s sales_before=%s",
                    filing.ticker, seg_name, _unit_raw, _unit_mult,
                    getattr(seg, "segment_sales", None),
                )

            records.append({
                "ticker":           filing.ticker,
                "period":           period,
                "quarter":          quarter,
                "segment_name":     seg_name,
                "segment_order":    getattr(seg, "segment_order", 0),
                "segment_sales":    getattr(seg, "segment_sales", None),
                "segment_profit":   getattr(seg, "segment_profit", None),
                "unit_raw":         _unit_raw,
                "unit_multiplier":  _unit_mult,
                "raw_profit_label": getattr(seg, "raw_profit_label", ""),
                "source":           "backfill_v4_pdf",
                "segment_name_norm": _normalize_segment_name_conservative(seg_name),
                "extractor_route":  f"v4_{getattr(seg, 'extraction_engine', 'pdf')}",
                "source_doc_type":  "earnings_summary",
                "disclosure_date":  filing.disclosure_date,
                "tdnet_doc_id":     fid,
                "row_type":         _classify_row_type(seg_name),
            })

    if not records:
        return SourceCandidate(
            source="pdf", attempted=True, available=True,
            error="v4_no_records_after_conversion",
            rule_trace=getattr(v4_result, "rule_trace", []),
        )

    validation = validate_extraction_result(records, source="pdf_compat")
    return SourceCandidate(
        source="pdf", attempted=True, available=True,
        segment_records=records, validation=validation,
        rule_trace=getattr(v4_result, "rule_trace", []),
    )



# ============================================================
# メインエントリポイント
# ============================================================

def process_one_filing_v4(
    filing, *,
    cache_root: str = "data/tdnet_cache",
    state_store=None,
    retry_download: int = 3, retry_xbrl: int = 2, retry_pdf: int = 1,
    timeout_download: int = 30, timeout_xbrl: int = 60, timeout_pdf: int = 120,
    run_id: str | None = None,
    sleep_fn=None,
) -> FilingResultV2:
    """V4 パイプライン: XBRL-first → V4 PDF fallback。V1 fallback なし。"""
    _ensure_imports()
    from lib.backfill.cache import (
        ensure_cache_layout, write_metadata, has_pdf, has_xbrl,
        save_extract_financials_result, save_extract_segments_result,
        save_quarantine, append_filing_log,
    )
    import time as _time
    _sleep = sleep_fn or _time.sleep

    t0 = time.monotonic()
    fid = filing.filing_id
    metrics: dict = {"attempts": {}, "pipeline": "v4"}
    paths = ensure_cache_layout(cache_root, fid)
    write_metadata(paths, filing)

    _update_state(state_store, fid, "running", stage="downloading_v4")
    append_filing_log(paths, {"event": "v4_start", "ticker": filing.ticker, "run_id": run_id})

    # Step 1: Download
    doc_path, xbrl_path = _download_originals(
        filing, paths, metrics,
        retry_download=retry_download, timeout_download=timeout_download, sleep_fn=_sleep,
    )

    if not doc_path and not xbrl_path:
        elapsed = int((time.monotonic() - t0) * 1000)
        return FilingResultV2(
            filing_id=fid, status="failed", source="", selected_path="none",
            confidence=0.0, reason="ダウンロード失敗", hard_fail_reason="",
            quarantine_reason="download_failed", fallback_used=False, fallback_reason="",
            raw_segment_count=0, valid_segment_count=0, invalid_segment_count=0,
            sales_non_null_count=0, profit_non_null_count=0,
            metrics={**metrics, "total_ms": elapsed},
            cache_paths={"cache_dir": str(paths.cache_dir)},
        )

    # PL 抽出 (全 source 共通)
    _update_state(state_store, fid, "running", stage="extracting_v4")
    financials_data, fin_via = _extract_financials_data(
        doc_path, xbrl_path, filing, metrics,
        retry_xbrl=retry_xbrl, retry_pdf=retry_pdf,
        timeout_xbrl=timeout_xbrl, timeout_pdf=timeout_pdf, sleep_fn=_sleep,
    )
    if financials_data:
        save_extract_financials_result(paths, financials_data)

    # Step 2: XBRL 抽出試行
    from src.segment.extraction_result_validator import validate_extraction_result

    candidates: list[SourceCandidate] = []

    xbrl_candidate = _try_xbrl_source(
        xbrl_path, doc_path, filing, financials_data, fid, paths, metrics,
        retry_xbrl=retry_xbrl, timeout_xbrl=timeout_xbrl, sleep_fn=_sleep,
    )
    candidates.append(xbrl_candidate)

    # Step 3: XBRL が成功していれば採用、そうでなければ V4 PDF
    xbrl_ok = (
        xbrl_candidate.attempted
        and xbrl_candidate.available
        and xbrl_candidate.validation is not None
        and xbrl_candidate.validation.status.value in ("success", "partial")
        and xbrl_candidate.segment_records
    )

    fallback_used = False
    fallback_reason = ""

    if xbrl_ok:
        best = xbrl_candidate
        pdf_candidate = SourceCandidate(
            source="pdf", attempted=False, available=bool(doc_path),
            skip_reason="xbrl_succeeded",
        )
        candidates.append(pdf_candidate)
        logger.debug(f"[v4] XBRL succeeded: fid={fid} n={len(best.segment_records)}")
    else:
        # XBRL 失敗 or なし → V4 PDF fallback
        fallback_used = True
        fallback_reason = xbrl_candidate.error or "xbrl_unavailable"

        # ETF/ETN 判定（固定ティッカー + タイトル）: PDF・AI へ流す前に早期 normal skip
        _ETF_FIXED_TICKERS = {
            "1329", "1356", "1360", "1364", "1398", "1469",
            "1475", "1476", "1477", "1478", "1483", "1488",
            "1568", "1569", "1579", "1580", "1655", "1657",
            "1658", "1659", "2013", "2014", "201A", "2250",
            "2522", "2563", "2851", "2852", "2864", "313A",
            "314A", "392A",
        }
        # title 取得: getattr だけでは None/空になるケースがあるため複数候補を走査
        _filing_title = ""
        for _attr in ("title", "document_title", "filing_title", "name"):
            _v = getattr(filing, _attr, None)
            if _v and isinstance(_v, str) and _v.strip():
                _filing_title = _v.strip()
                break
        if not _filing_title:
            # 最終手段: __dict__ を全探索して文字列っぽいキーを拾う
            _fd = getattr(filing, "__dict__", {})
            for _k, _v in _fd.items():
                if "title" in _k.lower() and isinstance(_v, str) and _v.strip():
                    _filing_title = _v.strip()
                    logger.debug("[v4] filing.title fallback via __dict__: key=%s val=%r", _k, _filing_title[:60])
                    break
        logger.debug("[v4] ETF gate title=%r ticker=%s", _filing_title[:80] if _filing_title else "", filing.ticker)
        if str(filing.ticker) in _ETF_FIXED_TICKERS or _is_etf_like_title(_filing_title):
            logger.info(
                "[v4] NORMAL SKIP ETF_LIKE ticker=%s title=%s",
                filing.ticker, _filing_title,
            )
            _elapsed_etf = int((time.monotonic() - t0) * 1000)
            metrics["total_ms"] = _elapsed_etf
            append_filing_log(paths, {
                "event": "skipped_normal", "ticker": filing.ticker,
                "reason": "etf_like", "pipeline": "v4",
            })
            return FilingResultV2(
                filing_id=fid, status="skipped_normal",
                source="", selected_path="none",
                confidence=0.0, reason="etf_like",
                hard_fail_reason="", quarantine_reason="",
                fallback_used=False, fallback_reason="",
                raw_segment_count=0, valid_segment_count=0,
                invalid_segment_count=0, sales_non_null_count=0,
                profit_non_null_count=0,
                metrics={**metrics, "total_ms": _elapsed_etf},
                cache_paths={"cache_dir": str(paths.cache_dir)},
                route_mode="normal_skip",
            )

        # REIT / 投資法人 / インフラファンド 判定（固定ティッカー + タイトル）: PDF・AI へ流さない
        _REIT_FIXED_TICKERS = {"2971", "3281", "8968", "8984"}
        if str(filing.ticker) in _REIT_FIXED_TICKERS or _is_reit_like_title(_filing_title):
            logger.info(
                "[v4] NORMAL SKIP REIT ticker=%s title=%s",
                filing.ticker, _filing_title,
            )
            _elapsed_reit = int((time.monotonic() - t0) * 1000)
            metrics["total_ms"] = _elapsed_reit
            append_filing_log(paths, {
                "event": "skipped_normal", "ticker": filing.ticker,
                "reason": "reit_like", "pipeline": "v4",
            })
            return FilingResultV2(
                filing_id=fid, status="skipped_normal",
                source="", selected_path="none",
                confidence=0.0, reason="reit_like",
                hard_fail_reason="", quarantine_reason="",
                fallback_used=False, fallback_reason="",
                raw_segment_count=0, valid_segment_count=0,
                invalid_segment_count=0, sales_non_null_count=0,
                profit_non_null_count=0,
                metrics={**metrics, "total_ms": _elapsed_reit},
                cache_paths={"cache_dir": str(paths.cache_dir)},
                route_mode="normal_skip",
            )

        logger.info(
            f"[v4] XBRL fallback to V4 PDF: fid={fid} ticker={filing.ticker} "
            f"reason={fallback_reason}"
        )
        pdf_candidate = _try_pdf_source_v4(
            doc_path, filing, financials_data, fid, metrics,
        )
        candidates.append(pdf_candidate)

        if pdf_candidate.validation and pdf_candidate.segment_records:
            best = pdf_candidate
        else:
            # 正常スキップ判定（XBRL・PDF 両方失敗時のみ実行）
            _pdf_err = pdf_candidate.error or ""
            _cached_normal_skip = ""
            if not _pdf_err.startswith("normal_skip:") and not _is_ai_fallback_applicable(_pdf_err):
                # doc_path なし等でまだ normal_skip 判定されていない場合のみ補完
                _cached_normal_skip = _detect_normal_skip(doc_path, filing.ticker, getattr(filing, 'title', ''))
                _pdf_err = f"normal_skip:{_cached_normal_skip}"
            if _pdf_err.startswith("normal_skip:"):
                _skip_reason = _pdf_err[len("normal_skip:"):]
                if _skip_reason:
                    logger.info(
                        "[v4] NORMAL SKIP pdf=%s ticker=%s reason=%s",
                        doc_path, filing.ticker, _skip_reason,
                    )
                    elapsed = int((time.monotonic() - t0) * 1000)
                    metrics["total_ms"] = elapsed
                    append_filing_log(paths, {
                        "event": "skipped_normal", "ticker": filing.ticker,
                        "reason": _skip_reason, "pipeline": "v4",
                    })
                    return FilingResultV2(
                        filing_id=fid, status="skipped_normal",
                        source="pdf", selected_path="none",
                        confidence=0.0, reason=_skip_reason,
                        hard_fail_reason="", quarantine_reason="",
                        fallback_used=True, fallback_reason=fallback_reason,
                        raw_segment_count=0, valid_segment_count=0,
                        invalid_segment_count=0, sales_non_null_count=0,
                        profit_non_null_count=0,
                        metrics={**metrics, "total_ms": elapsed},
                        cache_paths={"cache_dir": str(paths.cache_dir)},
                        route_mode="normal_skip",
                    )
            # 両方失敗 → AI フォールバック試行
            _pdf_err_for_ai = pdf_candidate.error or ""
            # AI 前に単一セグメント・省略系を再確認（_is_ai_fallback_applicable=True パスでの漏れを捕捉）
            _pre_ai_skip = _cached_normal_skip or _detect_normal_skip(doc_path, filing.ticker, _filing_title)
            if _pre_ai_skip:
                logger.info(
                    "[v4] PRE-AI NORMAL SKIP ticker=%s reason=%s",
                    filing.ticker, _pre_ai_skip,
                )
                elapsed = int((time.monotonic() - t0) * 1000)
                metrics["total_ms"] = elapsed
                append_filing_log(paths, {
                    "event": "skipped_normal", "ticker": filing.ticker,
                    "reason": _pre_ai_skip, "pipeline": "v4",
                })
                return FilingResultV2(
                    filing_id=fid, status="skipped_normal",
                    source="", selected_path="none",
                    confidence=0.0, reason=_pre_ai_skip,
                    hard_fail_reason="", quarantine_reason="",
                    fallback_used=True, fallback_reason=fallback_reason,
                    raw_segment_count=0, valid_segment_count=0,
                    invalid_segment_count=0, sales_non_null_count=0,
                    profit_non_null_count=0,
                    metrics={**metrics, "total_ms": elapsed},
                    cache_paths={"cache_dir": str(paths.cache_dir)},
                    route_mode="normal_skip",
                )
            if _is_ai_fallback_applicable(_pdf_err_for_ai):
                # セグメントページ存在チェック: ページ自体がない案件は AI に送っても意味がない
                if not _has_segment_page_signal(doc_path or "", ticker=filing.ticker):
                    logger.info(
                        "[v4] PRE-AI NO_SEGMENT_PAGE ticker=%s reason=no_segment_page",
                        filing.ticker,
                    )
                    elapsed = int((time.monotonic() - t0) * 1000)
                    metrics["total_ms"] = elapsed
                    append_filing_log(paths, {
                        "event": "skipped_normal", "ticker": filing.ticker,
                        "reason": "no_segment_page", "pipeline": "v4",
                    })
                    return FilingResultV2(
                        filing_id=fid, status="skipped_normal",
                        source="", selected_path="none",
                        confidence=0.0, reason="no_segment_page",
                        hard_fail_reason="", quarantine_reason="",
                        fallback_used=True, fallback_reason=fallback_reason,
                        raw_segment_count=0, valid_segment_count=0,
                        invalid_segment_count=0, sales_non_null_count=0,
                        profit_non_null_count=0,
                        metrics={**metrics, "total_ms": elapsed},
                        cache_paths={"cache_dir": str(paths.cache_dir)},
                        route_mode="normal_skip",
                    )
                _ai_result = extract_segments_with_ai(
                    doc_path or "",
                    ticker=filing.ticker,
                    title=getattr(filing, "title", ""),
                )
                if _ai_result and _ai_result.get("success"):
                    # AI 成功 → AI 抽出結果を SourceCandidate 相当に変換して採用
                    _ai_segs = _ai_result["segments"]

                    # ── Step A: 既存ロジック（financials_data or title）──
                    _ai_period = (financials_data or {}).get("period", "")
                    _ai_quarter = (financials_data or {}).get("quarter", "")
                    if not _ai_period or not _ai_quarter:
                        from src.year_parser import extract_fiscal_info
                        _fy, _fq = extract_fiscal_info(getattr(filing, "title", ""))
                        if not _ai_period and _fy:
                            _ai_period = _fy
                        if not _ai_quarter and _fq:
                            _ai_quarter = _fq

                    # ── Step B: PDF本文 + タイトル + AI出力 で period/quarter を補完 ──
                    _title_str = getattr(filing, "title", "") or ""
                    _page_text_for_ctx = ""
                    if not _ai_quarter:
                        # quarter がまだ解決できていない場合のみ PDF を再読み込み（コスト抑制）
                        _page_text_for_ctx = _extract_text_from_pdf(doc_path or "")
                    _ai_ctx = resolve_ai_period_context(
                        title=_title_str,
                        ai_segments=_ai_segs,
                        page_text_hint=_page_text_for_ctx,
                    )
                    _ctx_period_type = _ai_ctx["period_type"]
                    _ctx_quarter = _ai_ctx["quarter"]
                    _ctx_reason = _ai_ctx["reason"]
                    _ctx_confidence = _ai_ctx["confidence"]

                    # quarter を補完（既存ロジックで取れなかった場合のみ）
                    if not _ai_quarter and _ctx_quarter != "unknown":
                        _ai_quarter = _ctx_quarter

                    # ── Step C: 補完状態をログ・メトリクスに記録 ──
                    _period_type_resolved = _ctx_period_type in ("current", "previous")
                    _quarter_resolved = bool(_ai_quarter) and _ai_quarter != "unknown"
                    if _period_type_resolved or _quarter_resolved:
                        logger.info(
                            "[v4] AI PERIOD RESOLVED ticker=%s period_type=%s "
                            "quarter=%s reason=%s confidence=%s",
                            filing.ticker, _ctx_period_type,
                            _ai_quarter or "unknown", _ctx_reason, _ctx_confidence,
                        )
                    else:
                        logger.warning(
                            "[v4] AI PERIOD UNRESOLVED ticker=%s reason=%s",
                            filing.ticker, _ctx_reason,
                        )
                    metrics["ai_period_type_resolved"] = _period_type_resolved
                    metrics["ai_quarter_resolved"] = _quarter_resolved
                    metrics["ai_period_reason"] = _ctx_reason

                    # ── Step D: _ai_period が取れない場合は records 化不可 ──
                    if not _ai_period:
                        logger.warning(
                            "[v4] AI FALLBACK OK but period/quarter unresolved: ticker=%s",
                            filing.ticker,
                        )
                        metrics["ai_used"] = True
                        metrics["ai_reason"] = "ai_period_unresolved"
                        best = xbrl_candidate
                    else:
                        # ── Step E: records 化（quarter=unknown でも継続）──
                        _effective_quarter = _ai_quarter or "unknown"
                        _ai_records = []
                        for _i, _seg in enumerate(_ai_segs, start=1):
                            _seg_name = (_seg.get("segment_name") or "").strip()
                            # period_type 採用順:
                            #   1. ctx が current/previous → 全 seg に適用
                            #   2. AI 各 seg の period_type（mixed は渡さない）
                            #   3. unknown
                            if _ctx_period_type in ("current", "previous"):
                                _seg_period_type = _ctx_period_type
                            else:
                                _seg_period_type = (
                                    _seg.get("period_type", "unknown") or "unknown"
                                )
                                # AI が mixed を返した場合は unknown に落とす
                                if _seg_period_type == "mixed":
                                    _seg_period_type = "unknown"
                            _ai_records.append({
                                "ticker": filing.ticker,
                                "period": _ai_period,
                                "quarter": _effective_quarter,
                                "segment_name": _seg_name,
                                "segment_order": _i,
                                "segment_sales": _seg.get("sales"),
                                "segment_profit": _seg.get("profit"),
                                "raw_profit_label": "セグメント利益",
                                "source": "backfill_v4_ai",
                                "segment_name_norm": _normalize_segment_name_conservative(_seg_name),
                                "extractor_route": "v4_ai_text",
                                "source_doc_type": "earnings_summary",
                                "disclosure_date": filing.disclosure_date,
                                "tdnet_doc_id": fid,
                                "row_type": _classify_row_type(_seg_name),
                                "period_type": _seg_period_type,
                            })
                        if _ai_records:
                            from src.segment.extraction_result_validator import validate_extraction_result
                            _ai_validation = validate_extraction_result(_ai_records, source="ai_text")
                            _ai_candidate = SourceCandidate(
                                source="ai",
                                attempted=True,
                                available=True,
                                segment_records=_ai_records,
                                validation=_ai_validation,
                            )
                            candidates.append(_ai_candidate)
                            metrics["ai_used"] = True
                            metrics["ai_reason"] = "ai_ok"
                            metrics["ai_segment_count"] = len(_ai_records)
                            append_filing_log(paths, {
                                "event": "ai_fallback_ok",
                                "ticker": filing.ticker,
                                "segments": len(_ai_records),
                                "period": _ai_period,
                                "quarter": _effective_quarter,
                                "period_type": _ctx_period_type,
                                "pipeline": "v4",
                            })
                            best = _ai_candidate
                        else:
                            metrics["ai_used"] = True
                            metrics["ai_reason"] = "ai_no_records_after_conversion"
                            best = xbrl_candidate
                else:
                    # AI 失敗
                    _ai_reason = (_ai_result or {}).get("reason", "ai_skipped") if _ai_result else "ai_skipped"
                    metrics["ai_used"] = True
                    metrics["ai_reason"] = _ai_reason
                    best = xbrl_candidate
            else:
                # AI フォールバック非対象（通常の quarantine 経路）
                # PDF error に単一セグメント / 省略系の理由が記録されている場合は
                # quarantine に入れず skipped_normal として返す（B優先度案件の救済）
                _NS_SKIP_REASONS = {"single_segment_omitted", "segment_disclosure_omitted"}
                _raw_pdf_err_ns = pdf_candidate.error or ""
                _ns_skip = next(
                    (r for r in _NS_SKIP_REASONS if r in _raw_pdf_err_ns), ""
                )
                if _ns_skip:
                    logger.info(
                        "[phase2_v4] normal skip after xbrl failure: reason=%s ticker=%s",
                        _ns_skip, filing.ticker,
                    )
                    elapsed = int((time.monotonic() - t0) * 1000)
                    metrics["total_ms"] = elapsed
                    append_filing_log(paths, {
                        "event": "skipped_normal", "ticker": filing.ticker,
                        "reason": _ns_skip, "pipeline": "v4",
                    })
                    return FilingResultV2(
                        filing_id=fid, status="skipped_normal",
                        source="", selected_path="none",
                        confidence=0.0, reason=_ns_skip,
                        hard_fail_reason="", quarantine_reason="",
                        fallback_used=True, fallback_reason=fallback_reason,
                        raw_segment_count=0, valid_segment_count=0,
                        invalid_segment_count=0, sales_non_null_count=0,
                        profit_non_null_count=0,
                        metrics={**metrics, "total_ms": elapsed},
                        cache_paths={"cache_dir": str(paths.cache_dir)},
                        route_mode="normal_skip",
                    )
                metrics["ai_used"] = False
                best = xbrl_candidate

    # Step 4: FilingResultV2 構築
    validation = best.validation
    xbrl_resolved = xbrl_candidate.attempted and xbrl_candidate.available

    if validation:
        worker_status = validator_status_to_worker(validation.status.value)
        confidence = validation.confidence
        reason = validation.reason
        hard_fail_reason = validation.hard_fail_reason.value
        raw_seg_count = validation.raw_segment_count
        valid_seg_count = validation.valid_segment_count
        invalid_seg_count = validation.invalid_segment_count
        sales_nn = validation.sales_non_null_count
        profit_nn = validation.profit_non_null_count
        invalid_names = validation.invalid_names
        account_like_ratio = validation.account_like_ratio
        narrative_contamination = validation.narrative_contamination
    else:
        worker_status = "quarantined"
        confidence = 0.0
        reason = best.error or "no_extraction_attempted"
        hard_fail_reason = "no_records"
        raw_seg_count = valid_seg_count = invalid_seg_count = 0
        sales_nn = profit_nn = 0
        invalid_names = []
        account_like_ratio = 0.0
        narrative_contamination = False

    quarantine_reason = hard_fail_reason if worker_status == "quarantined" else ""

    segment_records = best.segment_records
    fp = compute_result_fingerprint(segment_records) if segment_records else None

    if segment_records:
        save_extract_segments_result(paths, segment_records)

    # candidate_summary
    summary_parts = []
    for c in candidates:
        if not c.attempted and not c.available:
            summary_parts.append(f"{c.source}:skip({c.skip_reason or 'not_available'})")
        elif c.validation:
            vs = c.validation.status.value
            vr = c.validation.hard_fail_reason.value
            summary_parts.append(f"{c.source}:{vs}" + (f"({vr})" if vr else ""))
        elif c.error:
            summary_parts.append(f"{c.source}:error({c.error[:50]})")
        else:
            summary_parts.append(f"{c.source}:not_attempted({c.skip_reason})")
    candidate_summary = " → ".join(summary_parts)

    elapsed = int((time.monotonic() - t0) * 1000)
    metrics["total_ms"] = elapsed

    quarantine_dict = None
    if worker_status == "quarantined":
        quarantine_dict = {
            "filing_id": fid, "ticker": filing.ticker,
            "stage": "segment_extraction_v4",
            "review_hint": quarantine_reason,
            "hard_fail_reason": hard_fail_reason,
            "selected_source": best.source,
            "candidate_summary": candidate_summary,
        }
        save_quarantine(paths, quarantine_dict)

    route_mode = (
        "xbrl_v2" if xbrl_ok
        else "v4_ai_text" if metrics.get("ai_reason") == "ai_ok"
        else "v4_pdf"
    )

    debug_entry = _build_debug_log(
        fid, candidates, best, worker_status, confidence,
        hard_fail_reason, quarantine_reason, fallback_used, fallback_reason,
        valid_seg_count, sales_nn, profit_nn, candidate_summary, route_mode,
    )
    logger.debug(json.dumps(debug_entry, ensure_ascii=False, default=str))

    log_event = "ok" if worker_status in ("ok", "partial") else "quarantined"
    append_filing_log(paths, {
        "event": log_event,
        "via": best.source,
        "status": worker_status,
        "segments": len(segment_records),
        "fingerprint": fp,
        "hard_fail_reason": hard_fail_reason,
        "fallback_used": fallback_used,
        "candidate_summary": candidate_summary,
        "pipeline": "v4",
    })

    selected_path = best.source if (validation and segment_records) else "none"

    logger.info(
        "[path-timer] ticker=%s path=%s elapsed_ms=%d",
        filing.ticker, selected_path, elapsed,
    )

    return FilingResultV2(
        filing_id=fid,
        status=worker_status,
        source=best.source if (validation and segment_records) else "",
        selected_path=selected_path,
        confidence=confidence,
        reason=reason,
        hard_fail_reason=hard_fail_reason,
        quarantine_reason=quarantine_reason,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        raw_segment_count=raw_seg_count,
        valid_segment_count=valid_seg_count,
        invalid_segment_count=invalid_seg_count,
        sales_non_null_count=sales_nn,
        profit_non_null_count=profit_nn,
        invalid_names=invalid_names,
        account_like_ratio=account_like_ratio,
        narrative_contamination=narrative_contamination,
        segment_records=segment_records,
        financial_records=[financials_data] if financials_data else [],
        via=selected_path,
        metrics=metrics,
        cache_paths={"cache_dir": str(paths.cache_dir)},
        quarantine=quarantine_dict,
        result_fingerprint=fp,
        rule_trace=getattr(best, "rule_trace", []),
        score_summary=getattr(best, "score_summary", {}),
        candidates=candidates,
        candidate_summary=candidate_summary,
        route_mode=route_mode,
    )
