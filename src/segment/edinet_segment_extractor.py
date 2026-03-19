"""
EDINET Segment Extractor — 法定開示 XBRL からセグメント売上・利益を抽出

EDINET 有価証券報告書・半期報告書の XBRL instance から
BusinessSegmentAxis / OperatingSegmentsAxis を軸にセグメント情報を抽出する。

TDnet iXBRL 抽出器 (xbrl_segment_extractor.py) とは独立した実装。
低レベルの数値パース・名称正規化は既存モジュールを流用可。
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("edinet_seg")

# ============================================================
# Data Models
# ============================================================

@dataclass
class EdinetSegmentRecord:
    """EDINET から抽出した1セグメント行。"""
    segment_name_raw: str = ""
    segment_name_norm: str = ""
    sales: Optional[int] = None          # 百万円
    profit: Optional[int] = None         # 百万円
    sales_ytd: Optional[int] = None
    profit_ytd: Optional[int] = None
    sales_qtd: Optional[int] = None
    profit_qtd: Optional[int] = None
    derivation_method: str = ""          # reported_qtd / derived_from_ytd_diff / ytd_only
    axis_name: str = ""
    member_name: str = ""
    role_name: str = ""
    concept_sales: str = ""
    concept_profit: str = ""
    context_sales: str = ""
    context_profit: str = ""
    special_row_type: str = "ordinary_segment"  # ordinary_segment / adjustment / total / corporate / other


@dataclass
class EdinetSegmentResult:
    """セグメント抽出結果。"""
    status: str = "error"                # ok / no_segments / error
    ticker: str = ""
    doc_type: str = ""                   # securities_report / semiannual_report
    period: str = ""                     # YYYY-MM-DD (期末日)
    quarter: str = ""                    # FY / 1H
    source_type: str = "edinet_xbrl"
    segments: list[EdinetSegmentRecord] = field(default_factory=list)
    review_hint: str = ""
    debug_summary: dict = field(default_factory=dict)


# ============================================================
# Namespace constants
# ============================================================
_NS = {
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xbrldi": "http://xbrl.org/2006/xbrldi",
    "link": "http://www.xbrl.org/2003/linkbase",
    "xlink": "http://www.w3.org/1999/xlink",
}

# ============================================================
# Segment Axis detection
# ============================================================
# EDINET では jpcrp_cor:OperatingSegmentsAxis が事業セグメント軸。
# BusinessSegmentAxis は TDnet iXBRL の context 内 member suffix に現れる。
# 両方を検出対象にする。
_SEGMENT_AXIS_PATTERNS = [
    "OperatingSegmentsAxis",
    "BusinessSegmentsAxis",
    "BusinessSegmentAxis",
    "ReportableSegmentsAxis",
]

# 除外する軸 (Phase 2 で別扱い)
_GEO_AXIS_PATTERNS = [
    "GeographicalSegmentsAxis",
    "GeographicalSegmentAxis",
    "GeographicAxis",
]


def _is_segment_axis(axis_name: str) -> bool:
    """axis 名がセグメント軸かどうか判定。"""
    local = axis_name.split(":")[-1] if ":" in axis_name else axis_name
    return any(pat in local for pat in _SEGMENT_AXIS_PATTERNS)


def _is_geo_axis(axis_name: str) -> bool:
    """axis 名が地理的軸かどうか判定。"""
    local = axis_name.split(":")[-1] if ":" in axis_name else axis_name
    return any(pat in local for pat in _GEO_AXIS_PATTERNS)


# ============================================================
# Concept classification
# ============================================================
_SALES_CONCEPTS = {
    # JP 基準
    "NetSales", "Sales", "Revenue", "OperatingRevenue",
    "RevenuesFromExternalCustomers", "NetSalesToExternalCustomers",
    "SalesToExternalCustomers",
    # 銀行・金融
    "NetRevenue", "OrdinaryRevenue",
    # IFRS
    "RevenueIFRS", "SalesToExternalCustomersIFRS",
    "RevenueFromExternalCustomersIFRS", "NetSalesIFRS",
}

_PROFIT_CONCEPTS = {
    # JP 基準
    "OperatingIncome", "OperatingProfit", "OrdinaryIncome",
    "SegmentProfitLoss", "SegmentIncome", "SegmentProfit", "SegmentLoss",
    "ProfitLoss",
    # IFRS (拡充: 総合商社・IFRS企業対応)
    "OperatingProfitLossIFRS", "BusinessProfitLossIFRS",
    "SegmentProfitLossIFRS", "ProfitLossBeforeTaxIFRS",
    "ProfitLossIFRS",
    # IFRS 追加 — 総合商社で使われるセグメント利益概念
    "ProfitLossOfOperatingSegments",
    "SegmentProfitLossOfOperatingSegments",
    "ProfitLossAttributableToOwnersOfParent",
    "ProfitLossAttributableToOwnersOfParentIFRS",
    "GrossProfitIFRS", "GrossProfit",
    "BusinessProfit", "CoreOperatingProfit",
    "CoreOperatingProfitIFRS",
    "ProfitLossBeforeTax",
}

# 優先度: 外部顧客売上 > NetSales (NetSales はセグメント間含む場合あり)
_SALES_PRIORITY = {
    "RevenuesFromExternalCustomers": 100,
    "SalesToExternalCustomers": 100,
    "RevenueFromExternalCustomersIFRS": 100,
    "SalesToExternalCustomersIFRS": 100,
    "NetSalesToExternalCustomers": 95,
    "NetSales": 80,
    "Revenue": 70,
    "RevenueIFRS": 70,
    "NetSalesIFRS": 80,
    "NetRevenue": 75,
    "OrdinaryRevenue": 65,
    "Sales": 60,
    "OperatingRevenue": 50,
}

_PROFIT_PRIORITY = {
    "SegmentProfitLoss": 100,
    "SegmentProfitLossIFRS": 100,
    "SegmentProfitLossOfOperatingSegments": 95,
    "ProfitLossOfOperatingSegments": 95,
    "OperatingIncome": 90,
    "OperatingProfit": 90,
    "OperatingProfitLossIFRS": 90,
    "BusinessProfitLossIFRS": 85,
    "BusinessProfit": 85,
    "CoreOperatingProfit": 85,
    "CoreOperatingProfitIFRS": 85,
    "ProfitLoss": 70,
    "ProfitLossIFRS": 70,
    "ProfitLossBeforeTaxIFRS": 60,
    "ProfitLossBeforeTax": 60,
    "GrossProfit": 55,
    "GrossProfitIFRS": 55,
    "OrdinaryIncome": 50,
    "SegmentIncome": 100,
    "SegmentProfit": 100,
    "SegmentLoss": 100,
    "ProfitLossAttributableToOwnersOfParent": 65,
    "ProfitLossAttributableToOwnersOfParentIFRS": 65,
}


def classify_concept(qname: str) -> tuple[str, int]:
    """concept QName を sales/profit/other に分類する。

    Args:
        qname: concept のローカル名 (namespace 除去済)

    Returns:
        (classification, priority)
        classification: "sales" / "profit" / "other"
        priority: 優先度 (0-100)
    """
    local = qname.split(":")[-1] if ":" in qname else qname

    if local in _SALES_CONCEPTS:
        return "sales", _SALES_PRIORITY.get(local, 50)
    if local in _PROFIT_CONCEPTS:
        return "profit", _PROFIT_PRIORITY.get(local, 50)

    # ラベルベースの fallback (ローカル名の部分一致)
    lower = local.lower()
    if any(kw in lower for kw in ["externalsales", "externalcustomer", "externrevenue"]):
        return "sales", 40
    if any(kw in lower for kw in ["netsales", "revenue", "sales"]) and "intersegment" not in lower:
        return "sales", 30
    if any(kw in lower for kw in ["segmentprofit", "operatingincome", "operatingprofit"]):
        return "profit", 30

    return "other", 0


# ============================================================
# Member name extraction & normalization
# ============================================================
_MEMBER_SUFFIX_RE = re.compile(
    r"(Reportable)?Segments?Member$|OperatingSegments?Member$|"
    r"Member$|AxisMember$",
    re.IGNORECASE,
)

_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# メタ/調整 member の判定
_META_MEMBERS = {
    "ReportableSegmentsMember",
    "ReconcilingItemsMember",
    "UnallocatedAmountsAndEliminationMember",
    "CorporateSharedMember",
    "OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember",
    "OtherOperatingSegmentsAxisMember",
    "OtherReportableSegmentsMember",
    "TotalOfReportableSegmentsMember",
    "TotalReportableSegmentsMember",
}

# total として確実に分類すべき member 名
_TOTAL_MEMBER_NAMES = {
    "ReportableSegmentsMember",
    "TotalOfReportableSegmentsMember",
    "TotalReportableSegmentsMember",
    "TotalOperatingSegmentsMember",
    "ReportableSegmentsTotalMember",
    "SegmentsTotalMember",
    "TotalMember",
    "AllSegmentsTotalMember",
}

# adjustment として分類すべき member 名
_ADJUSTMENT_MEMBER_NAMES = {
    "ReconcilingItemsMember",
    "UnallocatedAmountsAndEliminationMember",
    "EliminationsMember",
    "EliminationsAndReconcilingItemsMember",
    "IntersegmentEliminationMember",
    "AdjustmentMember",
}

# corporate
_CORPORATE_MEMBER_NAMES = {
    "CorporateSharedMember",
    "CorporateMember",
    "HeadOfficeMember",
}


def _classify_member(member_name: str, label_name: str = "") -> str:
    """member 名から特殊行タイプを分類。

    判定優先順位:
    1. member QName の完全一致
    2. member QName のパターンマッチ (suffix)
    3. 日本語ラベルベース
    4. member QName の部分一致
    5. fallback → ordinary_segment
    """
    local = member_name.split(":")[-1] if ":" in member_name else member_name

    # --- 1. 完全一致 ---
    if local in _TOTAL_MEMBER_NAMES:
        return "total"
    if local in _ADJUSTMENT_MEMBER_NAMES:
        return "adjustment"
    if local in _CORPORATE_MEMBER_NAMES:
        return "corporate"
    if local in ("OtherReportableSegmentsMember", "OtherOperatingSegmentsAxisMember"):
        return "other"

    # --- 2. suffix / パターンマッチ ---
    if any(local.endswith(sfx) for sfx in (
        "TotalOfReportableSegmentsMember",
        "TotalMember",
        "ReportableSegmentsTotalMember",
    )):
        return "total"
    if local.endswith("ReconcilingItemsMember") or local.endswith("EliminationsMember"):
        return "adjustment"
    if "NotIncludedInReportable" in local:
        return "other"

    # --- 3. 日本語ラベルベース (拡充) ---
    if label_name:
        label_norm = label_name.strip()
        # total — exact
        if label_norm in (
            "合計", "計", "全社合計", "セグメント合計",
            "報告セグメント計", "事業セグメント合計",
            "報告セグメント合計", "セグメント計",
        ):
            return "total"
        # total — endswith
        if label_norm.endswith("合計") or label_norm.endswith("セグメント計"):
            return "total"
        # total — contains
        if "報告セグメント" in label_norm and ("合計" in label_norm or "計" in label_norm):
            return "total"
        # subtotal
        if label_norm.endswith("小計") or label_norm.endswith("部門計"):
            return "subtotal"
        # adjustment
        if label_norm in (
            "調整額", "全社及び消去", "消去又は全社", "消去",
            "消去・全社", "調整", "全社・消去",
            "セグメント間取引消去",
        ):
            return "adjustment"
        if label_norm.endswith("調整額") or label_norm.endswith("消去額"):
            return "adjustment"
        # corporate
        if label_norm in ("全社", "本社", "全社共通", "本社・全社"):
            return "corporate"
        # other
        if label_norm in ("その他",) or label_norm.startswith("その他"):
            return "other"

    # --- 4. member QName 部分一致 (最終手段) ---
    if "TotalOfReportableSegments" in local or "TotalOfOperatingSegments" in local:
        return "total"
    if "Reconciling" in local or "Elimination" in local:
        return "adjustment"
    if "Corporate" in local and ("Shared" in local or "Common" in local):
        return "corporate"

    return "ordinary_segment"


def _member_to_segment_name(member_name: str) -> str:
    """member 名からセグメント名を生成。

    例:
        "jpcrp030000-asr_E01737-000DigitalSystemsAndServicesReportableSegmentMember"
        → "Digital Systems And Services"
    """
    local = member_name.split(":")[-1] if ":" in member_name else member_name

    # 企業固有のプレフィックスを除去 (jpcrp030000-asr_EXXXXX-000 パターン)
    local = re.sub(r"^jpcrp\d+-\w+_E\d+-\d+", "", local)

    # Member サフィックスを除去
    local = _MEMBER_SUFFIX_RE.sub("", local)

    # CamelCase を分割
    parts = _CAMEL_SPLIT.split(local)
    name = " ".join(parts).strip()

    return name


def _normalize_segment_name(raw_name: str) -> str:
    """セグメント名を正規化。"""
    if not raw_name:
        return ""
    s = unicodedata.normalize("NFKC", raw_name)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"セグメント$", "", s)
    # [メンバー] / [member] suffix を除去 (verbose label 由来)
    s = re.sub(r"[、,]?\s*報告セグメント\s*\[メンバー\]$", "", s)
    s = re.sub(r"\s*\[メンバー\]$", "", s)
    s = re.sub(r"\s*\[member\]$", "", s, flags=re.IGNORECASE)
    return s.strip()


# ============================================================
# Label Linkbase Parser (Phase 2)
# ============================================================
_NS_XLINK = "http://www.w3.org/1999/xlink"
_LABEL_ROLE_STANDARD = "http://www.xbrl.org/2003/role/label"
_LABEL_ROLE_VERBOSE = "http://www.xbrl.org/2003/role/verboseLabel"


def _parse_label_linkbase(zf: zipfile.ZipFile, lang: str = "jp") -> dict[str, str]:
    """ZIP 内のラベルリンクベースから concept_id → ラベルテキストのマッピングを生成。

    Args:
        zf: 開いた ZipFile
        lang: "jp" (日本語) or "en" (英語)

    Returns:
        {concept_id: label_text}
        concept_id は href の # 以降の部分
        label_text は standard label (role/label) を優先
    """
    suffix = "_lab.xml" if lang == "jp" else "_lab-en.xml"
    lab_files = [
        n for n in zf.namelist()
        if n.endswith(suffix) and "publicdoc" in n.lower()
    ]
    if not lab_files:
        return {}

    result: dict[str, str] = {}

    for lf in lab_files:
        try:
            content = zf.read(lf).decode("utf-8", errors="replace")
            root = ET.fromstring(content)
        except Exception:
            continue

        locs: dict[str, str] = {}      # xlink:label → href
        labels: dict[str, tuple[str, str]] = {}  # xlink:label → (text, role)
        arcs: list[tuple[str, str]] = []

        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "loc":
                locs[elem.get(f"{{{_NS_XLINK}}}label", "")] = elem.get(f"{{{_NS_XLINK}}}href", "")
            elif local == "label":
                labels[elem.get(f"{{{_NS_XLINK}}}label", "")] = (
                    elem.text or "",
                    elem.get(f"{{{_NS_XLINK}}}role", ""),
                )
            elif local == "labelArc":
                arcs.append((
                    elem.get(f"{{{_NS_XLINK}}}from", ""),
                    elem.get(f"{{{_NS_XLINK}}}to", ""),
                ))

        # Build concept_id → label mapping (standard label を優先)
        concept_all_labels: dict[str, dict[str, str]] = {}  # concept_id → {role: text}
        for from_, to_ in arcs:
            if from_ in locs and to_ in labels:
                href = locs[from_]
                concept_id = href.split("#")[-1] if "#" in href else href
                label_text, role = labels[to_]
                if concept_id not in concept_all_labels:
                    concept_all_labels[concept_id] = {}
                role_short = role.split("/")[-1] if "/" in role else role
                concept_all_labels[concept_id][role_short] = label_text

        for concept_id, role_labels in concept_all_labels.items():
            # standard label を優先、なければ verbose label、なければ最初のもの
            text = (
                role_labels.get("label")
                or role_labels.get("verboseLabel")
                or next(iter(role_labels.values()), "")
            )
            if text:
                result[concept_id] = text

    return result


def _resolve_member_label(
    member_qname: str,
    label_map: dict[str, str],
) -> str:
    """member QName からラベルリンクベースの正式名称を取得。

    取得できない場合は空文字を返す (caller が CamelCase fallback を使う)。
    """
    # member QName の形式:
    # context: jpcrp030000-asr_E02248-000_JapanReportableSegmentsMember  (概念名 = label key)
    # fact:    jpcrp_cor:JapanReportableSegmentsMember (名前空間付き)
    local = member_qname.split(":")[-1] if ":" in member_qname else member_qname

    # 直接一致
    if local in label_map:
        return label_map[local]

    # ラベルリンクの key は企業固有プレフィックス付き:
    # jpcrp030000-asr_E02248-000_JapanReportableSegmentsMember
    for key, label in label_map.items():
        if key.endswith(local) or key.endswith(f"_{local}"):
            return label

    return ""


# ============================================================
# Role Filter (Phase 2)
# ============================================================
_SEGMENT_ROLE_KEYWORDS = [
    "SegmentInformation",
    "segmentinformation",
    "セグメント情報",
]


def _find_segment_roles(zf: zipfile.ZipFile) -> list[str]:
    """定義リンクベースからセグメント情報関連の role URI を抽出。

    Returns:
        segment 関連の role URI リスト
    """
    def_files = [
        n for n in zf.namelist()
        if n.endswith("_def.xml") and "publicdoc" in n.lower()
    ]
    roles: list[str] = []

    for df in def_files:
        try:
            content = zf.read(df).decode("utf-8", errors="replace")
            root = ET.fromstring(content)
        except Exception:
            continue

        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "definitionLink":
                role = elem.get(f"{{{_NS_XLINK}}}role", "")
                if any(kw.lower() in role.lower() for kw in _SEGMENT_ROLE_KEYWORDS):
                    roles.append(role)

    return roles


# ============================================================
# YTD → Quarter Derivation (Phase 3)
# ============================================================
def derive_quarter_values(
    records_by_quarter: dict[str, list[EdinetSegmentRecord]],
    target_quarter: str,
) -> list[EdinetSegmentRecord]:
    """YTD 累計値から単四半期値を導出する。

    Args:
        records_by_quarter: {quarter: records} 例: {"1Q": [...], "2Q": [...]}
        target_quarter: 導出対象の四半期 ("1Q", "2Q", "3Q")

    Returns:
        target_quarter の records に qtd 値を設定したリスト

    ルール:
        1Q: YTD をそのまま quarter_value とみなす
        2Q: 2Q累計 - 1Q累計
        3Q: 3Q累計 - 2Q累計
        FY: annual 扱い (YTD→単Q変換しない)
    """
    target_records = records_by_quarter.get(target_quarter, [])
    if not target_records:
        return []

    # 前の四半期を特定
    prior_map = {"2Q": "1Q", "3Q": "2Q"}
    prior_q = prior_map.get(target_quarter)

    if target_quarter == "1Q":
        # 1Q: YTD = QTD
        for rec in target_records:
            rec.sales_ytd = rec.sales
            rec.profit_ytd = rec.profit
            rec.sales_qtd = rec.sales
            rec.profit_qtd = rec.profit
            rec.derivation_method = "reported_qtd"
        return target_records

    if target_quarter == "FY" or target_quarter == "1H":
        # FY/1H: annual/半期扱い — YTD→単Q変換しない
        for rec in target_records:
            rec.sales_ytd = rec.sales
            rec.profit_ytd = rec.profit
            rec.sales_qtd = rec.sales
            rec.profit_qtd = rec.profit
            rec.derivation_method = "reported_qtd"
        return target_records

    if not prior_q or prior_q not in records_by_quarter:
        # 前四半期データなし — YTD のみ保持
        for rec in target_records:
            rec.sales_ytd = rec.sales
            rec.profit_ytd = rec.profit
            rec.sales_qtd = None
            rec.profit_qtd = None
            rec.derivation_method = "ytd_only"
            rec.role_name = rec.role_name or "edinet_ytd_only_missing_prior_quarter"
        return target_records

    # 前四半期の records をセグメント名で indexed
    prior_records = records_by_quarter[prior_q]
    prior_by_name: dict[str, EdinetSegmentRecord] = {
        r.segment_name_norm: r for r in prior_records
    }

    for rec in target_records:
        rec.sales_ytd = rec.sales
        rec.profit_ytd = rec.profit
        prior = prior_by_name.get(rec.segment_name_norm)
        if prior:
            rec.sales_qtd = (
                (rec.sales - prior.sales) if rec.sales is not None and prior.sales is not None else None
            )
            rec.profit_qtd = (
                (rec.profit - prior.profit) if rec.profit is not None and prior.profit is not None else None
            )
            rec.derivation_method = "derived_from_ytd_diff"
        else:
            rec.sales_qtd = None
            rec.profit_qtd = None
            rec.derivation_method = "ytd_only"

    return target_records


# ============================================================
# Context parsing
# ============================================================
@dataclass
class ParsedContext:
    """XBRL context の解析結果。"""
    ctx_id: str = ""
    period_type: str = ""     # "duration" / "instant"
    start_date: str = ""
    end_date: str = ""
    instant_date: str = ""
    dimensions: list[dict] = field(default_factory=list)  # [{"axis": ..., "member": ...}]
    has_segment_axis: bool = False
    segment_axis_name: str = ""
    segment_member_name: str = ""
    is_current_period: bool = False
    is_prior_period: bool = False


def _parse_contexts(root: ET.Element) -> dict[str, ParsedContext]:
    """XBRL instance の全 context を解析。"""
    contexts: dict[str, ParsedContext] = {}

    for ctx_elem in root.findall(".//xbrli:context", _NS):
        ctx_id = ctx_elem.get("id", "")
        pc = ParsedContext(ctx_id=ctx_id)

        # Period
        period_elem = ctx_elem.find("xbrli:period", _NS)
        if period_elem is not None:
            instant = period_elem.find("xbrli:instant", _NS)
            start = period_elem.find("xbrli:startDate", _NS)
            end = period_elem.find("xbrli:endDate", _NS)
            if instant is not None:
                pc.period_type = "instant"
                pc.instant_date = instant.text or ""
            elif start is not None and end is not None:
                pc.period_type = "duration"
                pc.start_date = start.text or ""
                pc.end_date = end.text or ""

        # Dimensions (scenario)
        for scenario in ctx_elem.findall(".//xbrli:scenario", _NS):
            for em in scenario.findall("xbrldi:explicitMember", _NS):
                dim_axis = em.get("dimension", "")
                dim_member = em.text or ""
                pc.dimensions.append({"axis": dim_axis, "member": dim_member})

                if _is_segment_axis(dim_axis):
                    pc.has_segment_axis = True
                    pc.segment_axis_name = dim_axis
                    pc.segment_member_name = dim_member

        # Current / Prior 判定
        ctx_lower = ctx_id.lower()
        if "prior" in ctx_lower:
            pc.is_prior_period = True
        elif "current" in ctx_lower:
            pc.is_current_period = True
        else:
            # context ID にヒントがない場合は期間で判定 (将来拡張)
            pc.is_current_period = True  # デフォルトは当期

        contexts[ctx_id] = pc

    return contexts


# ============================================================
# XBRL instance file detection in ZIP
# ============================================================
def _find_xbrl_instance(zf: zipfile.ZipFile) -> Optional[str]:
    """EDINET ZIP から XBRL instance ファイルを検出。

    優先順:
    1. XBRL/PublicDoc/*.xbrl (pure XBRL instance)
    2. XBRL/PublicDoc/*.htm / *.html (iXBRL fallback)
       - label/cal/def/pre 等の補助ファイルは除外
       - 最大サイズのファイルを選択

    AuditDoc, manifest, label/cal/def/pre リンクは除外。
    """
    candidates_xbrl = []
    candidates_ixbrl = []

    _EXCLUDE_SUFFIXES = (
        "_lab.xml", "_lab-en.xml", "_cal.xml", "_def.xml", "_pre.xml",
        "manifest", "_lab_", "_cal_", "_def_", "_pre_",
    )

    for name in zf.namelist():
        lower = name.lower()
        # PublicDoc 配下のみ対象
        if "publicdoc" not in lower:
            continue
        # 補助ファイルを除外
        if any(sfx in lower for sfx in _EXCLUDE_SUFFIXES):
            continue
        # 画像・CSS・JS を除外
        if any(lower.endswith(ext) for ext in (".png", ".gif", ".jpg", ".css", ".js", ".xsd")):
            continue

        if name.endswith(".xbrl"):
            candidates_xbrl.append(name)
        elif lower.endswith((".htm", ".html")):
            candidates_ixbrl.append(name)

    # Pure XBRL instance を優先
    if candidates_xbrl:
        return max(candidates_xbrl, key=lambda n: zf.getinfo(n).file_size)

    # iXBRL fallback — 最大サイズの HTM/HTML を選択
    if candidates_ixbrl:
        return max(candidates_ixbrl, key=lambda n: zf.getinfo(n).file_size)

    return None


# ============================================================
# Fact extraction
# ============================================================
def _extract_segment_facts(
    root: ET.Element,
    contexts: dict[str, ParsedContext],
) -> list[dict]:
    """XBRL instance から segment axis を持つ fact を抽出。

    Returns:
        list of dicts:
            concept, ctx_id, value, decimals, unit_ref,
            segment_member, classification, priority
    """
    segment_ctx_ids = {
        cid for cid, pc in contexts.items()
        if pc.has_segment_axis and pc.period_type == "duration" and pc.is_current_period
    }

    facts = []
    for elem in root:
        tag = elem.tag
        # Skip XBRL infrastructure elements
        if tag.startswith("{http://www.xbrl.org/2003/") or tag.startswith("{http://www.xbrl.org/2003/linkbase}"):
            continue

        ctx_ref = elem.get("contextRef", "")
        if not ctx_ref or ctx_ref not in segment_ctx_ids:
            continue

        # Extract local concept name
        if "}" in tag:
            _, local = tag.rsplit("}", 1)
        else:
            local = tag

        classification, priority = classify_concept(local)
        if classification == "other":
            continue

        # Parse value
        raw_val = (elem.text or "").strip()
        val = _parse_xbrl_value(raw_val)

        pc = contexts[ctx_ref]
        facts.append({
            "concept": local,
            "ctx_id": ctx_ref,
            "value": val,
            "raw_value": raw_val,
            "decimals": elem.get("decimals", ""),
            "unit_ref": elem.get("unitRef", ""),
            "member": pc.segment_member_name,
            "axis": pc.segment_axis_name,
            "classification": classification,
            "priority": priority,
        })

    return facts


def _parse_xbrl_value(text: str) -> Optional[int]:
    """XBRL fact のテキスト値を整数にパース。"""
    if not text or text.strip() in ("", "-", "－", "―"):
        return None
    s = text.strip().replace(",", "").replace(" ", "")
    try:
        return int(float(s))
    except (ValueError, OverflowError):
        return None


def _yen_to_million(val: Optional[int]) -> Optional[int]:
    """円を百万円に変換。"""
    if val is None:
        return None
    return val // 1_000_000


# ============================================================
# iXBRL (inline XBRL) パーサー
# ============================================================
def _parse_ixbrl_content(html_content: str) -> Optional[ET.Element]:
    """iXBRL (HTML with inline XBRL tags) をパースして ET root を返す。

    EDINET の iXBRL は valid XHTML (XML準拠) の場合が多いため、
    まず標準 XML パーサーで試す。失敗したら名前空間修復を試みる。

    Returns:
        ET.Element root or None
    """
    # Step 1: そのまま XML パース (EDINET iXBRL は XHTML で XML 準拠が多い)
    try:
        root = ET.fromstring(html_content)
        logger.debug("[ixbrl] parsed as XML directly")
        return root
    except ET.ParseError:
        pass

    # Step 2: HTML5 パーサーで xml 部分を抽出
    # よくある問題: HTML entities (&nbsp; etc) が XML で不正
    import re
    # &xxx; を除去 (HTML entities → XML 不整合の主因)
    cleaned = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)\w+;', ' ', html_content)
    try:
        root = ET.fromstring(cleaned)
        logger.debug("[ixbrl] parsed after HTML entity cleanup")
        return root
    except ET.ParseError:
        pass

    # Step 3: XML宣言を追加して再試行
    if not cleaned.strip().startswith("<?xml"):
        cleaned_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + cleaned
        try:
            root = ET.fromstring(cleaned_xml)
            logger.debug("[ixbrl] parsed with XML declaration added")
            return root
        except ET.ParseError:
            pass

    logger.warning("[ixbrl] failed to parse iXBRL content")
    return None


# ============================================================
# Main entry point
# ============================================================
def extract_edinet_segments(
    zip_path: str,
    *,
    ticker: str = "",
    doc_type: str = "",
    period: str = "",
    quarter: str = "",
) -> EdinetSegmentResult:
    """EDINET ZIP からセグメント売上・利益を抽出。

    Args:
        zip_path: EDINET XBRL ZIP ファイルパス
        ticker: 銘柄コード (4桁)
        doc_type: securities_report / semiannual_report
        period: 期末日 YYYY-MM-DD
        quarter: FY / 1H

    Returns:
        EdinetSegmentResult
    """
    result = EdinetSegmentResult(
        ticker=ticker, doc_type=doc_type, period=period, quarter=quarter,
    )

    if not os.path.exists(zip_path):
        result.status = "error"
        result.review_hint = "zip_not_found"
        return result

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Step 1: Find XBRL instance
            instance_name = _find_xbrl_instance(zf)
            if not instance_name:
                result.status = "error"
                result.review_hint = "no_xbrl_instance_found"
                return result

            content = zf.read(instance_name).decode("utf-8", errors="replace")

            # Step 2: Parse XBRL/iXBRL
            is_ixbrl = instance_name.lower().endswith((".htm", ".html"))
            result.debug_summary["instance_type"] = "ixbrl" if is_ixbrl else "xbrl"
            if is_ixbrl:
                logger.info(f"[edinet] ixbrl_fallback_used: {instance_name}")

            root = None
            if is_ixbrl:
                root = _parse_ixbrl_content(content)
            else:
                try:
                    root = ET.fromstring(content)
                except ET.ParseError:
                    root = None

            if root is None:
                result.status = "error"
                result.review_hint = f"{'ixbrl' if is_ixbrl else 'xbrl'}_parse_error"
                return result

            # Step 2.5: Parse label linkbase (Phase 2)
            label_map_jp = _parse_label_linkbase(zf, lang="jp")
            label_map_en = _parse_label_linkbase(zf, lang="en")
            segment_roles = _find_segment_roles(zf)

            result.debug_summary["label_map_jp_count"] = len(label_map_jp)
            result.debug_summary["label_map_en_count"] = len(label_map_en)
            result.debug_summary["segment_roles"] = segment_roles

            # Step 3: Parse contexts
            contexts = _parse_contexts(root)
            segment_ctx_count = sum(1 for pc in contexts.values() if pc.has_segment_axis)

            result.debug_summary["instance_file"] = instance_name
            result.debug_summary["total_contexts"] = len(contexts)
            result.debug_summary["segment_axis_contexts"] = segment_ctx_count

            if segment_ctx_count == 0:
                result.status = "no_segments"
                result.review_hint = "no_segments_axis_missing"
                return result

            # Step 4: Extract segment facts
            facts = _extract_segment_facts(root, contexts)
            result.debug_summary["segment_facts_count"] = len(facts)

            if not facts:
                result.status = "no_segments"
                result.review_hint = "no_segments_single_segment"
                return result

            # Step 5: Group facts by member → build segment records
            #         Use label linkbase for segment names
            records = _build_segment_records(
                facts, contexts,
                label_map_jp=label_map_jp,
                label_map_en=label_map_en,
                segment_roles=segment_roles,
            )
            result.segments = records
            result.debug_summary["segment_record_count"] = len(records)

            if records:
                result.status = "ok"
            else:
                result.status = "no_segments"
                result.review_hint = "facts_found_but_no_records_built"

    except zipfile.BadZipFile:
        result.status = "error"
        result.review_hint = "bad_zip_file"
    except Exception as e:
        result.status = "error"
        result.review_hint = f"unexpected_error: {str(e)[:200]}"
        logger.warning(f"[edinet_seg] error: {zip_path}: {e}")

    return result


def _build_segment_records(
    facts: list[dict],
    contexts: dict[str, ParsedContext],
    *,
    label_map_jp: dict[str, str] | None = None,
    label_map_en: dict[str, str] | None = None,
    segment_roles: list[str] | None = None,
) -> list[EdinetSegmentRecord]:
    """fact を member ごとにグループ化して SegmentRecord を生成。

    Phase 2: label linkbase からセグメント名を取得。
    """
    label_jp = label_map_jp or {}
    label_en = label_map_en or {}

    # Group by member
    member_facts: dict[str, list[dict]] = {}
    for f in facts:
        member = f["member"]
        if member not in member_facts:
            member_facts[member] = []
        member_facts[member].append(f)

    records = []
    for member, mfacts in member_facts.items():
        rec = EdinetSegmentRecord()
        rec.member_name = member

        # Phase 2: ラベルリンクから正式名称取得 → CamelCase fallback
        label_name = _resolve_member_label(member, label_jp)
        if label_name:
            rec.segment_name_raw = label_name
        else:
            rec.segment_name_raw = _member_to_segment_name(member)

        rec.segment_name_norm = _normalize_segment_name(rec.segment_name_raw)
        rec.special_row_type = _classify_member(member, label_name=rec.segment_name_norm)
        rec.axis_name = mfacts[0]["axis"] if mfacts else ""

        # Phase 2: role name
        if segment_roles:
            rec.role_name = segment_roles[0]  # primary segment role

        # Pick best sales and profit facts by priority
        best_sales = None
        best_profit = None

        for f in mfacts:
            if f["classification"] == "sales":
                if best_sales is None or f["priority"] > best_sales["priority"]:
                    best_sales = f
            elif f["classification"] == "profit":
                if best_profit is None or f["priority"] > best_profit["priority"]:
                    best_profit = f

        if best_sales:
            rec.sales = _yen_to_million(best_sales["value"])
            rec.concept_sales = best_sales["concept"]
            rec.context_sales = best_sales["ctx_id"]
        if best_profit:
            rec.profit = _yen_to_million(best_profit["value"])
            rec.concept_profit = best_profit["concept"]
            rec.context_profit = best_profit["ctx_id"]

        # Phase 3: annual の場合は derivation_method = reported_qtd
        rec.derivation_method = "reported_qtd"

        # Only include if we have at least sales or profit
        if rec.sales is not None or rec.profit is not None:
            records.append(rec)

    return records
