# ============================================================
# extractor.py — 数値抽出（XBRL優先 → PDF FB）+ 予想修正抽出
# ============================================================
from __future__ import annotations

import io
import os
import threading
from dataclasses import dataclass, field
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pdfplumber

from .models import ExtractedFinancials, ForecastTarget, OrderMetric, ExtractedOrderMetrics, FINANCIAL_STATEMENT_KEYWORDS
from .utils import normalize_number, parse_scale_unit
from .xbrl_clean import read_xbrl_bytes
from .year_parser import (
    extract_fiscal_info,
    extract_fiscal_year_from_title,
    extract_fiscal_year_from_text,
    extract_all_fiscal_years,
    detect_all_quarters,
    parse_reiwa,
)
from .analysis.header_analysis import normalize_header

logger = logging.getLogger("tdnet")

# ============================================================
# キーワード定義
# ============================================================

SALES_KEYWORDS = [
    "売上高", "売上収益", "営業収益", "経常収益",
    "Net sales", "Revenue", "Net revenue", "Operating revenue",
]

GROSS_PROFIT_KEYWORDS = [
    "売上総利益", "粗利益",
    "Gross profit",
]

OP_KEYWORDS = [
    "営業利益", "営業損失",
    "Operating profit", "Operating income", "Operating loss",
]

# 単位検出パターン
SCALE_PATTERNS = [
    re.compile(r"[（(]単位[：:]?\s*(百万円|億円|千円|円)[）)]"),
    re.compile(r"単位[：:]\s*(百万円|億円|千円|円)"),
    re.compile(r"[（(](百万円|億円|千円)[）)]"),
]

# 予想修正: 行ラベル（優先順位つき）
# 実績値(B) > 修正予想(B)
ACTUAL_B_LABELS = ["実績値（B）", "実績値(B)", "実績（B）", "実績(B)"]
FORECAST_B_LABELS = [
    "今回修正予想（B）", "今回修正予想(B)",
    "修正予想（B）", "修正予想(B)",
    "今回修正予想",
]


# XBRL/iXBRL概念名 → フィールドマッピング
# 従来XBRLのタグ名（jppfs_cor:NetSales → tag_local = "NetSales"）
# iXBRLのname属性（tse-ed-t:NetSales → concept_local = "NetSales"）
_XBRL_TAG_MAP = {
    "NetSales": "sales",
    "Revenue": "sales",
    "OperatingRevenue": "sales",
    "OperatingRevenuesREIT": "sales",        # REIT/投資法人の営業収益
    "OperatingRevenueINV": "sales",          # 投資法人の営業収益
    "SalesIFRS": "sales",
    "RevenueFromContractsWithCustomers": "sales",
    "RevenueIFRS": "sales",
    "NetOperatingRevenueSEC": "sales",
    "OperatingRevenueSEC": "sales",
    "GrossProfit": "gross_profit",
    "GrossProfitIFRS": "gross_profit",
    "SellingGeneralAndAdministrativeExpenses": "selling_general_and_administrative_expenses",
    "SellingGeneralAndAdministrativeExpensesIFRS": "selling_general_and_administrative_expenses",
    "CostOfSales": "cost_of_sales",          # 売上原価（計算補完用）
    "OperatingIncome": "operating_profit",
    "OperatingProfit": "operating_profit",
    "OperatingIncomeIFRS": "operating_profit",
    "OrdinaryIncome": "operating_profit",    # 経常利益（営業利益なし時のFB）
}


# ZIPまたはiXBRLファイルの拡張子パターン（優先順）
_IXBRL_EXTENSIONS = ("-ixbrl.htm", ".ixbrl.htm", "-ixbrl.html", ".ixbrl.html", ".ixbrl")
_XBRL_EXTENSIONS = (".xbrl",)

# ZIPシグネチャ (PK\x03\x04)
_ZIP_SIGNATURE = b"PK\x03\x04"

# contextRefで当期累計を判定するキーワード
# InterimDuration: 中間期（2Q/半期決算）のAttachment PLで使用される
_CURRENT_DURATION_KEYWORDS = ("CurrentYearDuration", "CurrentYTD", "CurrentAccumulatedQ", "InterimDuration")


def _find_xbrl_in_zip(zf: zipfile.ZipFile) -> list[str]:
    """
    ZIP内のiXBRL/XBRLファイルを優先順位付きで返す。
    優先: iXBRL > XBRL
    """
    ixbrl_files = []
    xbrl_files = []

    for name in zf.namelist():
        lower = name.lower()
        if any(lower.endswith(ext) for ext in _IXBRL_EXTENSIONS):
            ixbrl_files.append(name)
        elif any(lower.endswith(ext) for ext in _XBRL_EXTENSIONS):
            xbrl_files.append(name)

    return ixbrl_files + xbrl_files


def _is_current_duration(context_ref: str) -> bool:
    """contextRefが当期累計を指すかを判定（部分一致）"""
    lower_ref = context_ref.lower()
    if "prior" in lower_ref or "previous" in lower_ref or "前" in lower_ref:
        return False
    return any(kw in context_ref for kw in _CURRENT_DURATION_KEYWORDS)


def _is_consolidated_preferred(context_ref: str) -> bool:
    """連結かどうか（連結優先）"""
    return "Consolidated" in context_ref and "NonConsolidated" not in context_ref


def _detect_quarter_from_context(context_ref: str) -> str:
    """
    contextRefからQ情報を検出する。

    例:
    - CurrentYearDuration_ThirdQuarterMember_... → '3Q'
    - CurrentYearDuration_ConsolidatedMember_ResultMember → '4Q'（四半期指定なし=通期）
    - CurrentYearDuration_AnnualMember_... → '4Q'
    """
    if "FirstQuarterMember" in context_ref:
        return "1Q"
    if "SecondQuarterMember" in context_ref:
        return "2Q"
    if "ThirdQuarterMember" in context_ref:
        return "3Q"
    if "YearEndMember" in context_ref or "AnnualMember" in context_ref:
        return "4Q"
    # 四半期指定なし + CurrentYearDuration → 確定不可（タイトルに委ねる）
    # ※ 10月決算1Qでも CurrentYearDuration が来るため 4Q 確定にしない
    return ""


def _apply_ixbrl_scale(raw_text: str, scale: str, sign: str) -> int | None:
    """
    iXBRLのscale/sign属性を適用して整数に変換。

    scale='6' → ×10^6, scale='0' → ×1
    sign='-' → 負数
    """
    val = normalize_number(raw_text)
    if val is None:
        return None

    if scale:
        try:
            val = int(val * (10 ** int(scale)))
        except (ValueError, OverflowError):
            pass

    if sign == "-" and val > 0:
        val = -val

    return val


