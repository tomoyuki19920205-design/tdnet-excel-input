#!/usr/bin/env python3
"""tools/parse_tanshin_worker.py — 本番相当 XBRL 解析ワーカー (サブプロセス専用) v3

本番 EARNINGS_V2 パイプラインで実際に行われる処理を、
DB / Supabase / Discord への書き込みを一切行わずに再現する。

起動モード:
  [A] --input-json モード (Phase 3-1a 以降・推奨)
        echo '{"zip_path": "...", "title": "...", ...}' | python parse_tanshin_worker.py --input-json
        → stdin から JSON を読み込み、親プロセスが渡したメタデータ全体を使用する。
        → title は入力 JSON の値を正として使用。SQLite / Supabase からの推定は行わない。

  [B] CLI 引数モード (後方互換)
        python parse_tanshin_worker.py <zip_path> [title] [ticker]
        → Phase 2b までの既存呼び出し方法。引き続き動作する。

  [C] テスト用フラグ
        --hang         : 60s sleep で意図的 timeout テスト
        --corrupt-json : 壊れた JSON を出力して parse error テスト

呼ぶ本番関数 (副作用なし):
  - extract_earnings_data()        : XBRL/PDF から PL 数値・YOY・セグメント抽出
  - extract_company_info_from_zip(): 会社名・証券コード抽出 (company_name が未指定の場合のみ)
  - extract_narrative_from_xbrl_zip(): 経営成績説明文テキスト抽出
  - extract_narrative()            : 理由テキスト構造化
  - format_earnings_message()      : 本番通知メッセージ生成 (AIなし版)
  - extract_guidance_from_zip()    : 来期ガイダンス抽出 (FY/4Q のみ)
  - format_guidance_section()      : ガイダンス表示文生成

スキップする処理 (副作用あり):
  - save_earnings_summary()    → SQLite 書き込み → スキップ
  - save_event_to_supabase()   → Supabase 書き込み → スキップ
  - send_earnings_discord()    → Discord 通知 → スキップ
  - download_document()        → ネットワーク DL → スキップ (既存 ZIP のみ使用)
  - call_reason_format_api()   → AI API 呼び出し → スキップ (reasons=生テキスト分割)

出力: JSON を stdout に 1 行 (UTF-8, ensure_ascii=True)
ログ: stderr のみ (stdout は JSON 専用)

入力 JSON 必須フィールド (--input-json モード):
  zip_path, ticker, company_name, title, source_title,
  disclosed_at, source_url, pdf_url, source_doc_id,
  xbrl_doc_id, archive_date, event_type

出力 JSON 必須フィールド:
  status, ticker, company_name, fiscal_year, quarter,
  sales_current, sales_yoy, op_current, op_yoy,
  primary_metric_name, primary_metric_value, primary_metric_yoy,
  has_yoy, is_fy_or_4q, has_guidance, narrative_extracted,
  formatted_message, formatted_message_length,
  extracted_payload, notification_compare_json,
  fingerprint, source_url, pdf_url, source_doc_id, archive_date,
  elapsed_ms, worker_version, error_type, error_message
"""

import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

# ── stdout / stdin を UTF-8 固定 (Windows cp932 対策) ──────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

# ── プロジェクトルートを sys.path に追加 ───────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_VERSION = "3.0.0"

# ─── 入力 JSON 必須フィールド ─────────────────────────────────────────────────

_REQUIRED_FIELDS = [
    "zip_path",
    "ticker",
    "title",
    "event_type"
]


# ─── ロギング ─────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """stderr にログを出力する。stdout は JSON 専用。"""
    print(f"[worker] {msg}", file=sys.stderr, flush=True)


def _sanitize_str(s: str) -> str:
    """サロゲート文字 (\udc00-\udcff) を除去して安全な文字列にする。"""
    if not isinstance(s, str):
        return s
    return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _sanitize_value(v):
    """任意の値を再帰的にサロゲート除去する。"""
    if isinstance(v, str):
        return _sanitize_str(v)
    elif isinstance(v, dict):
        return {_sanitize_str(k): _sanitize_value(vv) for k, vv in v.items()}
    elif isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    return v


