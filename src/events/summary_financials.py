#!/usr/bin/env python3
"""summary_financials.py — 決算短信の数値抽出とYOY/QoQ算出

XBRLファイル内の当期 + 前年同期データを同時抽出し、
文書内完結でYOY/QoQを計算する。AIは一切使わない。

優先順位:
  (1) 文書内XBRLから計算
  (2) 文書内PDFテーブルから計算
  (3) 不足時はDB参照（将来拡張）
  (4) 欠損扱い

QoQ導出:
  - 2Q単体 = 2Q累計 - 1Q累計
  - 3Q単体 = 3Q累計 - 2Q累計
  - 4Q単体 = 通期 - 3Q累計
"""
from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from src.events.pipeline_context import EarningsExtractionEvidence
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("summary_financials")

# ============================================================
# データモデル
# ============================================================

@dataclass
class PeriodFinancials:
    """ある期間の財務数値"""
    sales: int | None = None
    operating_profit: int | None = None
    gross_profit: int | None = None
    selling_general_and_administrative_expenses: int | None = None
    source: str = ""  # "xbrl" / "pdf"
    sales_priority: tuple | None = None
    evidences: list = field(default_factory=list)


@dataclass
class SegmentFinancials:
    """セグメント別の財務数値"""
    name: str = ""
    sales_current: int | None = None
    sales_prior: int | None = None
    profit_current: int | None = None
    profit_prior: int | None = None

    @property
    def sales_yoy(self) -> float | None:
        return _calc_ratio(self.sales_current, self.sales_prior)

    @property
    def profit_yoy(self) -> float | None:
        return _calc_ratio(self.profit_current, self.profit_prior)