def _parse_xbrl_content(raw: bytes, source_label: str = "xbrl") -> ExtractedFinancials | None:
    """
    単一のXBRL/iXBRLバイト列をパースして数値を抽出する。

    二段構成:
    1. 従来XBRLモード: タグ名からマッピング（jppfs_cor:NetSales等）
    2. iXBRLモード: ix:nonFraction の name 属性からマッピング

    Args:
        raw: iXBRL/XBRLファイルの生バイト列

    Returns:
        ExtractedFinancials or None
    """
    xml_str = read_xbrl_bytes(raw)
    root = ET.fromstring(xml_str)

    values: dict[str, int | None] = {
        "sales": None,
        "gross_profit": None,
        "selling_general_and_administrative_expenses": None,
        "operating_profit": None,
        "cost_of_sales": None,
    }
    value_priority: dict[str, bool] = {}

    # --- パス1: 従来XBRLモード（タグ名直接マッチ）---
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag

        if tag_local in _XBRL_TAG_MAP:
            field_name = _XBRL_TAG_MAP[tag_local]
            context = elem.get("contextRef", "")
            if _is_current_duration(context):
                val = normalize_number(elem.text or "")
                if val is not None:
                    is_consol = _is_consolidated_preferred(context)
                    if values[field_name] is None or (is_consol and not value_priority.get(field_name)):
                        values[field_name] = val
                        value_priority[field_name] = is_consol

    if values["sales"] is not None:
        sources = {k: source_label for k, v in values.items() if v is not None and k != "cost_of_sales"}
        if values["cost_of_sales"] is not None:
            sources["cost_of_sales"] = source_label
        result = ExtractedFinancials(
            sales=values["sales"],
            gross_profit=values["gross_profit"],
            selling_general_and_administrative_expenses=values["selling_general_and_administrative_expenses"],
            operating_profit=values["operating_profit"],
            source_unit="円",
            confidence="high",
            field_sources=sources,
        )
        result.cost_of_sales = values["cost_of_sales"]
        return result

    # --- パス2: iXBRLモード（ix:nonFraction の name 属性）---
    values = {"sales": None, "gross_profit": None, "selling_general_and_administrative_expenses": None, "operating_profit": None, "cost_of_sales": None}
    value_priority = {}
    detected_unit = "円"
    unknown_tags: set[str] = set()
    sales_context = ""  # 売上のcontextRef（Q検出用）

    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag

        if tag_local != "nonFraction":
            continue

        concept_name = elem.get("name", "")
        context = elem.get("contextRef", "")
        scale = elem.get("scale", "")
        sign = elem.get("sign", "")

        if not concept_name or not context:
            continue

        concept_local = concept_name.split(":")[-1] if ":" in concept_name else concept_name

        if concept_local not in _XBRL_TAG_MAP:
            if any(kw in concept_local.lower() for kw in [
                "sales", "revenue", "profit", "income", "loss", "operating", "gross",
            ]):
                unknown_tags.add(concept_name)
            continue

        field_name = _XBRL_TAG_MAP[concept_local]

        if not _is_current_duration(context):
            continue
        if "ForecastMember" in context or "LowerMember" in context or "UpperMember" in context:
            continue

        text = (elem.text or "").strip()
        if not text:
            text = "".join(elem.itertext()).strip()
        if not text:
            continue

        val = _apply_ixbrl_scale(text, scale, sign)
        if val is None:
            continue

        if scale:
            try:
                s = int(scale)
                if s >= 6:
                    detected_unit = "円"
                elif s >= 3:
                    detected_unit = "円"
            except ValueError:
                pass

        is_consol = _is_consolidated_preferred(context)
        if values[field_name] is None or (is_consol and not value_priority.get(field_name)):
            values[field_name] = val
            value_priority[field_name] = is_consol
            if field_name == "sales":
                sales_context = context

    if unknown_tags:
        logger.info(f"[XBRL] 未知の財務タグ検出: {sorted(unknown_tags)}")

    if values["sales"] is None:
        return None

    # contextRefからQ情報を検出
    detected_quarter = _detect_quarter_from_context(sales_context)

    sources = {k: source_label for k, v in values.items() if v is not None and k != "cost_of_sales"}
    if values["cost_of_sales"] is not None:
        sources["cost_of_sales"] = source_label
    result = ExtractedFinancials(
        sales=values["sales"],
        gross_profit=values["gross_profit"],
        operating_profit=values["operating_profit"],
        source_unit=detected_unit,
        confidence="high",
        field_sources=sources,
    )
    result.cost_of_sales = values["cost_of_sales"]
    # iXBRLから検出したqを設定（呼び出し側で上書き可能）
    if detected_quarter:
        result.quarter = detected_quarter

    return result


def _is_summary_file(name: str) -> bool:
    """ファイル名がSummaryかどうかを判定"""
    lower = name.lower()
    return "/summary/" in lower or "summary" in os.path.basename(lower)


def _is_pl_attachment(name: str) -> bool:
    """ファイル名がPL Attachmentかどうかを判定"""
    lower = name.lower()
    basename = os.path.basename(lower)
    return "/attachment/" in lower and ("pl" in basename or "qnpl" in basename or "scpl" in basename or "qcpl" in basename)


def _extract_from_xbrl(xbrl_path: str) -> ExtractedFinancials | None:
    """XBRLファイル（ZIP or 単体）から決算数値を抽出する。
    
    ZIP内にSummary + Attachment/PLがある場合:
    1. Summaryを最優先で抽出（sales/operating_profit）
    2. Summary成功後に不足項目（gross_profit/cost_of_sales）があれば
       Attachment/PLを追加探索して補完
    3. field_sourcesに summary_xbrl / attachment_xbrl を項目ごとに記録
    """
    try:
        raw = Path(xbrl_path).read_bytes()

        # --- ZIPファイルの場合: 展開して内部ファイルをパース ---
        if raw[:4] == _ZIP_SIGNATURE:
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw), "r")
            except zipfile.BadZipFile as e:
                logger.warning(f"[XBRL] 不正なZIPファイル: {xbrl_path} - {e}")
                return None

            candidates = _find_xbrl_in_zip(zf)
            if not candidates:
                logger.warning(f"[XBRL] ZIP内にXBRL/iXBRLファイルなし: {xbrl_path}")
                zf.close()
                return None

            logger.debug(f"[XBRL] ZIP内候補: {candidates}")

            # --- Phase 1: Summary ファイルを優先抽出 ---
            summary_result: ExtractedFinancials | None = None
            summary_candidates = [c for c in candidates if _is_summary_file(c)]
            other_candidates = [c for c in candidates if not _is_summary_file(c)]

            for entry_name in summary_candidates:
                try:
                    entry_bytes = zf.read(entry_name)
                    result = _parse_xbrl_content(entry_bytes, source_label="summary_xbrl")
                    if result is not None:
                        logger.info(f"[XBRL] Summary抽出成功: {entry_name}")
                        summary_result = result
                        break
                except Exception as e:
                    logger.debug(f"[XBRL] Summary解析失敗: {entry_name}: {e}")

            # Summary で取れなかった場合、全候補を順に試行
            if summary_result is None:
                for entry_name in other_candidates:
                    try:
                        entry_bytes = zf.read(entry_name)
                        result = _parse_xbrl_content(entry_bytes, source_label="xbrl")
                        if result is not None:
                            logger.info(f"[XBRL] ZIP内ファイルから抽出成功: {entry_name}")
                            zf.close()
                            return result
                    except Exception as e:
                        logger.debug(f"[XBRL] ZIP内ファイル解析失敗: {entry_name}: {e}")

                zf.close()
                logger.warning(f"[XBRL] ZIP内の全候補で抽出失敗: {xbrl_path}")
                return None

            # --- Phase 2: Summary 成功後、不足項目を Attachment/PL から補完 ---
            missing_fields = []
            if summary_result.gross_profit is None:
                missing_fields.append("gross_profit")
            if getattr(summary_result, "selling_general_and_administrative_expenses", None) is None:
                missing_fields.append("selling_general_and_administrative_expenses")
            if not hasattr(summary_result, "cost_of_sales") or getattr(summary_result, "cost_of_sales", None) is None:
                missing_fields.append("cost_of_sales")
            if summary_result.sales is None:
                missing_fields.append("sales")
            if summary_result.operating_profit is None:
                missing_fields.append("operating_profit")

            if missing_fields:
                pl_candidates = [c for c in candidates if _is_pl_attachment(c)]
                logger.info(f"[XBRL] Summary不足項目: {missing_fields}, PL Attachment候補: {len(pl_candidates)}")

                for entry_name in pl_candidates:
                    try:
                        entry_bytes = zf.read(entry_name)
                        pl_result = _parse_xbrl_content(entry_bytes, source_label="attachment_xbrl")
                        if pl_result is None:
                            continue
                        logger.info(f"[XBRL] PL Attachment抽出成功: {entry_name}")

                        # 不足項目だけを補完（既存値は上書きしない）
                        if "gross_profit" in missing_fields and pl_result.gross_profit is not None:
                            summary_result.gross_profit = pl_result.gross_profit
                            summary_result.field_sources["gross_profit"] = "attachment_xbrl"
                            logger.info(f"[XBRL] Attachment補完: gross_profit={pl_result.gross_profit}")

                        if "selling_general_and_administrative_expenses" in missing_fields and pl_result.selling_general_and_administrative_expenses is not None:
                            summary_result.selling_general_and_administrative_expenses = pl_result.selling_general_and_administrative_expenses
                            summary_result.field_sources["selling_general_and_administrative_expenses"] = "attachment_xbrl"
                            logger.info(f"[XBRL] Attachment補完: sga={pl_result.selling_general_and_administrative_expenses}")

                        if "cost_of_sales" in missing_fields:
                            pl_cos = getattr(pl_result, "cost_of_sales", None)
                            if pl_cos is not None:
                                summary_result.cost_of_sales = pl_cos
                                summary_result.field_sources["cost_of_sales"] = "attachment_xbrl"
                                logger.info(f"[XBRL] Attachment補完: cost_of_sales={pl_cos}")

                        if "sales" in missing_fields and pl_result.sales is not None:
                            summary_result.sales = pl_result.sales
                            summary_result.field_sources["sales"] = "attachment_xbrl"
                            logger.info(f"[XBRL] Attachment補完: sales={pl_result.sales}")

                        if "operating_profit" in missing_fields and pl_result.operating_profit is not None:
                            summary_result.operating_profit = pl_result.operating_profit
                            summary_result.field_sources["operating_profit"] = "attachment_xbrl"
                            logger.info(f"[XBRL] Attachment補完: operating_profit={pl_result.operating_profit}")

                        break  # 最初に成功したPL Attachmentで補完完了
                    except Exception as e:
                        logger.debug(f"[XBRL] PL Attachment解析失敗: {entry_name}: {e}")

            zf.close()
            return summary_result

        # --- 単体XBRLファイルの場合: 従来通り ---
        return _parse_xbrl_content(raw)

    except Exception as e:
        logger.warning(f"[XBRL] 抽出失敗: {e}")
        return None