def _emit(result: dict) -> None:
    """stdout に JSON を 1 行で出力する (UTF-8, ensure_ascii=True)。
    サロゲート文字を除去してから json.dumps する。
    """
    safe = _sanitize_value(result)
    line = json.dumps(safe, ensure_ascii=True) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


# ─── エラー結果ファクトリ ────────────────────────────────────────────────────

def _make_error_result(
    *,
    error_type: str,
    error_message: str,
    elapsed_ms: float,
    ticker: str = "",
    company_name: str = "",
    zip_path: str = "",
    source_url: str = "",
    pdf_url: str = "",
    source_doc_id: str = "",
    archive_date: str = "",
) -> dict:
    """エラー時の出力 JSON を生成する。必須フィールドをすべて含む。"""
    return {
        "status": "error",
        # ── 識別情報 ──
        "zip_path": zip_path,
        "zip_filename": Path(zip_path).name if zip_path else "",
        "ticker": ticker,
        "company_name": company_name,
        "source_doc_id": source_doc_id,
        "archive_date": archive_date,
        # ── 数値 ──
        "fiscal_year": "",
        "quarter": "",
        "sales_current": None,
        "sales_yoy": None,
        "op_current": None,
        "op_yoy": None,
        "primary_metric_name": "",
        "primary_metric_value": None,
        "primary_metric_yoy": None,
        "has_yoy": False,
        "is_fy_or_4q": False,
        "has_guidance": False,
        "narrative_extracted": False,
        # ── メッセージ ──
        "formatted_message": "",
        "formatted_message_length": 0,
        "formatted_message_preview": "",
        # ── ペイロード ──
        "extracted_payload": {},
        "notification_compare_json": {"compare": {}, "current": {}},
        "fingerprint": "",
        # ── URL ──
        "source_url": source_url,
        "pdf_url": pdf_url,
        # ── タイミング ──
        "elapsed_ms": elapsed_ms,
        "section_timings_ms": {},
        # ── エラー情報 ──
        "worker_version": _VERSION,
        "error_type": error_type,
        "error_message": error_message,
    }


# ─── ファイル名パーサ ───────────────────────────────────────────────────────────

def _parse_zip_filename(zip_filename: str):
    """Returns (archive_date, doc_id, ticker_fn, is_short)"""
    m_long = re.match(r"^([A-Z0-9]{4,5})_(\d{8})_(\d{14,18})\.zip$", zip_filename)
    if m_long:
        ticker, archive_date, doc_id = m_long.groups()
        return archive_date, doc_id, ticker, False
    m_short = re.match(r"^(\d{14,18})\.zip$", zip_filename)
    if m_short:
        doc_id = m_short.group(1)
        base_14 = doc_id[-14:]
        return base_14[:8], doc_id, "", True
    return None, None, "", False


def _base14(doc_id: str) -> str:
    return doc_id[-14:] if len(doc_id) >= 14 else doc_id


# ─── fiscal_year / quarter 推定 (本番ロジック移植) ────────────────────────────

_QUARTER_EXCLUDE_RE = re.compile(r"[1-3]四半期|中間")
_FY_TANSHIN_TITLE_RE = re.compile(r"\d{4}年\s*\d{1,2}月\s*期\s*決算短信")


def _normalize_title(title: str) -> str:
    return unicodedata.normalize("NFKC", title)