@dataclass
class EarningsSummaryData:
    """決算短信の全値の構成"""
    # 通期（累計ベース）
    sales_current: int | None = None
    sales_prior: int | None = None
    op_current: int | None = None
    op_prior: int | None = None
    gross_profit_current: int | None = None
    selling_general_and_administrative_expenses_current: int | None = None

    # 単体ベース（QoQ用）
    sales_q_current: int | None = None   # 当四半期単体
    sales_q_prior: int | None = None     # 前四半期単体
    op_q_current: int | None = None
    op_q_prior: int | None = None

    # セグメント
    segments: list[SegmentFinancials] = field(default_factory=list)
    evidences: list = field(default_factory=list)

    # メタ
    period: str = ""       # "2025-03-31"
    quarter: str = ""      # "1Q"/"2Q"/"3Q"/"4Q"/"FY"
    source: str = ""       # "xbrl" / "pdf"
    ticker: str = ""
    source_unit: str = ""  # "百万円" etc

    # ---- YOY (累計ベース、単四半期ベース優先) ----
    @property
    def sales_yoy(self) -> float | None:
        # 単四半期ベース優先
        r = _calc_ratio(self.sales_q_current, self.sales_q_prior)
        if r is not None:
            return r
        return _calc_ratio(self.sales_current, self.sales_prior)

    @property
    def op_yoy(self) -> float | None:
        r = _calc_ratio(self.op_q_current, self.op_q_prior)
        if r is not None:
            return r
        return _calc_ratio(self.op_current, self.op_prior)

    # ---- QoQ ----
    @property
    def sales_qoq(self) -> float | None:
        return _calc_ratio(self.sales_q_current, self.sales_q_prior)

    @property
    def op_qoq(self) -> float | None:
        return _calc_ratio(self.op_q_current, self.op_q_prior)

    @property
    def has_yoy(self) -> bool:
        return self.sales_yoy is not None or self.op_yoy is not None

    def format_summary_line(self, clip: float | None = 2.0) -> str:
        """出力フォーマットの数値行を生成（絶対値+YOY）"""
        parts = []
        sales_str = _fmt_metric("売上", self.sales_yoy, self.sales_qoq, self.sales_current, unit="yen", clip=clip)
        op_str = _fmt_metric("営業利益", self.op_yoy, self.op_qoq, self.op_current, unit="yen", clip=clip)
        if sales_str:
            parts.append(sales_str)
        if op_str:
            parts.append(op_str)
        return "\n".join(parts)

    def format_segment_lines(self) -> str:
        """セグメント行を列揃えで生成"""
        if not self.segments:
            return ""

        # 各セグメントのデータ行を事前計算
        rows: list[tuple[str, str, str]] = []  # (name, sales_col, profit_col)
        for seg in self.segments:
            # 売上列
            if seg.sales_current is not None and seg.sales_yoy is not None:
                s_col = f"{_fmt_oku(seg.sales_current)}({_fmt_pct_short(seg.sales_yoy, clip=2.0)})"
            elif seg.sales_yoy is not None:
                s_col = f"({_fmt_pct_short(seg.sales_yoy, clip=2.0)})"
            else:
                s_col = ""
            # 営利列
            if seg.profit_current is not None and seg.profit_yoy is not None:
                p_col = f"{_fmt_oku(seg.profit_current)}({_fmt_pct_short(seg.profit_yoy, clip=2.0)})"
            elif seg.profit_yoy is not None:
                p_col = f"({_fmt_pct_short(seg.profit_yoy, clip=2.0)})"
            else:
                p_col = ""
            if s_col or p_col:
                rows.append((seg.name, s_col, p_col))

        if not rows:
            return ""

        # 列幅計算 (全角文字幅を考慮)
        def _display_width(s: str) -> int:
            w = 0
            for c in s:
                w += 2 if ord(c) > 0x7F else 1
                # ただし半角カナ等は除外（簡易版）
            return w

        name_max = max(_display_width(n) for n, _, _ in rows)
        sales_max = max((_display_width(s) for _, s, _ in rows if s), default=0)

        lines = ["セグメント："]
        for name, s_col, p_col in rows:
            # セグメント名パディング
            pad_name = name_max - _display_width(name)
            padded_name = name + "　" * (pad_name // 2) + " " * (pad_name % 2)
            # 売上列パディング（右揃え）
            pad_sales = sales_max - _display_width(s_col)
            padded_sales = " " * pad_sales + s_col
            # 組み立て
            if p_col:
                lines.append(f"・{padded_name}　{padded_sales}　{p_col}")
            else:
                lines.append(f"・{padded_name}　{padded_sales}")

        return "\n".join(lines)


# ============================================================
# ヘルパー関数
# ============================================================

def _calc_ratio(current: int | None, prior: int | None) -> float | None:
    """YOY/QoQ比率を算出 (0.123 = +12.3%)"""
    if current is None or prior is None or prior == 0:
        return None
    return (current / prior) - 1.0


def _fmt_pct(ratio: float | None) -> str:
    """比率をフォーマット (+12.3%)"""
    if ratio is None:
        return "N/A"
    sign = "+" if ratio >= 0 else ""
    return f"{sign}{ratio * 100:.1f}%"


def _fmt_pct_short(ratio: float | None, clip: float | None = None) -> str:
    """比率を整数%表記 (+18% / -7%)。clip指定時は表示のみクリップ。"""
    if ratio is None:
        return "N/A"
    if clip is not None and abs(ratio) > clip:
        sign = "+" if ratio > 0 else "-"
        return f"{sign}{int(clip * 100)}%+"
    sign = "+" if ratio >= 0 else ""
    return f"{sign}{ratio * 100:.0f}%"


def _fmt_oku(val: int | None, unit: str = "millions") -> str:
    """数値を億円に変換してフォーマット（桁区切り、小数点1桁）

    unit: 'yen' (円) / 'millions' (百万円)
    """
    if val is None:
        return ""
    if unit == "yen":
        oku = val / 100_000_000  # 円 → 億円
    else:
        oku = val / 100  # 百万円 → 億円
    abs_oku = abs(oku)
    sign = "-" if oku < 0 else ""
    if abs_oku >= 10:
        return f"{sign}{abs_oku:,.0f}億円"
    else:
        return f"{sign}{abs_oku:,.1f}億円"


def _fmt_metric(label: str, yoy: float | None, qoq: float | None,
                abs_val: int | None = None, unit: str = "millions",
                clip: float | None = 2.0) -> str:
    """指標行をフォーマット（絶対値+YOY）。clip指定時は表示のみクリップ。"""
    if yoy is None:
        return ""
    yoy_str = _fmt_pct_short(yoy, clip=clip)
    abs_str = _fmt_oku(abs_val, unit=unit)
    if abs_str:
        parts = [f"{label} {abs_str}（YOY {yoy_str}）"]
    else:
        parts = [f"{label} YOY {yoy_str}"]
    if qoq is not None:
        qoq_str = _fmt_pct_short(qoq, clip=clip)
        parts[0] = parts[0].rstrip("）") + f" QoQ {qoq_str}）"
    return parts[0]


# ============================================================
# XBRL タグマップ（extractor.py と共通）
# ============================================================
_XBRL_TAG_MAP = {
    "NetSales": "sales",
    "Revenue": "sales",
    "OperatingRevenue": "sales",
    "OperatingRevenuesREIT": "sales",
    "OperatingRevenueINV": "sales",
    "SalesIFRS": "sales",
    "RevenueFromContractsWithCustomers": "sales",
    "RevenueIFRS": "sales",
    "NetOperatingRevenueSEC": "sales",
    "OperatingRevenueSEC": "sales",
    "GrossOperatingRevenues": "sales",
    "GrossOperatingRevenue": "sales",
    "OperatingIncome": "operating_profit",
    "OperatingProfit": "operating_profit",
    "OperatingIncomeIFRS": "operating_profit",
    "OrdinaryIncome": "operating_profit",
    "GrossProfit": "gross_profit",
    "GrossProfitLoss": "gross_profit",
    "GrossProfitIFRS": "gross_profit",
    "OperatingGrossProfit": "gross_profit",
    "SellingGeneralAndAdministrativeExpenses": "selling_general_and_administrative_expenses",
    "SellingGeneralAndAdministrativeExpensesIFRS": "selling_general_and_administrative_expenses",
}

_FALLBACK_TAG_MAP = {
    "OperatingRevenues": "sales",
    "OrdinaryIncomeBNK": "sales",
    "OperatingRevenuesSE": "sales",
    "NetSalesOfCompletedConstructionContractsCNS": "sales",
    "GrossProfitOnCompletedConstructionContractsCNS": "gross_profit",
}

_IXBRL_EXTENSIONS = ("-ixbrl.htm", ".ixbrl.htm", "-ixbrl.html", ".ixbrl.html", ".ixbrl")
_XBRL_EXTENSIONS = (".xbrl",)
_ZIP_SIGNATURE = b"PK\x03\x04"

# ============================================================
# contextRef 判定
# ============================================================

# 当期累計 (YTD)
_CURRENT_YTD_KEYWORDS = ("CurrentYearDuration", "CurrentYTDDuration", "CurrentAccumulatedQ")
# 前年同期累計
_PRIOR_YTD_KEYWORDS = ("Prior1YearDuration", "PriorYearDuration", "Prior1YTDDuration", "PriorAccumulatedQ")
# 当四半期 (単体)
_CURRENT_Q_KEYWORDS = ("CurrentQuarterDuration",)
# 前年同四半期 (単体)
_PRIOR_Q_KEYWORDS = ("Prior1QuarterDuration", "PriorQuarterDuration")
# 中間期（2Q）
_INTERIM_KEYWORDS = ("InterimDuration",)


def _classify_context(ctx: str) -> str:
    """contextRefをperiod分類する。

    Returns: "current_ytd" / "prior_ytd" / "current_q" / "prior_q" / "interim" / "unknown"
    """
    # Priorチェックを先に（CurrentYearDuration がPriorに部分一致しないように）
    for kw in _PRIOR_Q_KEYWORDS:
        if kw in ctx:
            return "prior_q"
    for kw in _PRIOR_YTD_KEYWORDS:
        if kw in ctx:
            return "prior_ytd"
    for kw in _CURRENT_Q_KEYWORDS:
        if kw in ctx:
            return "current_q"
    for kw in _CURRENT_YTD_KEYWORDS:
        if kw in ctx:
            return "current_ytd"
    for kw in _INTERIM_KEYWORDS:
        # Prior1InterimDuration は前期なので除外
        if kw in ctx and "Prior" not in ctx:
            return "current_ytd"  # 中間期は累計扱い
    return "unknown"


def _is_consolidated_preferred(ctx: str) -> bool:
    return "Consolidated" in ctx and "NonConsolidated" not in ctx


def _detect_quarter_from_context(ctx: str) -> str:
    if "FirstQuarterMember" in ctx:
        return "1Q"
    if "SecondQuarterMember" in ctx:
        return "2Q"
    if "ThirdQuarterMember" in ctx:
        return "3Q"
    if "YearEndMember" in ctx or "AnnualMember" in ctx:
        return "4Q"
    return ""


# ============================================================
# HTML バイト列のエンコーディング自動検出
# ============================================================

def _decode_html_bytes(raw: bytes) -> str:
    """HTMLバイト列を正しいエンコーディングでデコードする。

    検出順序:
      1. BOM (UTF-8 BOM, UTF-16 BOM)
      2. <meta charset="..."> タグ
      3. UTF-8 (strict) -> 成功時はこれを採用 (誤ってCP932と判定されるのを防止)
      4. chardet (利用可能な場合)
      5. Shift_JIS (cp932) fallback (エラーなしでデコードできる場合)
      6. UTF-8 errors=replace fallback
    """
    # 1. BOM
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")

    # 2. <meta charset> 検出
    head = raw[:2000].lower()
    charset_match = re.search(rb'charset[="\s]+([a-z0-9_-]+)', head)
    if charset_match:
        charset = charset_match.group(1).decode("ascii", errors="ignore")
        # 正規化
        charset_map = {"shift_jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
                       "euc-jp": "euc_jp", "iso-2022-jp": "iso2022_jp"}
        charset = charset_map.get(charset, charset)
        try:
            return raw.decode(charset, errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass

    # 3. UTF-8 (strict)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass

    # 4. chardet (利用可能な場合)
    try:
        import chardet
        detected = chardet.detect(raw[:5000])
        if detected and detected.get("encoding"):
            enc = detected["encoding"]
            try:
                return raw.decode(enc, errors="replace")
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass

    # 5. Shift_JIS (cp932) fallback (TDnetの多くはShift_JIS)
    try:
        return raw.decode("cp932", errors="strict")
    except UnicodeDecodeError:
        pass

    # 6. UTF-8 fallback
    return raw.decode("utf-8", errors="replace")


# ============================================================
# iXBRL scale 処理
# ============================================================
def _apply_ixbrl_scale(raw_text: str, scale: str, sign: str) -> int | None:
    from src.utils import normalize_number
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


# ============================================================
# XBRL パーサ（当期 + 前期同時抽出）
# ============================================================

def _parse_xbrl_multi_period(raw: bytes, include_evidence: bool = False) -> dict[str, PeriodFinancials]:
    """XBRLバイト列から当期・前期・当四半期・前四半期の数値を同時抽出。

    Returns:
        {"current_ytd": PeriodFinancials, "prior_ytd": ..., "current_q": ..., "prior_q": ...}
    """
    try:
        from src.xbrl_clean import read_xbrl_bytes
    except ImportError:
        logger.warning("xbrl_clean not available")
        return {}

    xml_str = read_xbrl_bytes(raw)
    root = ET.fromstring(xml_str)

    # 各期間の値を格納
    values: dict[str, dict[str, int | None]] = {
        "current_ytd": {"sales": None, "operating_profit": None, "gross_profit": None, "selling_general_and_administrative_expenses": None},
        "prior_ytd": {"sales": None, "operating_profit": None, "gross_profit": None, "selling_general_and_administrative_expenses": None},
        "current_q": {"sales": None, "operating_profit": None, "gross_profit": None, "selling_general_and_administrative_expenses": None},
        "prior_q": {"sales": None, "operating_profit": None, "gross_profit": None, "selling_general_and_administrative_expenses": None},
    }
    priority: dict[str, dict[str, tuple[bool, int]]] = {k: {} for k in values}
    evidences = {k: [] for k in values}

    # --- パス1: 従来XBRLモード ---
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        
        primary_metric = _XBRL_TAG_MAP.get(tag_local)
        fallback_metric = _FALLBACK_TAG_MAP.get(tag_local)
        if primary_metric is None and fallback_metric is None:
            continue
        field_name = primary_metric or fallback_metric
        is_fallback = primary_metric is None and fallback_metric is not None
        if field_name not in ("sales", "operating_profit", "gross_profit", "selling_general_and_administrative_expenses"):
            continue

        ctx = elem.get("contextRef", "")
        period_type = _classify_context(ctx)
        if period_type == "unknown":
            continue

        val_text = elem.text or ""
        from src.utils import normalize_number
        val = normalize_number(val_text)
        if val is None:
            continue

        is_consol = _is_consolidated_preferred(ctx)
        tag_prio = 0 if is_fallback else 10
        new_prio = (tag_prio, is_consol)
        current_prio = priority[period_type].get(field_name, (-1, False))
        if values[period_type][field_name] is None or new_prio > current_prio:
            values[period_type][field_name] = val
            priority[period_type][field_name] = new_prio
            if include_evidence:
                evidences[period_type].append(EarningsExtractionEvidence(
                    metric=field_name, value=val, tag_name=tag_local, context_ref=ctx,
                    unit="unknown", scale=None, source_file="xbrl", extraction_source="xbrl",
                    priority=tag_prio, fallback_used=is_fallback
                ))


    # パス1で当期売上が取れていれば結果を構築
    if values["current_ytd"]["sales"] is not None or values["current_ytd"]["gross_profit"] is not None:
        return {k: PeriodFinancials(sales=v["sales"], operating_profit=v["operating_profit"], gross_profit=v["gross_profit"], selling_general_and_administrative_expenses=v["selling_general_and_administrative_expenses"], source="xbrl", sales_priority=priority[k].get("sales", (-1, False)), evidences=evidences[k])
                for k, v in values.items()}

    # --- パス2: iXBRLモード ---
    values = {k: {"sales": None, "operating_profit": None, "gross_profit": None, "selling_general_and_administrative_expenses": None} for k in values}
    priority = {k: {} for k in values}
    evidences = {k: [] for k in values}

    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        if tag_local != "nonFraction":
            continue

        concept_name = elem.get("name", "")
        ctx = elem.get("contextRef", "")
        scale = elem.get("scale", "")
        sign = elem.get("sign", "")

        if not concept_name or not ctx:
            continue

        concept_local = concept_name.split(":")[-1] if ":" in concept_name else concept_name
        primary_metric = _XBRL_TAG_MAP.get(concept_local)
        fallback_metric = _FALLBACK_TAG_MAP.get(concept_local)
        if primary_metric is None and fallback_metric is None:
            continue
        field_name = primary_metric or fallback_metric
        is_fallback = primary_metric is None and fallback_metric is not None
        if field_name not in ("sales", "operating_profit", "gross_profit", "selling_general_and_administrative_expenses"):
            continue

        # Forecast/Estimate は除外
        if "ForecastMember" in ctx or "LowerMember" in ctx or "UpperMember" in ctx:
            continue

        period_type = _classify_context(ctx)
        if period_type == "unknown":
            continue

        text = (elem.text or "").strip()
        if not text:
            text = "".join(elem.itertext()).strip()
        if not text:
            continue

        val = _apply_ixbrl_scale(text, scale, sign)
        if val is None:
            continue

        is_consol = _is_consolidated_preferred(ctx)
        tag_prio = 0 if is_fallback else 10
        new_prio = (tag_prio, is_consol)
        current_prio = priority[period_type].get(field_name, (-1, False))
        
        if values[period_type][field_name] is None or new_prio > current_prio:
            values[period_type][field_name] = val
            priority[period_type][field_name] = new_prio
            if include_evidence:
                sc = int(scale) if scale and scale.lstrip("-").isdigit() else None
                evidences[period_type].append(EarningsExtractionEvidence(
                    metric=field_name, value=val, tag_name=concept_local, context_ref=ctx,
                    unit="unknown", scale=sc, source_file="ixbrl", extraction_source="ixbrl",
                    priority=tag_prio, fallback_used=is_fallback
                ))


    return {k: PeriodFinancials(sales=v["sales"], operating_profit=v["operating_profit"], gross_profit=v["gross_profit"], selling_general_and_administrative_expenses=v["selling_general_and_administrative_expenses"], source="xbrl", sales_priority=priority[k].get("sales", (-1, False)), evidences=evidences[k])
            for k, v in values.items()}


# ============================================================
# ZIP処理
# ============================================================

def _find_xbrl_in_zip(zf: zipfile.ZipFile) -> list[str]:
    ixbrl = []
    xbrl = []
    for name in zf.namelist():
        lower = name.lower()
        if any(lower.endswith(ext) for ext in _IXBRL_EXTENSIONS):
            ixbrl.append(name)
        elif any(lower.endswith(ext) for ext in _XBRL_EXTENSIONS):
            xbrl.append(name)
    return ixbrl + xbrl


def _is_summary_file(name: str) -> bool:
    lower = name.lower()
    return "/summary/" in lower or "summary" in os.path.basename(lower)


def _extract_multi_period_from_xbrl(xbrl_path: str, include_evidence: bool = False) -> dict[str, PeriodFinancials]:
    """XBRLファイル（ZIPまたは単体）から複数期間の数値を抽出"""
    try:
        raw = Path(xbrl_path).read_bytes()
    except Exception as e:
        logger.warning(f"[FINANCIALS] file read failed: {e}")
        return {}

    if raw[:4] == _ZIP_SIGNATURE:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw), "r")
        except zipfile.BadZipFile:
            return {}

        candidates = _find_xbrl_in_zip(zf)
        if not candidates:
            zf.close()
            return {}

        # Summary優先
        summary_candidates = [c for c in candidates if _is_summary_file(c)]
        other = [c for c in candidates if not _is_summary_file(c)]

        merged_result = {}
        for entry in summary_candidates + other:
            try:
                entry_bytes = zf.read(entry)
                result = _parse_xbrl_multi_period(entry_bytes, include_evidence=include_evidence)
                if not merged_result:
                    merged_result = result
                else:
                    for k, v in result.items():
                        if k not in merged_result:
                            merged_result[k] = v
                        else:
                            curr_sp = merged_result[k].sales_priority or (-1, False)
                            new_sp = v.sales_priority or (-1, False)
                            if merged_result[k].sales is None or new_sp > curr_sp:
                                merged_result[k].sales = v.sales
                                merged_result[k].sales_priority = new_sp
                            if merged_result[k].operating_profit is None: merged_result[k].operating_profit = v.operating_profit
                            if merged_result[k].gross_profit is None: merged_result[k].gross_profit = v.gross_profit
                            if getattr(merged_result[k], "selling_general_and_administrative_expenses", None) is None: merged_result[k].selling_general_and_administrative_expenses = getattr(v, "selling_general_and_administrative_expenses", None)
                            if include_evidence:
                                merged_result[k].evidences.extend(v.evidences)

                if merged_result.get("current_ytd") and merged_result["current_ytd"].sales is not None and merged_result["current_ytd"].gross_profit is not None:
                    logger.info(f"[FINANCIALS] multi-period extract OK (fully populated): {entry}")
                    zf.close()
                    return merged_result
            except Exception as e:
                logger.debug(f"[FINANCIALS] parse failed: {entry}: {e}")

        if merged_result and merged_result.get("current_ytd") and (merged_result["current_ytd"].sales is not None or merged_result["current_ytd"].gross_profit is not None):
            logger.info(f"[FINANCIALS] multi-period extract OK (partial or complete)")
            zf.close()
            return merged_result

        zf.close()
        return merged_result if merged_result else {}

    # 単体ファイル
    return _parse_xbrl_multi_period(raw, include_evidence=include_evidence)