# ============================================================
# PDF抽出（フォールバック）— 決算短信用
# ============================================================

def _detect_scale(text: str) -> str:
    """テキストから単位を検出"""
    for pattern in SCALE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    # デフォルトは百万円（決算短信は大半が百万円）
    return "百万円"


def _extract_value_near_keyword(
    lines: list[str],
    keywords: list[str],
) -> int | None:
    """
    キーワードを含む行から数値を抽出する。
    キーワード行の同じ行 → 次の行 の順で探索。
    """
    for i, line in enumerate(lines):
        matched_kw = None
        for kw in keywords:
            if kw in line:
                matched_kw = kw
                break

        if matched_kw is None:
            continue

        # 同じ行の数値を探す
        nums = _extract_numbers_from_line(line, matched_kw)
        if nums:
            return nums[0]

        # 次の行を確認
        if i + 1 < len(lines):
            nums = _extract_numbers_from_line(lines[i + 1])
            if nums:
                return nums[0]

    return None


def _extract_numbers_from_line(line: str, exclude_keyword: str | None = None) -> list[int]:
    """行から数値を抽出する（小さすぎるパーセンテージ値を除外）"""
    # キーワード部分を除去
    text = line
    if exclude_keyword:
        text = text.replace(exclude_keyword, "")

    # YoY%表記を除去
    text = re.sub(r"[+\-－]?\d{1,3}\.\d%?", "", text)

    # 数値パターン抽出
    # △▲付き、カンマ区切り、マイナス付きに対応
    pattern = re.compile(r"[△▲\-－]?\d[\d,]*(?:\.\d+)?")
    matches = pattern.findall(text)

    results: list[int] = []
    for raw in matches:
        val = normalize_number(raw)
        if val is not None and abs(val) >= 100:  # 100未満は除外（YoY%等）
            results.append(val)

    return results


def _ocr_enabled() -> bool:
    """OCRフォールバックが有効かどうかを環境変数で判定"""
    return os.environ.get("PDF_OCR_ENABLED", "0") == "1"


def _ocr_force_test() -> bool:
    """[一時検証用] OCR経路を強制発火させるフラグ。検証後に削除すること。"""
    return os.environ.get("PDF_OCR_FORCE_TEST", "0") == "1"


def _run_ocr_pipeline(pdf_path: str) -> str | None:
    """
    OCRパイプライン: Ghostscript→Vision API→テキスト結合。
    失敗時はNoneを返す（例外を投げない）。
    """
    try:
        from .pdf.ocr.ghostscript_render import render_pdf_to_images
        from .pdf.ocr.google_vision_ocr import GoogleVisionOcr
        from .pdf.ocr.base import OcrError

        images = render_pdf_to_images(pdf_path)
        if not images:
            logger.info("[OCR] Ghostscript: 0 pages rendered")
            return None

        ocr = GoogleVisionOcr()
        texts: list[str] = []
        for i, img in enumerate(images):
            try:
                page_result = ocr.ocr_image(img)
                page_result.page_number = i + 1
                if page_result.full_text:
                    texts.append(page_result.full_text)
            except OcrError as e:
                logger.warning(f"[OCR] page {i+1} failed: {e}")

        if not texts:
            logger.info("[OCR] No text extracted from any page")
            return None

        combined = "\n".join(texts)
        logger.info(f"[OCR] Total text length: {len(combined)}")
        return combined

    except Exception as e:
        logger.warning(f"[OCR] Pipeline error: {e}")
        return None



def _extract_segments_from_ocr(ocr_text: str) -> list[SegmentExtracted]:
    """OCRテキストからセグメントを抽出してSegmentExtractedリストに変換"""
    try:
        from .pdf.ocr.segment_extractor_ocr import extract_segments_from_ocr_text
        result = extract_segments_from_ocr_text(ocr_text)
        if not result.success or not result.segments:
            return []
        return [
            SegmentExtracted(
                segment_name=s.segment_name,
                segment_order=s.segment_order,
                segment_sales=s.segment_sales,
                segment_profit=s.segment_profit,
                raw_profit_label="ocr",
                raw_text=f"[ocr] {s.segment_name}",
            )
            for s in result.segments
        ]
    except Exception as e:
        logger.warning(f"[segment-ocr] extraction error: {e}")
        return []


def _extract_order_metrics_from_ocr(
    ocr_text: str,
) -> tuple[ExtractedOrderMetrics | None, str]:
    """OCRテキストから受注メトリクスを抽出してExtractedOrderMetricsに変換"""
    try:
        from .pdf.ocr.order_extractor_ocr import extract_orders_from_ocr_text
        result = extract_orders_from_ocr_text(ocr_text)
        if not result.success or not result.metrics:
            return None, f"ocr: {result.reason}"
        metrics = [
            OrderMetric(
                metric_name=m.metric_name,
                value=m.value,
                raw_value=m.raw_value,
                unit=m.unit,
                confidence="low",
                raw_text=m.raw_text,
            )
            for m in result.metrics
        ]
        return ExtractedOrderMetrics(
            metrics=metrics,
            source_unit=result.metrics[0].unit,
        ), ""
    except Exception as e:
        logger.warning(f"[order-ocr] extraction error: {e}")
        return None, f"ocr_error: {e}"