def _parse_fiscal_info(title: str, earnings, disclosed_at: str = "") -> tuple[str, str]:
    """fiscal_year, quarter を推定する (本番 _parse_fiscal_info 相当)。
    title は必ず親プロセスから渡された入力 JSON の title を使う。
    """
    fiscal_year_raw = earnings.period or ""

    # XBRL から取れない場合はタイトルから
    if not fiscal_year_raw and title:
        m = re.search(r"(\d{4})年\d{1,2}月期", title)
        if m:
            fiscal_year_raw = m.group(1)

    fiscal_year = ""
    if fiscal_year_raw:
        if len(fiscal_year_raw) >= 4 and fiscal_year_raw[:4].isdigit():
            fiscal_year = fiscal_year_raw[:4]

    if not fiscal_year and disclosed_at:
        m = re.match(r"^(\d{4})", disclosed_at)
        if m:
            fiscal_year = m.group(1)

    # quarter 推定 — 入力 JSON の title が判定の正
    quarter = ""
    normalized = _normalize_title(title)
    m = re.search(r"第(\d)四半期", normalized)
    if m:
        quarter = f"{m.group(1)}Q"
    elif "通期" in title or "本決算" in title:
        quarter = "FY"
    elif earnings.quarter:
        quarter = earnings.quarter
    else:
        if _FY_TANSHIN_TITLE_RE.search(normalized) and not _QUARTER_EXCLUDE_RE.search(normalized):
            quarter = "FY"

    return fiscal_year, quarter


def _is_fy_or_4q(earnings, title: str) -> tuple[bool, str]:
    if earnings.quarter in ("FY", "4Q"):
        return True, f"quarter={earnings.quarter}"
    if earnings.quarter in ("1Q", "2Q", "3Q"):
        return False, f"quarter={earnings.quarter}"
    normalized = _normalize_title(title)
    if _QUARTER_EXCLUDE_RE.search(normalized):
        return False, "title_contains_quarter_keyword"
    if re.search(r"通期|本決算", title):
        return True, "title_contains_tsuuki"
    if _FY_TANSHIN_TITLE_RE.search(normalized):
        return True, "title_fy_tanshin_pattern"
    return False, "no_fy_indicator"


# ─── primary_metric 生成 ───────────────────────────────────────────────────────

def _build_primary_metric(earnings, guidance) -> tuple[str, float | None, float | None]:
    if earnings.op_current is not None:
        return "営業利益", earnings.op_current, earnings.op_yoy
    elif earnings.sales_current is not None:
        return "売上高", earnings.sales_current, earnings.sales_yoy
    return "", None, None


# ─── notification_compare_json 生成 ──────────────────────────────────────────

def _build_notification_compare_json(earnings, guidance, fiscal_year: str, quarter: str) -> dict:
    """
    Viewer の 3 行目表示に使われる notification_compare_json を生成する。
    本番 Supabase の修復済み57件と同じ compare/current 形式で出力する。

    形式:
      {
        "compare": {"label": "前Q" or "来期FY予", "sales_yoy": ..., "op_yoy": ..., "source": ...},
        "current": {"label": "3Q" or "FY" etc., "sales_yoy": ..., "op_yoy": ...}
      }
    """
    current_label = quarter if quarter else fiscal_year or "current"
    current = {
        "label": current_label,
        "sales_yoy": round(earnings.sales_yoy, 6) if earnings.sales_yoy is not None else None,
        "op_yoy": round(earnings.op_yoy, 6) if earnings.op_yoy is not None else None,
    }

    if guidance and guidance.has_guidance:
        compare = {
            "label": "来期FY予",
            "sales_yoy": round(guidance.sales_yoy, 6) if guidance.sales_yoy is not None else None,
            "op_yoy": round(guidance.op_yoy, 6) if guidance.op_yoy is not None else None,
            "source": "xbrl_guidance",
        }
    else:
        compare_label = "前Q" if quarter and quarter not in ("FY", "4Q") else "通期予"
        compare = {
            "label": compare_label,
            "sales_yoy": round(earnings.sales_yoy, 6) if earnings.sales_yoy is not None else None,
            "op_yoy": round(earnings.op_yoy, 6) if earnings.op_yoy is not None else None,
            "source": earnings.source if earnings.source else "xbrl",
        }

    return {"compare": compare, "current": current}


# ─── メイン解析関数 ───────────────────────────────────────────────────────────