# ============================================================
# QoQ 単四半期導出
# ============================================================

def _derive_standalone_quarter(
    current_ytd: PeriodFinancials,
    prior_ytd: PeriodFinancials,
    current_q: PeriodFinancials,
    prior_q: PeriodFinancials,
    quarter: str,
) -> tuple[PeriodFinancials, PeriodFinancials]:
    """単四半期の当期/前期を導出する。

    XBRLに CurrentQuarterDuration があればそれを使い、
    なければ累計差分から導出する。

    Returns:
        (当四半期単体, 前四半期単体)
        ※ YOY計算に使う前年同四半期 = prior_q
        ※ QoQ計算に使う前四半期 = 累計差分から導出が必要
    """
    # 当四半期単体: XBRL に直接ある場合はそのまま
    q_current = PeriodFinancials(source="derived")
    q_prior_for_qoq = PeriodFinancials(source="derived")

    if current_q.sales is not None:
        q_current = current_q
    elif quarter in ("2Q", "3Q", "4Q") and current_ytd.sales is not None and prior_ytd.sales is not None:
        # 累計差分: ただし prior_ytd はここでは「前Q累計」ではなく「前年同期累計」
        # → QoQ 用の前四半期累計は文書内に通常ない → QoQ は N/A が多い
        # しかし XBRL Summary には前年同期累計が入っているので
        # YOY用の単四半期 = 文書から直接は難しい場合がある
        pass

    # YOY用: prior_q（前年同四半期単体）がXBRLにあればそれを使う
    # QoQ用: 前四半期単体は通常XBRLに含まれないため、QoQは累計ベースでは計算困難
    # → QoQはXBRLにCurrentQuarterDuration / PriorQuarterDuration がある場合のみ

    return q_current, q_prior_for_qoq