def _extract_from_pdf(pdf_path: str) -> tuple[ExtractedFinancials | None, str]:
    """PDFファイルから決算数値を抽出する（OCRフォールバック付き）"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # 最初の3ページからテキスト抽出
            text = ""
            for page in pdf.pages[:3]:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        # ── ① native textが空 → OCRでテキスト再取得 ──
        ocr_used = False
        # [一時検証用] 強制テスト: native textを空扱いにしてOCR経路を通す
        if _ocr_force_test() and text.strip():
            logger.info("[pdf-ocr] FORCE_TEST: native text suppressed to trigger OCR")
            text = ""
        if not text.strip():
            if _ocr_enabled():
                logger.info("[OCR] Native text empty, trying OCR fallback")
                ocr_text = _run_ocr_pipeline(pdf_path)
                if ocr_text and ocr_text.strip():
                    text = ocr_text
                    ocr_used = True
                    logger.info("[OCR] OCR text acquired, re-running extraction")
                else:
                    return None, "テキスト抽出不可（画像PDF？）— OCR also failed"
            else:
                return None, "テキスト抽出不可（画像PDF？）"

        lines = text.split("\n")

        # 単位検出
        scale_str = _detect_scale(text)
        scale_multiplier = parse_scale_unit(scale_str)

        # 各項目の抽出
        sales = _extract_value_near_keyword(lines, SALES_KEYWORDS)
        gross_profit = _extract_value_near_keyword(lines, GROSS_PROFIT_KEYWORDS)
        operating_profit = _extract_value_near_keyword(lines, OP_KEYWORDS)

        # ── ② native数値抽出失敗 → OCRテキストで再抽出 ──
        if sales is None and operating_profit is None and not ocr_used:
            if _ocr_enabled():
                logger.info("[OCR] Native extraction failed, trying OCR fallback")
                ocr_text = _run_ocr_pipeline(pdf_path)
                if ocr_text and ocr_text.strip():
                    ocr_lines = ocr_text.split("\n")
                    ocr_scale_str = _detect_scale(ocr_text)
                    ocr_sales = _extract_value_near_keyword(ocr_lines, SALES_KEYWORDS)
                    ocr_gp = _extract_value_near_keyword(ocr_lines, GROSS_PROFIT_KEYWORDS)
                    ocr_op = _extract_value_near_keyword(ocr_lines, OP_KEYWORDS)

                    if ocr_sales is not None or ocr_op is not None:
                        sales = ocr_sales
                        gross_profit = ocr_gp
                        operating_profit = ocr_op
                        scale_str = ocr_scale_str
                        ocr_used = True
                        logger.info(
                            f"[OCR] OCR extraction success: "
                            f"sales={sales}, gp={gross_profit}, op={operating_profit}"
                        )

        if sales is None and operating_profit is None:
            suffix = " (OCR also failed)" if ocr_used else ""
            return None, f"売上高・営業利益ともに抽出できず{suffix}"

        source_label = "pdf_ocr" if ocr_used else "pdf"
        sources = {}
        if sales is not None: sources["sales"] = source_label
        if gross_profit is not None: sources["gross_profit"] = source_label
        if operating_profit is not None: sources["operating_profit"] = source_label
        return ExtractedFinancials(
            sales=sales,
            gross_profit=gross_profit,
            operating_profit=operating_profit,
            source_unit=scale_str,
            confidence="low" if ocr_used else "medium",
            field_sources=sources,
        ), ""

    except Exception as e:
        return None, f"PDF解析エラー: {e}"


# ============================================================
# 予想修正・差異 数値抽出（複数ターゲット対応）
# ============================================================

def _find_label_row(lines: list[str], labels: list[str]) -> tuple[int, str] | None:
    """
    行リストからラベルに部分一致する行を探す。
    Returns: (行index, マッチしたラベル) or None
    """
    for i, line in enumerate(lines):
        for label in labels:
            if label in line:
                return (i, label)
    return None


def _extract_table_values(
    lines: list[str],
    label_row_idx: int,
    label: str,
) -> dict[str, int | None]:
    """
    ラベル行から売上高・営業利益の値を取得する。
    ラベル行の数値列、またはラベル行に対するテーブルの同行数値を解析。

    Returns: {"sales": ..., "operating_profit": ..., "gross_profit": ...}
    """
    result: dict[str, int | None] = {
        "sales": None,
        "operating_profit": None,
        "gross_profit": None,
    }

    # ラベル行自体からすべての数値を取得
    label_line = lines[label_row_idx]
    nums_on_label = _extract_numbers_from_line(label_line, label)

    if len(nums_on_label) >= 2:
        # テーブル形式: ラベル 売上高 営業利益 (2つ以上の数値)
        result["sales"] = nums_on_label[0]
        result["operating_profit"] = nums_on_label[1]
        if len(nums_on_label) >= 3:
            result["gross_profit"] = nums_on_label[2]
        return result

    if len(nums_on_label) == 1:
        result["sales"] = nums_on_label[0]

    # ラベル行の直下行にも数値がある場合
    if label_row_idx + 1 < len(lines):
        next_nums = _extract_numbers_from_line(lines[label_row_idx + 1])
        if next_nums and result["operating_profit"] is None:
            result["operating_profit"] = next_nums[0]

    return result


def _detect_block_quarter(block_lines: list[str], title: str) -> str | None:
    """
    テーブルブロック付近のテキストからQ情報を検出する。
    ブロック見出し → タイトル の順で探索。
    """
    # ブロック見出しから検出
    block_text = "\n".join(block_lines)
    quarters = detect_all_quarters(block_text)
    if quarters:
        return quarters[0]

    # タイトルからフォールバック
    quarters = detect_all_quarters(title)
    if quarters:
        return quarters[0]

    return None


def _detect_block_fy(block_lines: list[str], title: str) -> str | None:
    """
    テーブルブロック付近のテキストから年度情報を検出する。
    ブロック見出し → タイトル の順で探索。
    """
    block_text = "\n".join(block_lines)
    fys = extract_all_fiscal_years(block_text)
    if fys:
        return fys[0]

    fy = extract_fiscal_year_from_title(title)
    if fy:
        return fy

    return None


def _split_into_blocks(lines: list[str]) -> list[list[str]]:
    """
    テキスト行をテーブルブロックに分割する。
    空行 or 見出しっぽい行（「．」「.」「①②」など含まない長い行）で分割。
    """
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        # 空行でブロック分割
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue

        # 年度表記を含む見出し行でブロック分割
        if re.search(r"(20\d{2}|令和\d+)年\d+月期", stripped) and len(current) > 3:
            blocks.append(current)
            current = [line]
            continue

        current.append(line)

    if current:
        blocks.append(current)

    return blocks


def extract_forecast_targets(
    pdf_path: str,
    title: str,
) -> list[ForecastTarget]:
    """
    予想修正・差異開示のPDFから複数ターゲットを抽出する。

    Returns:
        list[ForecastTarget] — 年度・Qごとの抽出結果リスト
        複数年度が検出された場合、最小期末年のみを対象とし未来年度はスキップ。
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages[:5]:  # 最大5ページ
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        if not full_text.strip():
            logger.error("[ERROR] forecast_parse_failed reason=テキスト抽出不可（画像PDF？）")
            return []

    except Exception as e:
        logger.error(f"[ERROR] forecast_parse_failed reason=PDF読み込みエラー: {e}")
        return []

    lines = full_text.split("\n")

    # 単位検出
    scale_str = _detect_scale(full_text)

    # テキスト全体から年度・Qを検出
    all_fys = extract_all_fiscal_years(full_text + "\n" + title)
    all_qs = detect_all_quarters(full_text + "\n" + title)

    # タイトルからも年度・Q補完
    title_fy = extract_fiscal_year_from_title(title)
    if title_fy and title_fy not in all_fys:
        all_fys.insert(0, title_fy)

    title_qs = detect_all_quarters(title)
    for q in title_qs:
        if q not in all_qs:
            all_qs.append(q)

    logger.info(f"[抽出] 検出年度={all_fys}, 検出Q={all_qs}, 単位={scale_str}")

    # ── 複数年度フィルタ: 最小期末年のみ対象 ──
    chosen_fy: str | None = None
    skipped_fys: list[str] = []

    if all_fys:
        # 期末年で比較（R8/3 → 2026）
        fy_with_ad: list[tuple[str, int]] = []
        for fy in all_fys:
            parsed = parse_reiwa(fy)
            if parsed:
                fy_with_ad.append((fy, parsed[0]))

        if fy_with_ad:
            fy_with_ad.sort(key=lambda x: x[1])
            chosen_fy = fy_with_ad[0][0]
            chosen_ad = fy_with_ad[0][1]
            skipped_fys = [fy for fy, ad in fy_with_ad[1:] if ad > chosen_ad]

            if len(fy_with_ad) > 1:
                detected_ads = [ad for _, ad in fy_with_ad]
                skipped_ads = [ad for _, ad in fy_with_ad[1:] if ad > chosen_ad]
                logger.info(
                    f"[抽出] detected_fys={detected_ads}, chosen_fy={chosen_ad}, "
                    f"skipped_future_fys={skipped_ads}"
                )
        else:
            chosen_fy = all_fys[0]
    else:
        logger.warning("[抽出] 年度を検出できませんでした")

    # ── テーブルブロック分割 ──
    blocks = _split_into_blocks(lines)

    targets: list[ForecastTarget] = []
    processed_qs: set[str] = set()

    for block in blocks:
        block_fy = _detect_block_fy(block, title)
        block_q = _detect_block_quarter(block, title)

        # 年度がない場合はchosen_fyを使用
        if block_fy is None:
            block_fy = chosen_fy

        # 未来年度スキップ
        if block_fy and chosen_fy and block_fy != chosen_fy:
            if block_fy in skipped_fys:
                logger.info(f"[抽出] 未来年度スキップ: {block_fy} {block_q}")
                continue

        # 実績値(B) 探索（優先）
        actual_match = _find_label_row(block, ACTUAL_B_LABELS)
        forecast_match = _find_label_row(block, FORECAST_B_LABELS)

        if actual_match is None and forecast_match is None:
            continue

        # 実績値B優先、なければ修正予想B
        if actual_match:
            row_idx, label = actual_match
            source = "actualB"
        else:
            row_idx, label = forecast_match  # type: ignore
            source = "forecastB"

        values = _extract_table_values(block, row_idx, label)

        if values["sales"] is None and values["operating_profit"] is None:
            logger.warning(
                f"[抽出] ラベル '{label}' の行から数値を抽出できませんでした"
            )
            continue

        # Q判定: ブロックから → 既知のQ一覧から
        if block_q is None and all_qs:
            # まだ処理していないQがあればそれを使用
            for q in all_qs:
                if q not in processed_qs:
                    block_q = q
                    break

        if block_q is None:
            logger.warning(f"[抽出] Qを特定できません (fy={block_fy})")
            continue

        # 同一Q重複チェック（実績B優先）
        q_key = f"{block_fy}_{block_q}"
        if q_key in processed_qs:
            # 既に実績Bがあれば上書きしない
            existing = [t for t in targets if t.fiscal_year == block_fy and t.quarter == block_q]
            if existing and existing[0].source == "actualB":
                continue
            # 修正予想 → 実績Bに置き換え
            if source == "actualB":
                targets = [t for t in targets if not (t.fiscal_year == block_fy and t.quarter == block_q)]
            else:
                continue

        processed_qs.add(q_key)
        targets.append(ForecastTarget(
            fiscal_year=block_fy or "",
            quarter=block_q,
            sales=values["sales"],
            operating_profit=values["operating_profit"],
            gross_profit=values.get("gross_profit"),
            source=source,
            source_unit=scale_str,
        ))

    # ログ
    if targets:
        target_dicts = [
            {
                "fy": t.fiscal_year, "q": t.quarter,
                "sales": t.sales, "op": t.operating_profit,
                "source": t.source,
            }
            for t in targets
        ]
        logger.info(f"[INFO] targets_detected: {target_dicts}")
    else:
        logger.error(
            f"[ERROR] forecast_parse_failed reason=ターゲットを1つも抽出できませんでした"
        )

    skipped_count = len(skipped_fys)
    if skipped_count > 0:
        logger.info(f"[抽出] skipped_targets_future_fy_count={skipped_count}")

    return targets