def _run_production_parse(
    zip_path: Path,
    doc_title: str,
    doc_ticker: str,
    doc_company_name: str = "",
    disclosed_at: str = "",
    source_url_override: str = "",
    pdf_url_override: str = "",
    source_doc_id_override: str = "",
    archive_date_override: str = "",
    xbrl_doc_id_override: str = "",
    disclosure_id_override: str = "",
) -> dict:
    """
    本番 EARNINGS_V2 パイプラインの解析部分を、DB/Supabase/Discord 書き込みなしで実行する。

    title は必ず呼び出し元から渡された値を使う。worker 内で SQLite / Supabase から
    title を補完する処理は行わない (3320問題対策)。

    スキップ: save_earnings_summary / save_event_to_supabase / send_earnings_discord
              / download_document / call_reason_format_api
    """
    t_start = time.perf_counter()

    zip_path_str = str(zip_path)
    zip_filename = zip_path.name

    # ── ZIP ファイル名からメタデータを補完 (入力値があれば優先) ──
    archive_date_fn, doc_id_fn, ticker_fn, is_short = _parse_zip_filename(zip_filename)

    ticker = doc_ticker or ticker_fn or ""
    archive_date = archive_date_override or archive_date_fn or ""
    b14_from_fn = _base14(doc_id_fn) if doc_id_fn else ""

    # source_doc_id / xbrl_doc_id は後のルールで確定させる
    doc_id = doc_id_fn or ""

    import re
    def _is_1401(s): return bool(s and len(s) == 18 and str(s).startswith("1401"))
    def _extract_1401(s):
        m = re.search(r'1401\d{14}', str(s)) if s else None
        return m.group(0) if m else ""

    # source_doc_id 決定ルール
    if _is_1401(source_doc_id_override):
        source_doc_id = source_doc_id_override
    elif _extract_1401(pdf_url_override):
        source_doc_id = _extract_1401(pdf_url_override)
    elif _extract_1401(source_url_override):
        source_doc_id = _extract_1401(source_url_override)
    elif _is_1401(disclosure_id_override):
        source_doc_id = disclosure_id_override
    elif not _is_1401(source_doc_id):
        source_doc_id = ""

    def _is_0812(s): return bool(s and len(s) == 18 and str(s).startswith("0812"))
    def _extract_0812(s):
        m = re.search(r'0812\d{14}', str(s)) if s else None
        return m.group(0) if m else ""

    # xbrl_doc_id 決定ルール
    if _is_0812(xbrl_doc_id_override):
        xbrl_doc_id = xbrl_doc_id_override
    elif _extract_0812(zip_filename):
        xbrl_doc_id = _extract_0812(zip_filename)
    elif _is_1401(source_doc_id):
        xbrl_doc_id = "0812" + source_doc_id[4:]
    else:
        xbrl_doc_id = ""

    # source_url / pdf_url: 入力値を優先、なければ source_doc_id から生成
    source_url = source_url_override or (
        f"https://www.release.tdnet.info/inbs/{source_doc_id}.pdf" if source_doc_id else ""
    )
    pdf_url = pdf_url_override or source_url

    t_sections: dict[str, float] = {}

    # ── 1. XBRL PL 数値抽出 ──────────────────────────────────────────────────
    try:
        from src.events.summary_financials import (
            extract_earnings_data,
            extract_company_info_from_zip,
            extract_narrative_from_xbrl_zip,
        )
        t0 = time.perf_counter()
        earnings = extract_earnings_data(xbrl_path=zip_path_str, title=doc_title, ticker=ticker)
        t_sections["extract_earnings_data_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        if earnings is None:
            elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
            return _make_error_result(
                error_type="no_yoy",
                error_message="extract_earnings_data returned None (YOYが計算できない)",
                elapsed_ms=elapsed_ms,
                ticker=ticker, company_name=doc_company_name,
                zip_path=zip_path_str, source_url=source_url,
                pdf_url=pdf_url, source_doc_id=source_doc_id,
                archive_date=archive_date,
            )

    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        return _make_error_result(
            error_type="extract_earnings_data_failed",
            error_message=str(e)[:200],
            elapsed_ms=elapsed_ms,
            ticker=ticker, company_name=doc_company_name,
            zip_path=zip_path_str, source_url=source_url,
            pdf_url=pdf_url, source_doc_id=source_doc_id,
            archive_date=archive_date,
        )

    # ── 2. 会社名解決 ────────────────────────────────────────────────────────
    # 入力 JSON の company_name を優先。空の場合のみ ZIP から抽出する。
    if doc_company_name:
        company_name = doc_company_name
        t_sections["extract_company_info_ms"] = 0.0
    else:
        try:
            t0 = time.perf_counter()
            company_name, _ = extract_company_info_from_zip(zip_path_str)
            t_sections["extract_company_info_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception:
            company_name = ticker
            t_sections["extract_company_info_ms"] = -1

    # ── 3. 経営成績テキスト抽出 ─────────────────────────────────────────────
    narrative = None
    try:
        from src.events.summary_narrative_extractor import extract_narrative
        t0 = time.perf_counter()
        narrative_text = extract_narrative_from_xbrl_zip(zip_path_str)
        if narrative_text:
            narrative = extract_narrative(narrative_text, title=doc_title)
        t_sections["extract_narrative_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception:
        t_sections["extract_narrative_ms"] = -1

    # ── 4. fiscal_year / quarter 推定 ────────────────────────────────────────
    # 入力 JSON の title を quarter 判定の正とする。SQLite / Supabase は参照しない。
    fiscal_year, quarter = _parse_fiscal_info(
        doc_title, earnings,
        disclosed_at=disclosed_at or archive_date or ""
    )

    # ── 5. 通期判定 ──────────────────────────────────────────────────────────
    is_4q, fy_reason = _is_fy_or_4q(earnings, doc_title)
    if is_4q and quarter not in ("FY", "4Q", "1Q", "2Q", "3Q"):
        quarter = "FY"

    # ── 6. ガイダンス抽出 (FY/4Q のみ) ──────────────────────────────────────
    guidance = None
    if is_4q or quarter in ("FY", "4Q"):
        try:
            from src.events.earnings_guidance_extractor import (
                extract_guidance_from_zip,
                format_guidance_section,
            )
            t0 = time.perf_counter()
            guidance = extract_guidance_from_zip(
                xbrl_path=zip_path_str,
                actual_sales=earnings.sales_current,
                actual_op=earnings.op_current,
            )
            t_sections["extract_guidance_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception:
            t_sections["extract_guidance_ms"] = -1

    # ── 7. 通知メッセージ生成 ─────────────────────────────────────────────────
    # AI API 呼び出しはスキップ。理由テキストは生テキストを最大3文に分割して利用。
    full_message = ""
    try:
        from src.events.summary_notify import format_earnings_message
        t0 = time.perf_counter()

        company_reasons: list[str] = []
        if narrative and narrative.has_reason and narrative.company_reason:
            company_reasons = [
                s.strip() for s in narrative.company_reason.split("。") if s.strip()
            ][:3]

        summary_line = earnings.format_summary_line(clip=2.0)
        segment_lines = earnings.format_segment_lines()

        full_message = format_earnings_message(
            ticker=ticker,
            company_name=company_name,
            summary_line=summary_line,
            segment_lines=segment_lines,
            company_reasons=company_reasons,
            segment_reasons=[],
            title=doc_title,
        )

        if guidance:
            from src.events.earnings_guidance_extractor import format_guidance_section
            gs = format_guidance_section(guidance)
            if gs:
                full_message += "\n\n" + gs

        t_sections["format_message_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as e:
        _log(f"format_earnings_message failed: {e}")
        t_sections["format_message_ms"] = -1

    # ── 8. fingerprint 生成 ───────────────────────────────────────────────────
    # 本番 _compute_earnings_fingerprint と同じロジック
    fp_raw = f"earnings_v2:{ticker}:{doc_title}:{source_doc_id}"
    fingerprint = hashlib.sha256(fp_raw.encode()).hexdigest()[:32]

    # ── 9. primary_metric ────────────────────────────────────────────────────
    primary_metric_name, primary_metric_value, primary_metric_yoy = _build_primary_metric(
        earnings, guidance
    )

    # ── 10. notification_compare_json ────────────────────────────────────────
    notification_compare_json = _build_notification_compare_json(
        earnings, guidance, fiscal_year, quarter
    )

    # ── 11. extracted_payload 組み立て (raw_payload.extracted 相当) ─────────
    extracted_payload = {
        "ticker": ticker,
        "source_doc_id": source_doc_id,
        "xbrl_doc_id": xbrl_doc_id,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "sales_current": earnings.sales_current,
        "sales_label": getattr(earnings, "sales_label", ""),
        "sales_yoy": earnings.sales_yoy,
        "op_current": earnings.op_current,
        "op_label": getattr(earnings, "op_label", ""),
        "op_source": getattr(earnings, "op_source", ""),
        "op_yoy": earnings.op_yoy,
        "has_yoy": earnings.has_yoy,
        "source_url": source_url,
        "xbrl_path": zip_path_str,
        "segments": [
            {"name": s.name, "sales": s.sales_current, "profit": s.profit_current}
            for s in (earnings.segments or [])
        ],
    }

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)

    result = {
        "status": "ok",
        # ── 識別情報 ──
        "zip_path": zip_path_str,
        "zip_filename": zip_filename,
        "ticker": ticker,
        "company_name": company_name,
        "source_doc_id": source_doc_id,
        "xbrl_doc_id": xbrl_doc_id,
        "archive_date": archive_date,
        # ── 数値 ──
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "sales_current": earnings.sales_current,
        "sales_yoy": round(earnings.sales_yoy, 6) if earnings.sales_yoy is not None else None,
        "op_current": earnings.op_current,
        "op_yoy": round(earnings.op_yoy, 6) if earnings.op_yoy is not None else None,
        "primary_metric_name": primary_metric_name,
        "primary_metric_value": primary_metric_value,
        "primary_metric_yoy": round(primary_metric_yoy, 6) if primary_metric_yoy is not None else None,
        "has_yoy": earnings.has_yoy,
        "is_fy_or_4q": is_4q,
        "has_guidance": bool(guidance and guidance.has_guidance),
        "narrative_extracted": narrative is not None,
        # ── メッセージ ──
        "formatted_message": full_message,
        "formatted_message_length": len(full_message),
        "formatted_message_preview": full_message[:120],
        # ── ペイロード ──
        "extracted_payload": extracted_payload,
        "notification_compare_json": notification_compare_json,
        "fingerprint": fingerprint,
        # ── URL ──
        "source_url": source_url,
        "pdf_url": pdf_url,
        # ── デバッグ ──
        "fy_reason": fy_reason,
        "is_short_filename": is_short,
        # ── タイミング ──
        "elapsed_ms": elapsed_ms,
        "section_timings_ms": t_sections,
        # ── エラー情報 ──
        "error_type": "",
        "error_message": "",
    }

    if result.get("status") == "ok" and not str(result.get("quarter") or "").strip():
        result["status"] = "error"
        result["error_type"] = "ambiguous_quarter"
        result["error_message"] = "quarter could not be determined from title"

    return result


# ─── --input-json モード ─────────────────────────────────────────────────────

def _run_input_json_mode() -> None:
    """
    stdin から JSON を読み込み、メタデータを親プロセスから受け取って解析する。

    title は入力 JSON の値を正として使用する。
    SQLite / Supabase / ファイルシステムからの title 推定は行わない。

    エラー時は status="error" の JSON を stdout に出力してプロセス継続。
    クラッシュさせない。
    """
    t_start = time.perf_counter()

    # ── stdin 読み込み ─────────────────────────────────────────────────────
    try:
        raw = sys.stdin.read()
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="stdin_read_error",
            error_message=str(e)[:200],
            elapsed_ms=elapsed_ms,
        )
        _log(f"ERROR: stdin read failed: {e}")
        _emit(result)
        return

    # ── JSON パース ───────────────────────────────────────────────────────
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as e:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="invalid_json",
            error_message=f"JSON decode error: {e.msg} at pos {e.pos}",
            elapsed_ms=elapsed_ms,
        )
        _log(f"ERROR: invalid JSON from stdin: {e}")
        _emit(result)
        return

    if not isinstance(params, dict):
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="invalid_json",
            error_message=f"Expected JSON object, got {type(params).__name__}",
            elapsed_ms=elapsed_ms,
        )
        _log(f"ERROR: expected JSON object, got {type(params).__name__}")
        _emit(result)
        return

    # ── 必須フィールド検証: 存在チェック + 空文字チェック ─────────────────────
    # フィールドが存在しない / None / 空文字 / 空白のみ の場合はすべてエラー。
    # title 空文字は quarter を推定できないため status=error とする。
    missing_fields = [
        f for f in _REQUIRED_FIELDS
        if f not in params
        or params[f] is None
    ]
    empty_fields = [
        f for f in _REQUIRED_FIELDS
        if f not in missing_fields  # 存在はする
        and isinstance(params[f], str)
        and not params[f].strip()   # 空文字 or 空白のみ
    ]

    if missing_fields:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="missing_required_field",
            error_message=f"Missing required fields: {', '.join(missing_fields)}",
            elapsed_ms=elapsed_ms,
            ticker=params.get("ticker", ""),
            company_name=params.get("company_name", ""),
        )
        _log(f"ERROR: missing required fields: {missing_fields}")
        _emit(result)
        return

    if empty_fields:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="invalid_required_field",
            error_message=f"Required fields are empty: {', '.join(empty_fields)}",
            elapsed_ms=elapsed_ms,
            ticker=params.get("ticker", ""),
            company_name=params.get("company_name", ""),
            source_url=params.get("source_url", ""),
            pdf_url=params.get("pdf_url", ""),
            source_doc_id=params.get("source_doc_id", ""),
            archive_date=params.get("archive_date", ""),
        )
        _log(f"ERROR: required fields are empty: {empty_fields}")
        _emit(result)
        return

    title = params["title"]  # ここに来た時点で空文字でない

    # ── ZIP ファイル存在確認 ───────────────────────────────────────────────
    zip_path_str = params["zip_path"]
    zip_path = Path(zip_path_str)
    if not zip_path.exists():
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="file_not_found",
            error_message=f"File not found: {zip_path_str}",
            elapsed_ms=elapsed_ms,
            ticker=params.get("ticker", ""),
            company_name=params.get("company_name", ""),
            zip_path=zip_path_str,
            source_url=params.get("source_url", ""),
            pdf_url=params.get("pdf_url", ""),
            source_doc_id=params.get("source_doc_id", ""),
            archive_date=params.get("archive_date", ""),
        )
        _log(f"ERROR: file not found: {zip_path_str}")
        _emit(result)
        return

    # ── ログ出力 ─────────────────────────────────────────────────────────
    _log(
        f"Parsing --input-json (v{_VERSION}): {zip_path.name} "
        f"ticker={params['ticker']!r} "
        f"title={title[:50]!r} "
        f"archive_date={params.get('archive_date')!r}"
    )

    # ── 解析実行 ──────────────────────────────────────────────────────────
    try:
        result = _run_production_parse(
            zip_path=zip_path,
            doc_title=title,
            doc_ticker=params["ticker"],
            doc_company_name=params.get("company_name", ""),
            disclosed_at=params.get("disclosed_at", ""),
            source_url_override=params.get("source_url", ""),
            pdf_url_override=params.get("pdf_url", ""),
            source_doc_id_override=params.get("source_doc_id", ""),
            archive_date_override=params.get("archive_date", ""),
            xbrl_doc_id_override=params.get("xbrl_doc_id", ""),
            disclosure_id_override=params.get("disclosure_id", ""),
        )
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        result = _make_error_result(
            error_type="unexpected_error",
            error_message=str(e)[:300],
            elapsed_ms=elapsed_ms,
            ticker=params.get("ticker", ""),
            company_name=params.get("company_name", ""),
            zip_path=zip_path_str,
            source_url=params.get("source_url", ""),
            pdf_url=params.get("pdf_url", ""),
            source_doc_id=params.get("source_doc_id", ""),
            archive_date=params.get("archive_date", ""),
        )
        _log(f"ERROR: unexpected error: {e}")

    # ── run_id をそのまま返却 (デバッグ用) ───────────────────────────────
    if "run_id" in params:
        result["run_id"] = params["run_id"]

    _log(
        f"Done: {zip_path.name} in {result.get('elapsed_ms', '?')}ms "
        f"status={result.get('status')} "
        f"ticker={result.get('ticker')} "
        f"quarter={result.get('quarter')} "
        f"msg_len={result.get('formatted_message_length', 0)}"
    )
    _emit(result)