# ============================================================
# メインエントリ
# ============================================================

def extract_earnings_data(
    xbrl_path: str | None = None,
    pdf_path: str | None = None,
    title: str = "",
    ticker: str = "",
    include_evidence: bool = False,
) -> EarningsSummaryData | None:
    """決算短信から数値を抽出しYOY/QoQを算出する。

    Parameters
    ----------
    xbrl_path : XBRL ZIP パス（優先）
    pdf_path : PDF パス（フォールバック）
    title : 開示タイトル（Q検出用）
    ticker : 銘柄コード

    Returns
    -------
    EarningsSummaryData | None
        YOYが1つも計算できない場合はNone
    """
    result = EarningsSummaryData(ticker=ticker)

    # ---- XBRL から複数期間抽出 ----
    periods: dict[str, PeriodFinancials] = {}
    if xbrl_path and os.path.isfile(xbrl_path):
        periods = _extract_multi_period_from_xbrl(xbrl_path, include_evidence=include_evidence)

    if periods.get("current_ytd") and periods["current_ytd"].sales is not None:
        cur_ytd = periods["current_ytd"]
        pri = periods.get("prior_ytd", PeriodFinancials())
        cur_q = periods.get("current_q", PeriodFinancials())
        pri_q = periods.get("prior_q", PeriodFinancials())

        result.sales_current = cur_ytd.sales
        result.sales_prior = pri.sales
        result.op_current = cur_ytd.operating_profit
        result.op_prior = pri.operating_profit
        result.gross_profit_current = cur_ytd.gross_profit
        result.selling_general_and_administrative_expenses_current = getattr(cur_ytd, "selling_general_and_administrative_expenses", None)
        result.source = cur_ytd.source
        if include_evidence:
            result.evidences.extend(cur_ytd.evidences)
            result.evidences.extend(cur_q.evidences)

        # 単四半期値
        if cur_q.sales is not None:
            result.sales_q_current = cur_q.sales
        if pri_q.sales is not None:
            result.sales_q_prior = pri_q.sales
        if cur_q.operating_profit is not None:
            result.op_q_current = cur_q.operating_profit
        if pri_q.operating_profit is not None:
            result.op_q_prior = pri_q.operating_profit

        logger.info(
            f"[FINANCIALS] XBRL extracted: "
            f"sales_cur={cur_ytd.sales} sales_pri={pri.sales} "
            f"op_cur={cur_ytd.operating_profit} op_pri={pri.operating_profit} "
            f"q_sales_cur={cur_q.sales} q_sales_pri={pri_q.sales}"
        )

    # ---- PDF フォールバック（XBRL で取れなかった場合） ----
    if result.sales_current is None and pdf_path and os.path.isfile(pdf_path):
        try:
            from src.extractor import _extract_from_pdf
            pdf_result, _ = _extract_from_pdf(pdf_path)
            if pdf_result and pdf_result.sales is not None:
                result.sales_current = pdf_result.sales
                result.op_current = pdf_result.operating_profit
                result.source = "pdf"
                result.source_unit = pdf_result.source_unit
                logger.info(f"[FINANCIALS] PDF fallback: sales={pdf_result.sales} op={pdf_result.operating_profit}")
                # PDF からは前期値が取れないことが多い → YOY 計算不可の可能性
        except Exception as e:
            logger.warning(f"[FINANCIALS] PDF extraction failed: {e}")

    # ---- Q検出 ----
    if title:
        from src.year_parser import detect_all_quarters
        qs = detect_all_quarters(title)
        if qs:
            result.quarter = qs[0]

    # ---- セグメント抽出 ----
    if xbrl_path and os.path.isfile(xbrl_path):
        segments = _extract_segments_from_zip(xbrl_path)
        if segments:
            # 日本語ラベル取得（lab.xml → HTMLテーブル の2段階）
            ja_labels = _extract_japanese_labels_from_zip(xbrl_path)
            if not ja_labels or any(seg.name not in ja_labels.values() for seg in segments):
                html_labels = _extract_japanese_labels_from_segment_html(xbrl_path)
                if html_labels:
                    # HTMLラベルで補完
                    for k, v in html_labels.items():
                        if k not in ja_labels:
                            ja_labels[k] = v
            if ja_labels:
                for seg in segments:
                    ja = ja_labels.get(seg.name)
                    if ja:
                        seg.name = ja

            # セグメント絞り込み（主要のみ、最大5件）
            segments = _filter_key_segments(segments, max_segments=5)
            result.segments = segments
            logger.info(f"[FINANCIALS] segments={len(segments)}: {[s.name for s in segments]}")

    # ---- YOY 計算可能チェック ----
    if not result.has_yoy:
        logger.info(f"[FINANCIALS] YOY calculation not possible for {ticker}")
        # YOYが計算できない場合でも、保存処理を継続するために None は返さず結果を返す
        # return None

    return result