# ============================================================
# メインエクスポート（決算短信用）
# ============================================================

# PDFフォールバック除外キーワード（タイトルに含まれていたらPDF抽出をスキップ）
_PDF_SKIP_KEYWORDS = [
    # 説明資料系
    "説明会", "説明資料", "プレゼン", "スライド",
    "presentation",
    # 質疑応答系
    "質疑", "Q&A", "Ｑ＆Ａ", "q&a",
    # 再掲載・補足系
    "再掲載", "再掲", "再度掲載", "(再)", "（再）",
    "補足", "参考資料",
    # IR資料系
    "IR資料", "ＩＲ資料",
]


def _is_tanshin_title(title: str) -> bool:
    """
    タイトルが決算短信（PDFフォールバック対象）かを判定する。

    ポジティブ条件: FINANCIAL_STATEMENT_KEYWORDS のいずれかを含む
    ネガティブ条件: 説明資料・質疑応答・再掲載等のキーワードを含む

    Returns:
        True: PDFフォールバック対象（決算短信）
        False: PDFフォールバック対象外（スキップ）
    """
    if not title:
        return False

    # ネガティブ条件: 除外キーワードを含む場合はスキップ
    title_lower = title.lower()
    for kw in _PDF_SKIP_KEYWORDS:
        if kw.lower() in title_lower:
            return False

    # ポジティブ条件: 共通定数キーワードのいずれかを含む
    for kw in FINANCIAL_STATEMENT_KEYWORDS:
        if kw in title:
            return True

    return False


def extract_financials(
    doc_path: str,
    title: str,
    xbrl_path: str | None = None,
) -> tuple[ExtractedFinancials | None, str]:
    """
    開示書類から決算数値を抽出する。

    XBRL → PDF の優先順位で抽出を試みる。
    PDFフォールバックはタイトルが「決算短信」の場合のみ。

    Args:
        doc_path: PDFファイルパス
        title: 開示タイトル
        xbrl_path: XBRLファイルパス（あれば）

    Returns:
        (ExtractedFinancials or None, エラーメッセージ)
    """
    # 年度・四半期の抽出
    result_text = ""
    try:
        with open(doc_path, "rb") as f:
            # PDFの先頭テキストから年度情報を取る（pdfplumberを使用）
            pdf = pdfplumber.open(doc_path)
            first_page_text = ""
            if pdf.pages:
                first_page_text = pdf.pages[0].extract_text() or ""
            pdf.close()
            result_text = first_page_text
    except Exception:
        pass

    fiscal_year, quarter = extract_fiscal_info(title, result_text)

    # ルート1: XBRL（常に試行）
    if xbrl_path and Path(xbrl_path).exists():
        logger.info(f"[抽出] XBRL抽出を試行: {xbrl_path}")
        result = _extract_from_xbrl(xbrl_path)
        if result is not None:
            # 年度: iXBRL由来があればそちら優先、なければtitle/PDF
            result.fiscal_year = result.fiscal_year or fiscal_year or ""
            # quarter: タイトル由来を最優先（iXBRLは確定性が低い）
            if quarter:
                result.quarter = quarter
            # iXBRL由来のみの場合はフォールバック
            result.quarter = result.quarter or ""
            logger.info(f"[抽出] XBRL抽出成功: sales={result.sales}, gp={result.gross_profit}, op={result.operating_profit}")

            # === per-field PDF 補完 ===
            _PL_FIELDS = ("sales", "gross_profit", "operating_profit")
            missing = [f for f in _PL_FIELDS if getattr(result, f) is None]
            if missing and _is_tanshin_title(title) and Path(doc_path).exists():
                logger.info(f"[補完] XBRL欠損フィールド={missing}, PDF補完を試行")
                pdf_result, pdf_err = _extract_from_pdf(doc_path)
                if pdf_result is not None:
                    for f in missing:
                        pdf_val = getattr(pdf_result, f)
                        if pdf_val is None:
                            continue
                        # --- validation ---
                        # gp <= sales チェック
                        if f == "gross_profit":
                            ref_sales = result.sales if result.sales is not None else (
                                pdf_result.sales if pdf_result.sales is not None else None
                            )
                            if ref_sales is not None and abs(pdf_val) > abs(ref_sales):
                                logger.warning(
                                    f"[補完] QUARANTINE: {f}={pdf_val} > sales={ref_sales}, PDF値を拒否"
                                )
                                continue
                        # 単位一致チェック (XBRL が百万円なら PDF も百万円であるべき)
                        # ※ XBRL と PDF は独立に単位を検出するので値域で判断
                        setattr(result, f, pdf_val)
                        result.field_sources[f] = "pdf_fallback"
                        logger.info(f"[補完] {f}={pdf_val} from PDF (source_unit={pdf_result.source_unit})")
                else:
                    logger.info(f"[補完] PDF抽出失敗: {pdf_err}")

            return result, ""

    # ルート2: PDF（決算短信タイトルの場合のみ）
    if Path(doc_path).exists():
        if not _is_tanshin_title(title):
            reason = f"SKIP_PDF_NOT_TANSHIN: title={title[:40]}"
            logger.info(f"[抽出] {reason}")
            return None, reason

        logger.info(f"[抽出] PDF抽出を試行: {doc_path}")
        result, error = _extract_from_pdf(doc_path)
        if result is not None:
            result.fiscal_year = fiscal_year or ""
            result.quarter = quarter or ""
            logger.info(f"[抽出] PDF抽出成功: sales={result.sales}, op={result.operating_profit}")
            return result, ""
        return None, error

    return None, "ドキュメントが見つかりません"