# ─── CLI モード (後方互換) ────────────────────────────────────────────────────

def _run_cli_mode(args: list[str]) -> None:
    """
    既存の CLI 引数モード。後方互換のために維持する。

    Usage: parse_tanshin_worker.py <zip_path> [title] [ticker]
    """
    if not args:
        _log("ERROR: No ZIP path provided.")
        _emit(_make_error_result(
            error_type="no_zip_path",
            error_message="No ZIP path provided",
            elapsed_ms=0,
        ))
        sys.exit(1)

    zip_path_str = args[0]
    zip_path = Path(zip_path_str)

    if not zip_path.exists():
        _log(f"ERROR: File not found: {zip_path_str}")
        _emit(_make_error_result(
            error_type="file_not_found",
            error_message=f"File not found: {zip_path_str}",
            elapsed_ms=0,
            zip_path=zip_path_str,
        ))
        sys.exit(1)

    doc_title = args[1] if len(args) > 1 else ""
    doc_ticker = args[2] if len(args) > 2 else ""

    _log(f"Parsing CLI (v{_VERSION}): {zip_path.name} title={doc_title!r} ticker={doc_ticker!r}")

    try:
        result = _run_production_parse(zip_path, doc_title=doc_title, doc_ticker=doc_ticker)
    except Exception as e:
        result = _make_error_result(
            error_type="unexpected_error",
            error_message=str(e)[:300],
            elapsed_ms=0,
            zip_path=zip_path_str,
        )

    _log(
        f"Done: {zip_path.name} in {result.get('elapsed_ms', '?')}ms "
        f"status={result.get('status')} "
        f"ticker={result.get('ticker')} "
        f"quarter={result.get('quarter')} "
        f"sales={result.get('sales_current')} "
        f"op={result.get('op_current')} "
        f"msg_len={result.get('formatted_message_length', 0)}"
    )
    _emit(result)


# ─── メイン ────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    # ── テストモード ──────────────────────────────────────────────────────────
    if "--hang" in args:
        _log("HANG mode: sleeping 60s to simulate timeout...")
        time.sleep(60)
        _emit({"status": "hang_completed", "note": "should not reach here"})
        return

    if "--corrupt-json" in args:
        _log("CORRUPT-JSON mode: emitting invalid JSON")
        print("{ this is not valid json !!!", flush=True)
        return

    # ── --input-json モード ───────────────────────────────────────────────────
    if "--input-json" in args:
        _run_input_json_mode()
        return

    # ── CLI 引数モード (後方互換) ─────────────────────────────────────────────
    # --input-json 以外のフラグを除いた引数リストをそのまま渡す
    cli_args = [a for a in args if not a.startswith("--")]
    _run_cli_mode(cli_args)


if __name__ == "__main__":
    main()