# ============================================================
# 企業名・ticker抽出
# ============================================================

def extract_company_info_from_zip(xbrl_path: str) -> tuple[str, str]:
    """XBRL ZIPから企業名とtickerを抽出する。

    Returns
    -------
    tuple[str, str]
        (企業名, ticker)
    """
    try:
        raw = Path(xbrl_path).read_bytes()
        if raw[:4] != _ZIP_SIGNATURE:
            return ("", "")
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        return ("", "")

    company = ""
    ticker = ""

    # ticker: ファイル名パターンから取得
    for name in zf.namelist():
        m = re.search(r"-(\d{4,5})-\d{4}", os.path.basename(name))
        if m:
            ticker = m.group(1)
            break

    # company: iXBRL DEIタグからFilerName/CompanyNameを取得
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            raw_bytes = zf.read(name)
            content = _decode_html_bytes(raw_bytes)
            if "ix:non" not in content.lower():
                continue
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup.find_all(re.compile(r"ix:non", re.IGNORECASE)):
                tag_name = tag.get("name", "")
                if "FilerName" in tag_name or "CompanyName" in tag_name:
                    t = tag.get_text(strip=True)
                    if t and len(t) < 50:
                        # 「株式会社」「投資法人」等を考慮して短縮
                        company = t.replace("株式会社", "").strip()
                        break
            if company:
                break
        except Exception:
            continue

    zf.close()
    return (company, ticker)