# ============================================================
# 受注系メトリクス抽出
# ============================================================

# 受注系キーワード辞書
ORDERS_KEYWORDS = [
    "受注高", "受注額", "新規受注", "受注工事高",
]
BACKLOG_KEYWORDS = [
    "受注残高", "受注残", "手持工事高", "手持ち工事高",
    "繰越工事高", "繰越高", "次期繰越工事高", "繰り越し工事高",
]
CARRYOVER_KEYWORDS = [
    "繰越工事高", "繰越高", "次期繰越工事高", "繰り越し工事高",
]

# 合計行判定キーワード
TOTAL_ROW_KEYWORDS = ["合計", "総計", "計"]

# metric_nameマッピング
_ORDER_METRIC_MAP = [
    ("orders_total", ORDERS_KEYWORDS),
    ("backlog_total", BACKLOG_KEYWORDS),
    ("carryover_construction_total", CARRYOVER_KEYWORDS),
]


def _find_order_table_block(lines: list[str]) -> list[tuple[int, int, str]]:
    """
    受注系キーワードを含む行の位置を検出する。
    Returns: [(line_idx, keyword_group_idx, matched_keyword), ...]
    """
    hits = []
    for i, line in enumerate(lines):
        for gidx, (metric_name, keywords) in enumerate(_ORDER_METRIC_MAP):
            for kw in keywords:
                if kw in line:
                    hits.append((i, gidx, kw))
                    break
    return hits


def _extract_total_from_table(
    lines: list[str],
    keyword_line_idx: int,
    keyword: str,
) -> tuple[int | None, str, str]:
    """
    キーワード行付近から「合計」行の数値を抽出する。

    Returns: (value, confidence, raw_text)
        value: 抽出した数値 or None
        confidence: 'high'(合計行あり) / 'medium'(単一行) / 'low'(取れず)
        raw_text: 抽出元の行テキスト
    """
    # キーワード行から下方30行を探索（表の範囲）
    search_end = min(keyword_line_idx + 30, len(lines))

    # 合計行を探す
    for i in range(keyword_line_idx, search_end):
        line = lines[i]
        is_total = False
        for tw in TOTAL_ROW_KEYWORDS:
            if tw in line:
                # "合計" が行の先頭付近にあるか「合計」だけの行
                stripped = line.strip()
                if stripped.startswith(tw) or stripped == tw:
                    is_total = True
                    break
                # "受注高合計" のようにキーワード+合計
                if keyword in line and tw in line:
                    is_total = True
                    break

        if not is_total:
            continue

        # 合計行から数値抽出
        nums = _extract_numbers_from_line(line, tw)
        if nums:
            return nums[0], "high", line.strip()

        # 合計行の次の行
        if i + 1 < len(lines):
            nums = _extract_numbers_from_line(lines[i + 1])
            if nums:
                return nums[0], "high", lines[i + 1].strip()

    # 合計行がない → quarantine対象
    return None, "low", ""