# ============================================================
# qualitative.htm からテキスト抽出
# ============================================================

def extract_narrative_from_xbrl_zip(xbrl_path: str) -> str:
    """XBRL ZIP 内の qualitative.htm から経営概況テキストを抽出する。

    Returns
    -------
    str
        経営概況テキスト（BeautifulSoup でプレーンテキスト化済み）
    """
    try:
        raw = Path(xbrl_path).read_bytes()
        if raw[:4] != _ZIP_SIGNATURE:
            return ""
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        return ""

    # 1. qualitative.htm を探す
    qual_file = None
    for name in zf.namelist():
        basename = os.path.basename(name).lower()
        if basename == "qualitative.htm" or basename == "qualitative.html":
            qual_file = name
            break

    # 2. フォールバック: Attachment 内で大きな非iXBRL HTMLファイル
    if not qual_file:
        for name in zf.namelist():
            if "Attachment" not in name:
                continue
            if not name.endswith((".htm", ".html")):
                continue
            if "-ixbrl" in name.lower() or ".ixbrl" in name.lower():
                continue
            try:
                info = zf.getinfo(name)
                if info.file_size > 10000:
                    raw_bytes = zf.read(name)
                    # バイト列でキーワード検索（Shift_JIS/UTF-8 両対応）
                    kws_utf8 = [kw.encode("utf-8") for kw in ["経営成績", "業績の概況", "当期の概況"]]
                    kws_sjis = [kw.encode("shift_jis", errors="ignore") for kw in ["経営成績", "業績の概況", "当期の概況"]]
                    peek = raw_bytes[:2000]
                    if any(kw in peek for kw in kws_utf8 + kws_sjis):
                        qual_file = name
                        break
            except Exception:
                continue

    if not qual_file:
        zf.close()
        return ""

    try:
        raw_bytes = zf.read(qual_file)
    except Exception:
        zf.close()
        return ""
    zf.close()

    content = _decode_html_bytes(raw_bytes)

    # HTML → プレーンテキスト
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")
        # scriptやstyleタグを除去
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        # BeautifulSoup なしの場合はタグ除去のみ
        text = re.sub(r"<[^>]+>", "", content)

    logger.info(f"[NARRATIVE] qualitative.htm extracted: {len(text)} chars from {qual_file}")
    return text


# ============================================================
# 日本語セグメントラベル取得（lab.xml）
# ============================================================