def extract_order_metrics(
    pdf_path: str,
    title: str,
) -> tuple[ExtractedOrderMetrics | None, str]:
    """
    決算短信PDFから受注高・受注残・繰越工事高を抽出する。

    Returns:
        (ExtractedOrderMetrics or None, エラー/quarantine理由)
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages[:5]:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        return None, f"PDF読み込みエラー: {e}"

    # [一時検証用] 強制テスト: native textを空扱い
    if _ocr_force_test() and text.strip():
        logger.info("[pdf-ocr] FORCE_TEST: order native text suppressed")
        text = ""
    # ── ① テキスト空 → OCR fallback ──
    if not text.strip():
        if _ocr_enabled():
            logger.info("[order-ocr] Native text empty, trying OCR fallback")
            ocr_text = _run_ocr_pipeline(pdf_path)
            if ocr_text and ocr_text.strip():
                return _extract_order_metrics_from_ocr(ocr_text)
        return None, "テキスト抽出不可"

    lines = text.split("\n")

    # 受注系キーワードがテキスト内に存在するかチェック
    all_kws = ORDERS_KEYWORDS + BACKLOG_KEYWORDS + CARRYOVER_KEYWORDS
    has_any = any(kw in text for kw in all_kws)

    # ── ② キーワードなし → OCR fallback ──
    if not has_any:
        if _ocr_enabled():
            logger.info("[order-ocr] No keywords in native text, trying OCR fallback")
            ocr_text = _run_ocr_pipeline(pdf_path)
            if ocr_text and ocr_text.strip():
                result = _extract_order_metrics_from_ocr(ocr_text)
                if result[0] is not None:
                    return result
        return None, "no_order_keywords"

    # 単位検出
    scale_str = _detect_scale(text)
    scale_multiplier = parse_scale_unit(scale_str)

    metrics: list[OrderMetric] = []
    quarantine_reasons: list[str] = []

    for metric_name, keywords in _ORDER_METRIC_MAP:
        # キーワードを含む行を探す
        kw_line_idx = None
        matched_kw = None
        for i, line in enumerate(lines):
            for kw in keywords:
                if kw in line:
                    kw_line_idx = i
                    matched_kw = kw
                    break
            if kw_line_idx is not None:
                break

        if kw_line_idx is None:
            continue  # このメトリクスは書類に存在しない

        value, confidence, raw_text = _extract_total_from_table(
            lines, kw_line_idx, matched_kw
        )

        if value is not None:
            # 単位正規化（百万円に統一）
            normalized = value
            if scale_str == "億円":
                normalized = value * 100  # 億→百万
            elif scale_str == "千円":
                normalized = value // 1000 if abs(value) >= 1000 else value  # 千円→百万
            elif scale_str == "円":
                normalized = value // 1_000_000 if abs(value) >= 1_000_000 else value

            metrics.append(OrderMetric(
                metric_name=metric_name,
                value=normalized,
                raw_value=value,
                unit=scale_str,
                confidence=confidence,
                raw_text=raw_text,
            ))
        else:
            quarantine_reasons.append(
                f"{metric_name}: キーワード'{matched_kw}'はあるが合計行が見つからない"
            )

    # ── ③ メトリクス0件 → OCR fallback ──
    if not metrics:
        if _ocr_enabled():
            logger.info("[order-ocr] Native extraction found 0 metrics, trying OCR fallback")
            ocr_text = _run_ocr_pipeline(pdf_path)
            if ocr_text and ocr_text.strip():
                result = _extract_order_metrics_from_ocr(ocr_text)
                if result[0] is not None:
                    return result
        reason = "; ".join(quarantine_reasons) if quarantine_reasons else "no_extractable_values"
        return None, reason

    return ExtractedOrderMetrics(
        metrics=metrics,
        source_unit=scale_str,
    ), "; ".join(quarantine_reasons) if quarantine_reasons else ""


# ============================================================
# セグメント別 売上・利益 抽出
# ============================================================

# セグメント表の見出しキーワード
_SEGMENT_HEADER_KW = [
    "報告セグメント", "事業セグメント", "セグメント情報",
    "セグメント別", "事業別", "部門別",
    # Phase 6: 売上/利益型ヘッダー
    "売上高及び利益", "売上高と利益", "売上収益及び利益",
    "報告セグメントごとの売上高", "セグメントの業績",
    "外部顧客への売上高",
    "セグメント間内部売上高", "セグメント間内部営業収益",
]

# 列ヘッダー（売上系）
_SEG_SALES_KW = [
    "売上高", "売上収益", "営業収益", "収益",
    "外部顧客への売上高", "外部顧客への売上収益",
    "外部顧客売上高", "外部売上高",
    "営業収入", "経常収益", "事業収益",
    "売上", "売 上 高",
    "Net sales", "Revenue", "Sales",
]

# 列ヘッダー（利益系）
_SEG_PROFIT_KW = [
    "セグメント利益", "セグメント損益", "営業利益", "事業利益",
    "利益又は損失", "利益（損失）", "損益",
    "セグメント利益又は損失", "セグメント利益（損失）",
    "経常利益", "セグメント 利益", "営業 利益",
    "利益又は損失（△は損失）", "利益(損失)",
    "Operating profit", "Operating income", "Profit",
    "Segment profit", "Segment income",
]

# スキップ行（合計・調整・消去等）
_SEG_SKIP_LABELS = [
    "合計", "総計", "計", "調整額", "消去", "消去又は全社",
    "全社", "配賦不能", "セグメント間", "内部取引",
]


@dataclass
class SegmentExtracted:
    """抽出されたセグメント1件"""
    segment_name: str
    segment_order: int
    segment_sales: float | None = None
    segment_profit: float | None = None
    raw_profit_label: str = ""
    raw_text: str = ""
    # Phase 2: Trace & Scores
    rule_trace: list[str] = field(default_factory=list)
    score_summary: dict = field(default_factory=dict)


def _find_segment_table_region(lines: list[str]) -> tuple[int, int] | None:
    """
    セグメント表の開始行と終了行を検出する。
    目次行（"…" "・・" "───" を含む行）はスキップする。
    Returns: (start_idx, end_idx) or None
    """
    # 目次行の判定: 点線やページ番号参照を含む行（例: "（セグメント情報等）………8"）
    _TOC_INDICATORS = ["…", "・・", "───", "─────"]

    def _is_toc_line(text: str) -> bool:
        return any(ind in text for ind in _TOC_INDICATORS)

    start = None
    for i, line in enumerate(lines):
        if _is_toc_line(line):
            continue  # 目次行はスキップ
        for kw in _SEGMENT_HEADER_KW:
            if kw in line:
                start = i
                break
        if start is not None:
            break

    if start is None:
        return None

    # 表の終了を検出（空行が2つ連続 or 別セクション見出し）
    _SECTION_END_KW = [
        "連結損益", "連結貸借", "連結キャッシュ", "経営成績",
        "連結損益計算書", "四半期連結損益計算書",
        "連結貸借対照表", "キャッシュ・フローの状況",
        "財政状態",
    ]
    end = min(start + 50, len(lines))
    blank_count = 0
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            blank_count += 1
            if blank_count >= 2:
                end = i
                break
        else:
            blank_count = 0
        # 別セクション見出しで終了
        if any(kw in stripped for kw in _SECTION_END_KW):
            end = i
            break

    return (start, end)


def _is_segment_skip_label(text: str) -> bool:
    """合計・調整・消去等のスキップ対象かを完全一致で判定"""
    stripped = text.strip()
    return stripped in _SEG_SKIP_LABELS


def _detect_column_positions(
    lines: list[str], start: int, end: int,
) -> tuple[bool, bool, str]:
    """
    セグメント表に売上列と利益列があるかを検出する。
    ヘッダー正規化を適用し、空白揺れ・全半角揺れを吸収。
    Returns: (has_sales_col, has_profit_col, raw_profit_label)
    """
    # ヘッダー探索範囲: startから最大10行（複数行ヘッダー対応）
    header_end = min(start + 10, end)
    header_text_raw = "\n".join(lines[start:header_end])
    header_normalized = normalize_header(header_text_raw)

    has_sales = False
    for kw in _SEG_SALES_KW:
        kw_norm = normalize_header(kw)
        if kw_norm in header_normalized:
            has_sales = True
            break

    has_profit = False
    profit_label = ""
    for kw in _SEG_PROFIT_KW:
        kw_norm = normalize_header(kw)
        if kw_norm in header_normalized:
            has_profit = True
            profit_label = kw
            break
    return has_sales, has_profit, profit_label


_extraction_tls = threading.local()


def get_last_v2_segment_result():
    """最後に実行された v2 セグメント抽出結果 (スレッドローカル) を取得。"""
    return getattr(_extraction_tls, "last_v2_result", None)


def extract_segment_financials(
    pdf_path: str,
    title: str,
    *,
    use_v2: bool = True,
    doc_id: str = "",
    ticker: str = "",
) -> tuple[list[SegmentExtracted], str]:
    """
    決算短信PDFからセグメント別売上・利益を抽出する。

    v2 (use_v2=True):
      多段スコアリング (page→table→header→column→row) で検出。
      confidence が低い場合は v1 にフォールバック。

    v1 (fallback):
      従来の KW ベース検出。

    Returns:
        (list[SegmentExtracted], quarantine理由)
    """
    import os
    # 環境変数で v2 無効化
    if os.environ.get("SEGMENT_V2_DISABLE", "").lower() in ("1", "true"):
        use_v2 = False

    v2_quarantine_reason = ""

    # [一時検証用] 強制テスト: v2/v1をスキップしてOCR経路に直行
    if _ocr_force_test() and _ocr_enabled():
        logger.info("[pdf-ocr] FORCE_TEST: segment v2/v1 skipped, going to OCR")
        ocr_text = _run_ocr_pipeline(pdf_path)
        if ocr_text and ocr_text.strip():
            ocr_segments = _extract_segments_from_ocr(ocr_text)
            if ocr_segments:
                return ocr_segments, ""
        logger.info("[pdf-ocr] FORCE_TEST: OCR segment extraction failed")
        return [], "force_test_ocr_failed"

    if use_v2:
        try:
            from .analysis.segment_detection_v2 import run_segment_detection_v2
            v2_result = run_segment_detection_v2(pdf_path, doc_id=doc_id, ticker=ticker)
            _extraction_tls.last_v2_result = v2_result

            if v2_result.success and v2_result.segments:
                # v2 成功 → SegmentExtracted に変換
                segments = []
                for seg in v2_result.segments:
                    segments.append(SegmentExtracted(
                        segment_name=seg.segment_name,
                        segment_order=seg.segment_order,
                        segment_sales=seg.segment_sales,
                        segment_profit=seg.segment_profit,
                        raw_profit_label=seg.raw_profit_label,
                        raw_text=seg.raw_text,
                        rule_trace=v2_result.rule_trace,
                        score_summary=v2_result.score_summary,
                    ))
                logger.info(
                    f"[v2] セグメント抽出成功: {len(segments)}件, "
                    f"engine={seg.extraction_engine}, "
                    f"unit={v2_result.score_summary.get('unit_raw', '?')}, "
                    f"profit_role={v2_result.score_summary.get('profit_col_role', '?')}"
                )
                return segments, ""

            # v2 が quarantine → v1 fallback
            v2_quarantine_reason = v2_result.quarantine_reason or ""
            logger.info(f"[v2] fallback to v1: {v2_quarantine_reason} stage={v2_result.failed_stage}")
        except Exception as e:
            logger.warning(f"[v2] 例外発生, v1 fallback: {e}")

    # ============ v1 ロジック ============
    segments_v1, err_v1 = _extract_segment_financials_v1(pdf_path, title)
    if segments_v1:
        return segments_v1, ""

    # ============ OCR fallback ============
    if _ocr_enabled():
        logger.info(f"[segment-ocr] v2/v1 both failed, trying OCR fallback")
        ocr_text = _run_ocr_pipeline(pdf_path)
        if ocr_text and ocr_text.strip():
            ocr_segments = _extract_segments_from_ocr(ocr_text)
            if ocr_segments:
                return ocr_segments, ""
            logger.info("[ocr] no improvement for segments")

    # V2 quarantine_reason を error に付与して返す (worker 側で抽出可能)
    if v2_quarantine_reason:
        return [], f"v2_reason:{v2_quarantine_reason}|{err_v1}"
    return [], err_v1


def _extract_segment_financials_v1(
    pdf_path: str,
    title: str,
) -> tuple[list[SegmentExtracted], str]:
    """v1 セグメント抽出 (従来ロジック + PL除外)"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages[:8]:  # セグメント情報は後半ページにあることも
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        return [], f"PDF読み込みエラー: {e}"

    if not text.strip():
        return [], "テキスト抽出不可"

    lines = text.split("\n")

    # セグメント表を検出
    region = _find_segment_table_region(lines)
    if region is None:
        return [], "no_segment_table"

    start, end = region

    # --- PL テーブル判定 ---
    _PL_V1_LABELS = [
        "売上原価", "売上総利益", "販売費及び一般管理費", "販売費・一般管理費",
        "営業外収益", "営業外費用", "経常利益", "特別利益", "特別損失",
        "税金等調整前", "法人税等", "当期純利益", "四半期純利益",
        "受取利息", "支払利息", "受取配当金", "減価償却費", "人件費",
    ]
    _PL_V1_STRONG = [
        "売上原価", "売上総利益", "販売費及び一般管理費",
        "営業外収益", "営業外費用", "経常利益", "税金等調整前", "法人税等",
    ]
    region_text = "\n".join(lines[start:end])
    pl_account_hits = sum(1 for kw in _PL_V1_LABELS if kw in region_text)
    pl_strong_hits = sum(1 for kw in _PL_V1_STRONG if kw in region_text)

    if pl_strong_hits >= 3 or pl_account_hits >= 5:
        logger.info(
            f"[v1] PL テーブル判定で reject: "
            f"account_hits={pl_account_hits}, strong_hits={pl_strong_hits}"
        )
        return [], "picked_pl_table"

    # --- Candidate Guard (narrative/BS/CF 汚染検出) ---
    try:
        from src.analysis.row_classifier import evaluate_candidate_guard, log_candidate_guard
        _v1_labels = []
        for _v1_i in range(start, end):
            _v1_line = lines[_v1_i].strip()
            if not _v1_line:
                continue
            _v1_m = re.match(r'^([^\d△▲\-－]*)', _v1_line)
            if _v1_m and _v1_m.group(1).strip():
                _v1_labels.append(_v1_m.group(1).strip())
            elif _v1_line:
                _v1_labels.append(_v1_line)

        _v1_guard = evaluate_candidate_guard(_v1_labels)
        log_candidate_guard(_v1_guard, table_index=0)

        if not _v1_guard.accepted:
            _v1_hint_map = {
                "narrative_guard": "pdf_narrative_block_selected",
                "bs_cf_guard": "pdf_narrative_block_selected",
                "pl_guard": "pdf_pl_table_selected",
                "detail_breakdown_guard": "pdf_segment_like_but_invalid_structure",
                "invalid_structure": "pdf_segment_like_but_invalid_structure",
                "no_valid_segment_rows": "pdf_no_segment_table_after_guard",
                "total_metric_dominant": "pdf_no_segment_table_after_guard",
            }
            _v1_hint = _v1_hint_map.get(_v1_guard.reject_reason, "pdf_no_segment_table_after_guard")
            logger.info(
                f"[v1] candidate guard reject: {_v1_guard.reject_reason} "
                f"valid={_v1_guard.valid_segment_like} narr={_v1_guard.narrative_like} "
                f"bscf={_v1_guard.bs_cf_like} garbage={_v1_guard.garbage_fragment_like}"
            )
            return [], f"candidate_guard:{_v1_guard.reject_reason}|hint={_v1_hint}"
    except ImportError:
        pass  # row_classifier が無い環境では guard をスキップ

    has_sales, has_profit, profit_label = _detect_column_positions(lines, start, end)

    if not has_sales and not has_profit:
        return [], "segment_table_found_but_no_sales_profit_columns"

    # 単位検出
    scale_str = _detect_scale(text)

    segments: list[SegmentExtracted] = []
    order = 0

    # PL 勘定科目として除外するラベル
    _PL_ROW_REJECT = set(_PL_V1_LABELS)


    # セグメント行からデータ抽出
    for i in range(start + 1, end):
        line = lines[i].strip()
        if not line:
            continue

        # 数値がない行はスキップ（見出しやラベルのみ）
        nums = _extract_numbers_from_line(line)
        if not nums:
            continue

        # 行の最初の非数値部分をセグメント名として取得
        name_match = re.match(r'^([^\d△▲\-－]+)', line)
        if not name_match:
            continue

        seg_name = name_match.group(1).strip()
        if len(seg_name) <= 1:
            continue

        if _is_segment_skip_label(seg_name):
            continue

        # PL 勘定科目行は除外
        if any(pl_kw in seg_name for pl_kw in _PL_ROW_REJECT):
            continue

        seg_sales = None
        seg_profit = None

        if has_sales and has_profit:
            if len(nums) >= 2:
                seg_sales = nums[0]
                seg_profit = nums[1]
            elif len(nums) == 1:
                seg_sales = nums[0]
        elif has_sales:
            seg_sales = nums[0] if nums else None
        elif has_profit:
            seg_profit = nums[0] if nums else None

        if seg_sales is not None:
            if scale_str == "億円":
                seg_sales = seg_sales * 100
            elif scale_str == "千円":
                seg_sales = seg_sales // 1000 if abs(seg_sales) >= 1000 else seg_sales
            elif scale_str == "円":
                seg_sales = seg_sales // 1_000_000 if abs(seg_sales) >= 1_000_000 else seg_sales

        if seg_profit is not None:
            if scale_str == "億円":
                seg_profit = seg_profit * 100
            elif scale_str == "千円":
                seg_profit = seg_profit // 1000 if abs(seg_profit) >= 1000 else seg_profit
            elif scale_str == "円":
                seg_profit = seg_profit // 1_000_000 if abs(seg_profit) >= 1_000_000 else seg_profit

        order += 1
        segments.append(SegmentExtracted(
            segment_name=seg_name,
            segment_order=order,
            segment_sales=seg_sales,
            segment_profit=seg_profit,
            raw_profit_label=profit_label,
            raw_text=line,
        ))

    if not segments:
        return [], "segment_table_found_but_no_rows_extracted"

    return segments, ""