def _extract_japanese_labels_from_zip(xbrl_path: str) -> dict[str, str]:
    """XBRL ZIP 内の lab.xml から日本語セグメントラベルを取得。

    Returns
    -------
    dict
        {英語readable名: 日本語ラベル} のマッピング
    """
    try:
        raw = Path(xbrl_path).read_bytes()
        if raw[:4] != _ZIP_SIGNATURE:
            return {}
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        return {}

    # lab.xml（en でない方）を探す
    lab_file = None
    for name in zf.namelist():
        basename = os.path.basename(name).lower()
        if basename.endswith("-lab.xml") and "-lab-en" not in basename:
            lab_file = name
            break

    if not lab_file:
        zf.close()
        return {}

    try:
        content = zf.read(lab_file).decode("utf-8", errors="replace")
    except Exception:
        zf.close()
        return {}
    zf.close()

    # XML パースしてラベルを取得
    labels: dict[str, str] = {}
    try:
        from src.segment.xbrl_segment_extractor import _camel_to_readable
        from src.segment.normalize import normalize_segment_name

        root = ET.fromstring(content)
        for elem in root.iter():
            tag = elem.tag
            if not isinstance(tag, str):
                continue
            if "label" not in tag.lower() or "}" not in tag:
                continue
            text = (elem.text or "").strip()
            if not text or len(text) >= 50:
                continue
            label_id = elem.get("{http://www.w3.org/1999/xlink}label", "")
            if not label_id or "Segment" not in label_id:
                continue

            # 冗長ラベル除去（「報告セグメント [メンバー]」等）
            if "メンバー" in text or "[メンバー]" in text:
                continue

            # パターン1: label_CamelNameReportableSegmentsMember
            # パターン2: label_tse-xxx-12340CamelNameReportableSegmentsMember
            member_patterns = [
                re.compile(r"label_(\w+?)(?:Reportable|Operating|Business)?Segments?Member", re.IGNORECASE),
                re.compile(r"\d+0?(\w+?)(?:Reportable|Operating|Business)?Segments?Member", re.IGNORECASE),
            ]
            for pat in member_patterns:
                m = pat.search(label_id)
                if m:
                    eng_name = m.group(1)
                    # 余分なprefix除去 (tse-xxx-等が混入した場合)
                    if "-" in eng_name:
                        parts = eng_name.split("-")
                        eng_name = parts[-1]
                    readable = _camel_to_readable(eng_name)
                    normalized = normalize_segment_name(readable) or readable
                    # 短い日本語ラベルを優先
                    existing = labels.get(normalized, "")
                    if not existing or len(text) < len(existing):
                        labels[normalized] = text
                    break
    except Exception as e:
        logger.debug(f"[LABELS] lab.xml parse error: {e}")

    if labels:
        logger.info(f"[LABELS] Japanese labels: {labels}")
    return labels


# ============================================================
# セグメントHTMLテーブルから日本語名を取得
# ============================================================

def _extract_japanese_labels_from_segment_html(xbrl_path: str) -> dict[str, str]:
    """セグメントHTMLのテーブル行ヘッダーから日本語名を取得。

    テーブルの最初のカラムにセグメント名が並ぶパターンを利用。
    contextRef のmember順序と対応付け。

    Returns
    -------
    dict
        {英語readable名: 日本語ラベル}
    """
    try:
        raw = Path(xbrl_path).read_bytes()
        if raw[:4] != _ZIP_SIGNATURE:
            return {}
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        return {}

    from src.segment.xbrl_segment_extractor import (
        _find_segment_files, _extract_segment_member, _camel_to_readable,
    )
    from src.segment.normalize import normalize_segment_name

    seg_files = _find_segment_files(zf)
    if not seg_files:
        zf.close()
        return {}

    try:
        content = zf.read(seg_files[0])
        content = _decode_html_bytes(content)
    except Exception:
        zf.close()
        return {}
    zf.close()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")

    # Step 1: contextRef からユニークなmember名を出現順に収集
    member_order: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("ix:nonfraction"):
        ctx = tag.get("contextref", "")
        if "duration" not in ctx.lower():
            continue
        member = _extract_segment_member(ctx)
        if member and member not in seen:
            seen.add(member)
            member_order.append(member)

    # Step 2: テーブルの最初の列からセグメント日本語名を収集
    # 各テーブルの最初のrow群の最初のcellがセグメント名の可能性
    # ※表項目名（売上高、営業利益等）はセグメント名ではないので除外
    _TABLE_HEADER_EXCLUSIONS = {
        "売上高", "外部顧客への売上高", "売上高顧客との契約から生じる収益",
        "顧客との契約から生じる収益", "営業利益", "営業損失",
        "セグメント利益", "セグメント損失", "セグメント利益又は損失",
        "経常利益", "経常損失", "経常利益又は経常損失",
        "セグメント間の内部売上高又は振替高", "セグメント間の内部売上高",
        "内部売上高又は振替高", "内部売上高",
        "計", "合計", "報告セグメント計",
        "調整額", "消去又は全社", "消去", "全社",
    }
    ja_names: list[str] = []
    tables = soup.find_all("table")
    if tables:
        first_table = tables[0]
        for row in first_table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if cells:
                text = cells[0].get_text(strip=True)
                # セグメント名らしいものだけ採用（短くて日本語含む）
                if text and 3 <= len(text) <= 25 and re.search(r"[\u3040-\u9fff]", text):
                    # 表項目名を除外
                    if text in _TABLE_HEADER_EXCLUSIONS:
                        continue
                    # 「売上」「利益」「損失」で始まるものも除外
                    if re.match(r"^(売上|利益|損失|収益|費用|原価)", text):
                        continue
                    ja_names.append(text)

    # Step 3: member順 と 日本語名順 を対応付け
    labels: dict[str, str] = {}
    if member_order and ja_names:
        for i, member in enumerate(member_order):
            if i < len(ja_names):
                readable = _camel_to_readable(member)
                normalized = normalize_segment_name(readable) or readable
                labels[normalized] = ja_names[i]

    if labels:
        logger.info(f"[LABELS_HTML] Segment labels from HTML: {labels}")
    return labels


# ============================================================
# セグメント絞り込み
# ============================================================

def _filter_key_segments(
    segments: list,
    max_segments: int = 5,
) -> list:
    """主要セグメントのみ残す。

    フィルタルール:
    - 'Other' / 'その他' は削除（全社のYOY変動に大きく寄与していない場合）
    - 売上が全社の5%未満のセグメントは除外
    - 最大max_segments件に絞る（YOY絶対値の大きい順）
    """
    if len(segments) <= max_segments:
        # Other だけ除外
        return [
            s for s in segments
            if s.name.lower() not in ("other", "その他", "その他の事業", "その他事業")
        ] or segments  # 全部除外されたら元に戻す

    # 総売上を推定
    total_sales = sum(abs(s.sales_current or 0) for s in segments)

    filtered = []
    for s in segments:
        # Other 系は除外
        if s.name.lower() in ("other", "その他", "その他の事業", "その他事業"):
            continue
        # 極端に小さいセグメントは除外（全社の5%未満）
        if total_sales > 0 and s.sales_current is not None:
            share = abs(s.sales_current) / total_sales
            if share < 0.05:
                continue
        filtered.append(s)

    if not filtered:
        return segments[:max_segments]

    # YOY絶対値の大きい順でソート
    def sort_key(s):
        yoy_abs = 0
        if s.sales_yoy is not None:
            yoy_abs += abs(s.sales_yoy)
        if s.profit_yoy is not None:
            yoy_abs += abs(s.profit_yoy)
        return yoy_abs

    filtered.sort(key=sort_key, reverse=True)
    return filtered[:max_segments]
# セグメント抽出（当期 + 前期 → YOY）
# ============================================================

def _extract_segments_from_zip(xbrl_path: str) -> list[SegmentFinancials]:
    """XBRL ZIPからセグメント別の当期/前期売上・利益を抽出。

    xbrl_segment_extractor.py のロジックを活用しつつ、
    前期データも同時抽出してYOY計算用の構造を返す。
    """
    try:
        from src.segment.xbrl_segment_extractor import (
            _find_segment_files,
            _extract_segment_member,
            _camel_to_readable,
            ALL_SALES_TAGS,
            ALL_PROFIT_TAGS,
            _COMPANY_SALES_SUFFIXES,
            _COMPANY_PROFIT_SUFFIXES,
            _parse_ixbrl_number,
            _detect_unit_from_html,
            _to_million_yen,
        )
        from src.segment.normalize import normalize_segment_name, classify_special_row
    except ImportError as e:
        logger.warning(f"[SEGMENT] import failed: {e}")
        return []

    try:
        raw = Path(xbrl_path).read_bytes()
        if raw[:4] != _ZIP_SIGNATURE:
            return []
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except Exception:
        return []

    try:
        seg_files = _find_segment_files(zf)
    except Exception:
        zf.close()
        return []

    if not seg_files:
        zf.close()
        return []

    # セグメント別: {member_name: {period: {sales, profit}}}
    seg_data: dict[str, dict[str, dict[str, int | None]]] = {}

    for seg_file in seg_files:
        try:
            content = zf.read(seg_file).decode("utf-8", errors="replace")
        except Exception:
            continue

        unit = _detect_unit_from_html(content)

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")

        for tag in soup.find_all("ix:nonfraction"):
            name_attr = (tag.get("name") or "").lower()
            ctx = tag.get("contextref", "")
            sign = tag.get("sign")
            text = tag.get_text(strip=True)

            if not ctx:
                continue

            # Duration のみ
            if "duration" not in ctx.lower():
                continue

            # セグメントmember抽出
            member = _extract_segment_member(ctx)
            if not member:
                continue

            # 当期/前期判定
            ctx_lower = ctx.lower()
            if "prior" in ctx_lower:
                period_key = "prior"
            elif "current" in ctx_lower or "interim" in ctx_lower:
                period_key = "current"
            else:
                continue

            # 売上/利益判定
            is_sales = name_attr in ALL_SALES_TAGS
            is_profit = name_attr in ALL_PROFIT_TAGS

            if not is_sales and not is_profit:
                local_name = name_attr.split(":")[-1] if ":" in name_attr else name_attr
                if any(local_name.endswith(s) for s in _COMPANY_SALES_SUFFIXES):
                    is_sales = True
                elif any(local_name.endswith(s) for s in _COMPANY_PROFIT_SUFFIXES):
                    is_profit = True

            if not is_sales and not is_profit:
                continue

            value = _parse_ixbrl_number(text, sign)
            if value is None:
                continue
            value = _to_million_yen(value, unit)

            # 格納
            readable = _camel_to_readable(member)
            normalized = normalize_segment_name(readable) or readable

            # 特殊行（合計・調整額・全社）は除外。ordinary_segment は通常セグメント=残す
            special = classify_special_row(normalized)
            if special and special != "ordinary_segment":
                continue

            if normalized not in seg_data:
                seg_data[normalized] = {"current": {}, "prior": {}}

            field = "sales" if is_sales else "profit"
            # 外部顧客売上を優先（既存値を上書き）
            if field not in seg_data[normalized][period_key] or (
                is_sales and "externalcustomer" in name_attr
            ):
                seg_data[normalized][period_key][field] = value

    zf.close()

    # SegmentFinancials に変換
    result: list[SegmentFinancials] = []
    for seg_name, periods in seg_data.items():
        cur = periods.get("current", {})
        pri = periods.get("prior", {})

        # 売上も利益も全くないセグメントは除外
        if not cur and not pri:
            continue

        sf = SegmentFinancials(
            name=seg_name,
            sales_current=cur.get("sales"),
            sales_prior=pri.get("sales"),
            profit_current=cur.get("profit"),
            profit_prior=pri.get("profit"),
        )
        # YOYが1つでも計算できるセグメントのみ含める
        if sf.sales_yoy is not None or sf.profit_yoy is not None:
            result.append(sf)

    # 売上高の大きい順にソート
    result.sort(key=lambda s: s.sales_current or 0, reverse=True)

    return result
