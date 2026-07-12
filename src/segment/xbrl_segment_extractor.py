"""
XBRL (iXBRL) セグメント抽出器

TDnet 決算短信 XBRL ZIP の Attachment ファイルから
iXBRL タグを構造的に解析し、セグメント売上/利益を抽出する。

SEGMENT_EXTRACTION_SPEC §5.1
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import logging
import os
import re
import zipfile
from typing import Optional
from dataclasses import dataclass
from dateutil.relativedelta import relativedelta

from bs4 import BeautifulSoup

from src.segment.models import SegmentRawRow
from src.segment.normalize import (
    normalize_segment_name,
    classify_special_row,
    is_single_segment_company,
)

logger = logging.getLogger("xbrl_seg")

def _calculate_expected_context_end(period_str: str, quarter: str) -> Optional[datetime.date]:
    if not period_str or len(period_str) < 10:
        return None
    try:
        fy_end = datetime.datetime.strptime(period_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None

    months_to_subtract = 0
    if quarter == "1Q": months_to_subtract = 9
    elif quarter == "2Q": months_to_subtract = 6
    elif quarter == "3Q": months_to_subtract = 3
    elif quarter == "FY": months_to_subtract = 0
    elif quarter == "4Q": months_to_subtract = 0

    if months_to_subtract == 0:
        return fy_end

    d = fy_end - relativedelta(months=months_to_subtract)
    _, last_day = calendar.monthrange(d.year, d.month)
    return datetime.date(d.year, d.month, min(d.day, last_day))

# ============================================================
# iXBRL タグ → セグメント値マッピング
# ============================================================
# JP基準
_SALES_TAGS = {
    "jpcrp_cor:revenuesfromexternalcustomers",  # 外部顧客への売上高
    "jppfs_cor:netsales",                       # 売上高
}
_PROFIT_TAGS = {
    "jppfs_cor:operatingincome",                # 営業利益 / セグメント利益
    "jppfs_cor:operatingprofit",
    "jppfs_cor:ordinaryincome",                 # 経常利益
    "jppfs_cor:ordinaryincomebnk",              # 経常利益（銀行業）
    "jppfs_cor:incomebeforeincometaxes",        # 税引前当期純利益
}
# IFRS基準
_IFRS_SALES_TAGS = {
    "jpigp_cor:revenueifrs",
    "jpigp_cor:revenue",
    "jpigp_cor:revenue2ifrs",                          # IFRS "2" variant
    "jpigp_cor:revenuefromexternalcustomersifrs",       # 外部顧客売上 (IFRS)
    "jpigp_cor:revenuefromexternalcustomers2ifrs",      # 外部顧客売上 (IFRS "2")
    "jpigp_cor:salestoexternalcustomersifrs",           # 外部顧客への売上 (IFRS alt)
    "jpigp_cor:netsalesifrs",                           # 純売上高 (IFRS)
}
_IFRS_PROFIT_TAGS = {
    "jpigp_cor:profitlossifrs",
    "jpigp_cor:operatingprofitlossifrs",
    "jpigp_cor:businessprofitlossifrs",
    "jpigp_cor:profitlossbeforetaxifrs",                # 税前損益 (IFRS)
}

# 会社固有 namespace のパターン (tse-qcediffr-72030:xxx 等)
# element 名の末尾が以下のいずれかなら sales/profit として認識
_COMPANY_SALES_SUFFIXES = (
    "revenuesfromexternalcustomers",
    "operatingrevenuefromexternalcustomersifrs",
    "salesrevenuesifrs",
    "salestoexternalcustomersifrs",
    "revenuefromexternalcustomersifrs",
    "revenuefromexternalcustomers2ifrs",
    "netsales",
    "netsalesifrs",
    "revenueifrs",
    "revenue2ifrs",
)
_COMPANY_PROFIT_SUFFIXES = (
    "operatingincome",
    "operatingprofit",
    "operatingprofitlossifrs",
    "profitlossifrs",
    "profitlossbeforetaxifrs",
    "businessprofitlossifrs",
    "ordinaryincome",
    "ordinaryincomebnk",
    "incomebeforeincometaxes",
)

ALL_SALES_TAGS = _SALES_TAGS | _IFRS_SALES_TAGS
ALL_PROFIT_TAGS = _PROFIT_TAGS | _IFRS_PROFIT_TAGS

# ============================================================
# context からセグメント名を抽出
# ============================================================
# パターン1: 標準 suffix (ReportableSegmentsMember 等)
_SEGMENT_MEMBER_RE = re.compile(
    r"(?:tse-\w+-\d+0?)"       # prefix (tse-acedjpfr-25900, tse-qcediffr-72030 等)
    r"(\w+?)"                   # segment member name
    r"(?:ReportableSegments?Member|OperatingSegments?Member"
    r"|BusinessSegments?Member|OtherSegments?Member)",
    re.IGNORECASE,
)

# パターン2: ticker prefix + plain Member (76860StoreSalesMember, 246A0NetworkMember 等)
# ticker は数字4-5桁 or 英数字混合 (246A0 等)
_TICKER_MEMBER_RE = re.compile(
    r"(?:^|_)(?:\d[\dA-Z]{3,5})"  # ticker prefix (76860, 246A0, 145A0 等)
    r"(\w+?)"                      # segment member name
    r"Member$",
    re.IGNORECASE,
)

# パターン3: OperatingSegmentsNotIncluded... (残余区分)
_OSNI_RE = re.compile(
    r"OperatingSegmentsNotIncludedInReportableSegments"
    r"(?:AndOtherRevenueGeneratingBusinessActivities)?"
    r"Member",
    re.IGNORECASE,
)

# パターン4: OtherReportableSegmentsMember (Other区分)
_OTHER_SEGMENTS_RE = re.compile(
    r"OtherReportableSegments?Member",
    re.IGNORECASE,
)

# CamelCase → 日本語マッピング用正規化
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _extract_segment_member(context_ref: str) -> Optional[str]:
    """context ref からセグメント member 名を抽出。

    対応パターン:
    1. tse-xxx-{ticker}NameReportableSegmentsMember
    2. {ticker}NameMember (plain suffix)
    3. OperatingSegmentsNotIncluded...Member → "その他"
    4. OtherReportableSegmentsMember → "Other"

    例: "CurrentYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember"
    → "DomesticBeverageBusiness"
    """
    # パターン1: 標準 suffix
    m = _SEGMENT_MEMBER_RE.search(context_ref)
    if m:
        return m.group(1)

    # パターン3: OperatingSegmentsNotIncluded... → "Other" として扱う
    if _OSNI_RE.search(context_ref):
        return "Other"

    # パターン4: OtherReportableSegmentsMember
    if _OTHER_SEGMENTS_RE.search(context_ref):
        return "Other"

    # パターン2: ticker prefix + plain Member
    # context_ref の各 _ 区切りパーツから探す
    for part in context_ref.split("_"):
        m2 = _TICKER_MEMBER_RE.search(part)
        if m2:
            name = m2.group(1)
            # 除外: 構造メンバー (Reconciling, EntityTotal, etc.)
            if name.lower() in ("reconciling", "reconcilingitems",
                                "entitytotal", "corporateexpensesandelimination"):
                return None
            return name

    # unknown member suffix 検出 (パターン漏れの早期発見用)
    if "Member" in context_ref and ("Segment" in context_ref or "segment" in context_ref):
        _UNKNOWN_SUFFIX_RE = re.compile(r"((?:Segment|segment)\w*Member)")
        um = _UNKNOWN_SUFFIX_RE.search(context_ref)
        if um:
            logger.debug(f"[xbrl_seg] unknown member suffix: {um.group(1)} in {context_ref[:120]}")
    return None


def _round_to_month_end(date_str: str) -> str:
    """YYYY-MM-DD を同月末日に丸める。

    PL 側は period を月末日 (e.g. 2026-01-31) で保持するため、
    XBRL ファイル名から取得した実日付 (e.g. 2026-01-20) を月末に統一する。

    例: '2026-01-20' → '2026-01-31'
         '2025-03-31' → '2025-03-31' (変更なし)
    """
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_str)
    if not m:
        return date_str
    year, month = int(m.group(1)), int(m.group(2))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _classify_period_type(context_ref: str) -> str:
    """context_ref を "current" / "previous" / "unknown" に分類する。

    - "prior" を含む → "previous" (前期・前年同期)
    - currentyear / currentytd / currentquarter / interimduration を含む → "current"
    - それ以外 → "unknown"
    """
    ctx = context_ref.lower()
    if "prior" in ctx:
        return "previous"
    if (
        "currentyear" in ctx
        or "currentytd" in ctx
        or "currentquarter" in ctx
        or "interimduration" in ctx
    ):
        return "current"
    return "unknown"


def _is_current_period(context_ref: str) -> bool:
    """context が当期データかどうか判定。後方互換のため残す。"""
    return _classify_period_type(context_ref) == "current"


def _is_duration_context(context_ref: str) -> bool:
    """context が期間（Duration）かどうか。Instant は除外。"""
    ctx = context_ref.lower()
    return "duration" in ctx


# ============================================================
# 単位変換
# ============================================================
def _detect_unit_from_html(html_content: str) -> str:
    """HTML ヘッダーから単位を検出。"""
    if "百万円" in html_content:
        return "million_yen"
    if "千円" in html_content:
        return "thousand_yen"
    if "円" in html_content:
        return "yen"
    return "million_yen"  # デフォルト



def _parse_context_periods(soup: BeautifulSoup) -> dict[str, dict]:
    contexts = {}
    for ctx in soup.find_all(lambda t: t.name and t.name.endswith("context")):
        cid = ctx.get("id")
        if not cid:
            continue

        info = {}
        period = ctx.find(lambda t: t.name and t.name.endswith("period"))
        if period:
            start = period.find(lambda t: t.name and t.name.endswith("startdate"))
            end = period.find(lambda t: t.name and t.name.endswith("enddate"))
            instant = period.find(lambda t: t.name and t.name.endswith("instant"))

            if start and end:
                info["type"] = "duration"
                s_str = start.get_text(strip=True).split("T")[0]
                e_str = end.get_text(strip=True).split("T")[0]
                info["start"] = s_str
                info["end"] = e_str
                try:
                    s_date = datetime.datetime.strptime(s_str, "%Y-%m-%d").date()
                    e_date = datetime.datetime.strptime(e_str, "%Y-%m-%d").date()
                    info["duration_days"] = (e_date - s_date).days
                    info["duration_months"] = round(info["duration_days"] / 30.436875)
                except Exception:
                    pass
            elif instant:
                info["type"] = "instant"
                info["instant"] = instant.get_text(strip=True).split("T")[0]

        contexts[cid] = info
    return contexts

def _to_million_yen(value: int, unit: str) -> int:
    """百万円に変換。"""
    if unit == "million_yen":
        return value
    if unit == "thousand_yen":
        return value // 1000
    if unit == "yen":
        return value // 1_000_000
    return value


# ============================================================
# 数値パース
# ============================================================
def _parse_ixbrl_number(text: str, sign_attr: str | None = None) -> Optional[int]:
    """iXBRL テキストから数値を抽出。

    符号:
    - sign="-" attr → 負
    - テキスト中の △ → 負
    - テキスト中の ( ) → 負
    """
    if not text or text.strip() in ("－", "-", "―", "—", ""):
        return None

    negative = False
    if sign_attr == "-":
        negative = True

    s = text.strip()
    if "△" in s:
        negative = True
        s = s.replace("△", "")
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]

    s = s.replace(",", "").replace("，", "").replace(" ", "").strip()

    try:
        val = int(float(s))
        return -val if negative else val
    except (ValueError, OverflowError):
        return None


# ============================================================
# ticker 抽出
# ============================================================
_TICKER_FROM_CTX = re.compile(r"tse-\w+-([\dA-Z]{4,5}0?)", re.IGNORECASE)

def _extract_ticker_from_context(context_ref: str) -> Optional[str]:
    m = _TICKER_FROM_CTX.search(context_ref)
    if m:
        code = m.group(1)
        # 5文字 かつ 末尾 "0" → 先頭4文字 (alpha 対応: isdigit 不問)
        if len(code) == 5 and code.endswith("0"):
            return code[:4]
        return code
    return None


def _parse_quarter_from_title(title: str) -> str:
    """タイトルから1Q, 2Q, 3Q, FY を判定する。"""
    if not title: return "UNKNOWN"
    t = title.lower().replace('１', '1').replace('２', '2').replace('３', '3')
    # 訂正の除去
    t = re.sub(r'[\(（]訂正[\)）]', '', t)
    t = t.replace('訂正', '')

    if '第3四半期' in t: return '3Q'
    if '第2四半期' in t or '中間' in t: return '2Q'
    if '第1四半期' in t: return '1Q'

    if '決算短信' in t:
        if not re.search(r'第[1234]四半期', t) and '中間' not in t:
            return 'FY'
    return "UNKNOWN"

# ============================================================
# メインエントリ
# ============================================================
@dataclass
class SegmentExtractionResult:
    status: str
    segments: list[SegmentRawRow]
    reason: str | None = None
    title_quarter: str | None = None
    date_guard_status: str | None = None
    candidate_file_count: int = 0
    parsed_file_count: int = 0


def extract_segments_from_xbrl_zip(
    zip_path: str,
    period: Optional[str] = None,
    quarter: Optional[str] = None,
    title: Optional[str] = None,
    include_context_evidence: bool = False,
) -> list[SegmentRawRow]:
    """XBRL ZIP からセグメント情報を抽出。

    Args:
        zip_path: TDnet XBRL ZIP ファイルパス
        period: 決算期末日 (YYYY-MM-DD)。None の場合はファイル名から推定
        quarter: 四半期 (1Q/2Q/3Q/FY)。None の場合はファイル名から推定

    Returns:
        SegmentRawRow のリスト
    """
    result = extract_segments_from_xbrl_zip_detailed(
        zip_path=zip_path,
        period=period,
        quarter=quarter,
        title=title,
        include_context_evidence=include_context_evidence
    )
    return result.segments


def extract_segments_from_xbrl_zip_detailed(
    zip_path: str,
    period: Optional[str] = None,
    quarter: Optional[str] = None,
    title: Optional[str] = None,
    include_context_evidence: bool = False,
) -> SegmentExtractionResult:
    """XBRL ZIP からセグメント情報を詳細ステータス付きで抽出。"""
    parse_error_count = 0
    unresolved_context_count = 0
    date_guard_status_set = set()
    candidate_file_count = 0
    parsed_file_count = 0
    results = []

    if not os.path.exists(zip_path):
        logger.warning(f"ZIP not found: {zip_path}")
        return SegmentExtractionResult(
            status="zip_not_found",
            segments=[],
            reason="zip_file_not_found"
        )

    try:
        doc_hash = hashlib.md5(open(zip_path, "rb").read()).hexdigest()[:12]
    except Exception as e:
        logger.warning(f"Failed to read/hash ZIP file {zip_path}: {e}")
        return SegmentExtractionResult(
            status="parse_error",
            segments=[],
            reason=f"zip_hash_failure: {type(e).__name__}"
        )

    basename = os.path.basename(zip_path)
    meta_quarter = quarter
    estimated_period = period
    accounting_standard = "JP"

    global_context_map = {}
    document_title = None
    title_quarter = None

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith((".htm", ".html")):
                    try:
                        content = zf.read(name).decode("utf-8", errors="replace")

                        if not document_title:
                            m = re.search(r'<ix:nonNumeric[^>]*?name=[\"\'\s]*[^:]*:(?:DocumentTitle|DocumentName)[\"\'\s]*[^>]*>(.*?)</ix:nonNumeric>', content, re.I | re.S)
                            if m:
                                document_title = m.group(1).strip()

                        if "context" in content.lower():
                            s = BeautifulSoup(content, "html.parser")
                            global_context_map.update(_parse_context_periods(s))
                    except Exception as e:
                        logger.warning(f"Failed to parse context in {name}: {e}")
                        parse_error_count += 1

            if document_title:
                title_quarter = _parse_quarter_from_title(document_title)

            if not title_quarter or title_quarter == "UNKNOWN":
                logger.warning(f"[XBRL] Could not verify quarter from title for {basename}: '{document_title}'. Skipping to be safe.")
                return SegmentExtractionResult(
                    status="quarter_unresolved",
                    segments=[],
                    reason="title_quarter_unknown",
                    title_quarter=title_quarter
                )

            estimated_quarter = title_quarter
            if meta_quarter and meta_quarter != "UNKNOWN" and meta_quarter != title_quarter:
                logger.info(f"[XBRL] Quarter mismatch for {basename}: meta={meta_quarter}, title={title_quarter} ({document_title}). Trusting title_quarter.")

            seg_files = _find_segment_files(zf)
            candidate_file_count = len(seg_files)
            if not seg_files:
                logger.info(f"[XBRL] No segment files in {basename}")
                return SegmentExtractionResult(
                    status="segment_source_unavailable",
                    segments=[],
                    reason="no_segment_files_in_zip",
                    title_quarter=estimated_quarter,
                    candidate_file_count=0
                )

            if not global_context_map:
                logger.warning(f"[XBRL] global_context_map is empty for {basename}")
                return SegmentExtractionResult(
                    status="context_unresolved",
                    segments=[],
                    reason="global_context_map_empty",
                    title_quarter=estimated_quarter,
                    candidate_file_count=candidate_file_count
                )

            expected_end = None
            if period and estimated_quarter:
                expected_end = _calculate_expected_context_end(period, estimated_quarter)

            for seg_file in seg_files:
                try:
                    content = zf.read(seg_file).decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"Failed to read segment file {seg_file}: {e}")
                    parse_error_count += 1
                    continue

                fn = seg_file.lower()
                if "ifsm" in fn or "iffr" in fn:
                    accounting_standard = "IFRS"

                loop_period = estimated_period
                if not loop_period:
                    pm = re.search(r"-(\d{4}-\d{2}-\d{2})", seg_file)
                    if pm:
                        loop_period = pm.group(1)

                if loop_period:
                    loop_period = _round_to_month_end(loop_period)

                try:
                    unit = _detect_unit_from_html(content)
                    soup = BeautifulSoup(content, "html.parser")
                    rows = _extract_ixbrl_segment_data(
                        soup,
                        accounting_standard,
                        estimated_quarter,
                        global_context_map,
                        expected_end=expected_end,
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse BeautifulSoup or rows in {seg_file}: {e}")
                    parse_error_count += 1
                    continue

                parsed_file_count += 1

                file_date_guard_status = "UNKNOWN"

                def _get_prior_expected_end(ed: datetime.date) -> datetime.date:
                    try:
                        if ed.month == 2 and ed.day == 29:
                            return datetime.date(ed.year - 1, 2, 28)
                        else:
                            return datetime.date(ed.year - 1, ed.month, ed.day)
                    except ValueError:
                        return ed

                if expected_end:
                    actual_ends = []
                    if rows:
                        used_ctx_ids = list(set([r.get("context_ref") for r in rows.values() if "context_ref" in r]))
                        for cid in used_ctx_ids:
                            info = global_context_map.get(cid)
                            if info and info.get("type") == "duration" and "end" in info:
                                try:
                                    dt = datetime.datetime.strptime(info["end"][:10], "%Y-%m-%d").date()
                                    ptype = _classify_period_type(cid)
                                    actual_ends.append((dt, ptype, cid))
                                except ValueError:
                                    pass

                    if not actual_ends:
                        for cid, info in global_context_map.items():
                            if info.get("type") == "duration" and "end" in info:
                                ptype = _classify_period_type(cid)
                                if ptype == "current" or ptype == "previous":
                                    try:
                                        dt = datetime.datetime.strptime(info["end"][:10], "%Y-%m-%d").date()
                                        actual_ends.append((dt, ptype, cid))
                                    except ValueError:
                                        pass

                    if actual_ends:
                        current_ends = [x for x in actual_ends if x[1] == "current"]
                        if current_ends:
                            min_diff = min(abs((ae[0] - expected_end).days) for ae in current_ends)
                            if min_diff > 40:
                                file_date_guard_status = "SKIP"
                            else:
                                file_date_guard_status = "PASS"
                        else:
                            file_date_guard_status = "SKIP"
                    else:
                        file_date_guard_status = "SKIP"

                date_guard_status_set.add(file_date_guard_status)

                if file_date_guard_status == "SKIP":
                    logger.warning(
                        f"[XBRL] Context Date Guard failed for file {seg_file}. "
                        f"expected_end={expected_end} (fy={period}, q={estimated_quarter}). Skipping."
                    )
                    continue

                if not rows:
                    continue

                ticker = None
                for tag in soup.find_all("ix:nonfraction")[:20]:
                    _ctx = tag.get("contextref", "")
                    if _ctx:
                        ticker = _extract_ticker_from_context(_ctx)
                        if ticker:
                            break

                _prev_period = None
                if loop_period and len(loop_period) >= 4:
                    try:
                        _prev_year = int(loop_period[:4]) - 1
                        _month_day = loop_period[5:]
                        if _month_day == "02-29" and not calendar.isleap(_prev_year):
                            _prev_period = f"{_prev_year}-02-28"
                        else:
                            _prev_period = f"{_prev_year}-{_month_day}"
                    except ValueError:
                        _prev_period = None

                for key, data in rows.items():
                    member_name, period_type = key
                    raw_name = _camel_to_readable(member_name)
                    normalized = normalize_segment_name(raw_name)
                    special = classify_special_row(normalized or raw_name)

                    sales = data.get("sales")
                    profit = data.get("profit")

                    if sales is not None:
                        sales = _to_million_yen(sales, unit)
                    if profit is not None:
                        profit = _to_million_yen(profit, unit)

                    if period_type == "previous" and _prev_period:
                        row_period = _prev_period
                    else:
                        row_period = loop_period or ""

                    profit_tag = data.get("profit_tag")
                    row_raw_json = {"_segment_period_role": period_type}
                    if profit_tag:
                        row_raw_json["profit_tag"] = profit_tag

                    if include_context_evidence:
                        row_raw_json = row_raw_json or {}
                        ctx_ref = data.get("context_ref", "")
                        cinfo = global_context_map.get(ctx_ref, {})
                        cstart = cinfo.get("start", "?")
                        cend = cinfo.get("end", "?")
                        ptype = _classify_period_type(ctx_ref) if ctx_ref else "unknown"

                        d_days = "?"
                        adjusted_expected_end_str = "?"

                        if cend != "?" and expected_end:
                            try:
                                cdate = datetime.datetime.strptime(cend[:10], "%Y-%m-%d").date()
                                if ptype == "previous":
                                    adj_expected_end = _get_prior_expected_end(expected_end)
                                    adjusted_expected_end_str = str(adj_expected_end)
                                    d_days = abs((cdate - adj_expected_end).days)
                                else:
                                    adjusted_expected_end_str = str(expected_end)
                                    d_days = abs((cdate - expected_end).days)
                            except:
                                pass

                        row_raw_json["_context_evidence"] = {
                            "context_ref": ctx_ref,
                            "context_start": cstart,
                            "context_end": cend,
                            "duration_days": cinfo.get("duration_days", "?"),
                            "current_or_previous": period_type,
                            "quarter": estimated_quarter or "",
                            "selection_reason": data.get("selection_reason", ""),
                            "expected_context_end": str(expected_end) if expected_end else "?",
                            "adjusted_expected_context_end": adjusted_expected_end_str,
                            "diff_days": d_days,
                            "date_guard_status": "PASS" if type(d_days) is int and d_days <= 40 else ("SKIP" if type(d_days) is int else "UNKNOWN"),
                            "context_period_type": ptype,
                            "evidence_mode": True
                        }

                    row = SegmentRawRow(
                        source="xbrl",
                        source_document_id=basename,
                        doc_hash=doc_hash,
                        raw_ticker=ticker or "",
                        normalized_ticker=ticker or "",
                        period=row_period,
                        quarter=estimated_quarter or "",
                        raw_segment_name=raw_name,
                        normalized_segment_name=normalized,
                        special_row_type=special,
                        sales=sales,
                        profit=profit,
                        unit="million_yen",
                        extraction_method="xbrl",
                        confidence_score=0.95,
                        is_consolidated=True,
                        accounting_standard=accounting_standard,
                        table_title=f"XBRL segment: {seg_file}",
                        raw_json=row_raw_json,
                    )
                    results.append(row)

            if results:
                from src.segment.normalize import normalize_segment_key
                groups = {}
                for r in results:
                    k = (r.period, r.quarter, normalize_segment_key(r.raw_segment_name))
                    if k not in groups:
                        groups[k] = []
                    groups[k].append(r)

                dedup_results = []
                for k, rows_in_group in groups.items():
                    if len(rows_in_group) == 1:
                        dedup_results.append(rows_in_group[0])
                        continue
                    first_sales = rows_in_group[0].sales
                    first_profit = rows_in_group[0].profit
                    conflict = False
                    for r in rows_in_group[1:]:
                        if r.sales is not None and first_sales is not None and r.sales != first_sales:
                            conflict = True
                        if r.profit is not None and first_profit is not None and r.profit != first_profit:
                            conflict = True
                    if conflict:
                        for r in rows_in_group:
                            rj = r.raw_json or {}
                            rj["duplicate_resolution_reason"] = "conflicting_value"
                            r.raw_json = rj
                            dedup_results.append(r)
                    else:
                        sorted_rows = sorted(rows_in_group, key=lambda x: len(x.raw_segment_name))
                        best_row = sorted_rows[0]
                        rj = best_row.raw_json or {}
                        rj["duplicate_resolution_reason"] = "folded_same_value"
                        best_row.raw_json = rj
                        dedup_results.append(best_row)
                results = dedup_results

    except zipfile.BadZipFile as e:
        logger.warning(f"Bad ZIP: {zip_path}")
        parse_error_count += 1
        return SegmentExtractionResult(
            status="parse_error",
            segments=[],
            reason=f"bad_zip_file: {type(e).__name__}",
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )
    except Exception as e:
        logger.warning(f"Error processing {zip_path}: {e}")
        parse_error_count += 1
        return SegmentExtractionResult(
            status="parse_error",
            segments=results,
            reason=f"unexpected_exception: {type(e).__name__}",
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    if parse_error_count > 0:
        return SegmentExtractionResult(
            status="parse_error",
            segments=results,
            reason="partial_parsing_failure",
            title_quarter=estimated_quarter,
            date_guard_status="PASS" if "PASS" in date_guard_status_set else ("SKIP" if "SKIP" in date_guard_status_set else "UNKNOWN"),
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    if unresolved_context_count > 0:
        return SegmentExtractionResult(
            status="context_unresolved",
            segments=results,
            reason="unresolved_contexts_exist",
            title_quarter=estimated_quarter,
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    date_guard_status_final = "UNKNOWN"
    if "SKIP" in date_guard_status_set:
        date_guard_status_final = "SKIP"
    elif "PASS" in date_guard_status_set:
        date_guard_status_final = "PASS"

    if expected_end and date_guard_status_final == "SKIP":
        return SegmentExtractionResult(
            status="date_guard_skip",
            segments=results,
            reason="all_candidate_files_skipped_by_date_guard" if "PASS" not in date_guard_status_set else "mixed_date_guard_results_contains_skip",
            title_quarter=estimated_quarter,
            date_guard_status="SKIP",
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    if len(results) > 0:
        return SegmentExtractionResult(
            status="success_with_rows",
            segments=results,
            title_quarter=estimated_quarter,
            date_guard_status=date_guard_status_final,
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    is_date_guard_ok = (expected_end is not None) and (date_guard_status_final == "PASS")

    if (
        candidate_file_count >= 1
        and parsed_file_count == candidate_file_count
        and parse_error_count == 0
        and unresolved_context_count == 0
        and is_date_guard_ok
        and len(results) == 0
    ):
        return SegmentExtractionResult(
            status="success_empty",
            segments=[],
            title_quarter=estimated_quarter,
            date_guard_status=date_guard_status_final,
            candidate_file_count=candidate_file_count,
            parsed_file_count=parsed_file_count
        )

    return SegmentExtractionResult(
        status="date_guard_skip" if date_guard_status_final == "SKIP" else "context_unresolved",
        segments=results,
        reason="fallback_unresolved_status",
        title_quarter=estimated_quarter,
        date_guard_status=date_guard_status_final,
        candidate_file_count=candidate_file_count,
        parsed_file_count=parsed_file_count
    )


def _find_segment_files(zf: zipfile.ZipFile) -> list[str]:
    """ZIP 内のセグメント関連ファイルを探す。"""
    seg_files = []
    for name in zf.namelist():
        # acsg (annual consolidated segment), qcsg (quarterly) 等
        if "sg" in os.path.basename(name).lower() and name.endswith((".htm", ".html")):
            seg_files.append(name)
    if seg_files:
        return seg_files

    # fallback: Attachment 内でセグメントテキストを含むファイル
    for name in zf.namelist():
        if "Attachment" in name and name.endswith((".htm", ".html")):
            try:
                content = zf.read(name).decode("utf-8", errors="replace")[:5000]
                if "セグメント" in content:
                    seg_files.append(name)
            except:
                pass
    return seg_files


def _get_profit_priority(name: str) -> int:
    """利益タグの優先順位（低い方が優先）"""
    n = name.lower()
    if "extraordinary" in n:
        return 999
    if "operating" in n or "businessprofit" in n or "profitlossifrs" in n:
        return 1
    if "ordinaryincomebnk" in n:
        return 3
    if "ordinaryincome" in n:
        return 2
    if "beforetax" in n or "incomebefore" in n:
        return 4
    return 1

def _extract_ixbrl_segment_data(
    soup: BeautifulSoup, accounting_standard: str, estimated_quarter: str = "UNKNOWN", global_context_map: Optional[dict] = None, expected_end: Optional[datetime.date] = None
) -> dict[tuple[str, str], dict]:
    """iXBRL タグからセグメント別の売上/利益を抽出。

    当期 (current) と前期 (previous) の両方を収集する。
    unknown context および Instant context は除外する。

    Returns:
        {(member_name, period_type): {"sales": int, "profit": int}}
        period_type は "current" | "previous"
    """
    sales_tags = ALL_SALES_TAGS
    profit_tags = ALL_PROFIT_TAGS

    # 優先: 計 (NetSales等) > 外部顧客への売上高等のその他売上タグ
    _PRIMARY_SALES_NAMES = {
        "jppfs_cor:netsales",
        "jpigp_cor:netsalesifrs",
        "jpigp_cor:revenueifrs",
        "jpigp_cor:revenue",
        "jpigp_cor:revenue2ifrs",
    }

    # 一時的な蓄積用。キーを (member, period_type, context_ref) にして上書き衝突を防ぐ
    temp_candidate: dict[tuple[str, str, str], dict] = {}

    # ix:nonfraction タグを収集
    for tag in soup.find_all("ix:nonfraction"):
        name = (tag.get("name") or "").lower()
        ctx = tag.get("contextref", "")
        sign = tag.get("sign")
        text = tag.get_text(strip=True)

        # unknown context と Instant は除外。current / previous は両方通す。
        period_type = _classify_period_type(ctx)
        if period_type == "unknown" or not _is_duration_context(ctx):
            continue

        # セグメント member 抽出
        member = _extract_segment_member(ctx)
        if not member:
            continue

        # キーを (member, period_type, context_ref) にする
        key = (member, period_type, ctx)
        if key not in temp_candidate:
            temp_candidate[key] = {
                "context_ref": ctx,
                "sales": None,
                "profit": None,
                "profit_priority": 999,
                "profit_tag": None
            }

        value = _parse_ixbrl_number(text, sign)
        if value is None:
            continue

        # 会社固有 namespace を含む element の判定
        is_sales = name in sales_tags
        is_profit = name in profit_tags
        is_primary_sales = name in _PRIMARY_SALES_NAMES

        if not is_sales and not is_profit:
            # 会社固有 namespace (tse-xxx:yyy) を suffix で判定
            local_name = name.split(":")[-1] if ":" in name else name
            if any(local_name.endswith(s) for s in _COMPANY_SALES_SUFFIXES):
                is_sales = True
                # 計(NetSales等)は primary 扱い
                if any(local_name.endswith(s) for s in (
                    "netsales",
                    "netsalesifrs",
                    "revenueifrs",
                    "revenue",
                    "revenue2ifrs",
                )):
                    is_primary_sales = True
            elif any(local_name.endswith(s) for s in _COMPANY_PROFIT_SUFFIXES):
                if not local_name.endswith("extraordinaryincome"):
                    is_profit = True

        if is_sales:
            if is_primary_sales or temp_candidate[key]["sales"] is None:
                temp_candidate[key]["sales"] = value
        elif is_profit:
            priority = _get_profit_priority(name)
            current_priority = temp_candidate[key].get("profit_priority", 999)
            if priority < current_priority:
                temp_candidate[key]["profit"] = value
                temp_candidate[key]["profit_priority"] = priority
                temp_candidate[key]["profit_tag"] = name

    # global_context_map から「当期の期待終了日」の基準値 (reference_end) を推定
    reference_end: Optional[datetime.date] = None
    if global_context_map:
        for cid, info in global_context_map.items():
            if info.get("type") == "duration" and "end" in info:
                ptype = _classify_period_type(cid)
                if ptype == "current":
                    try:
                        dt = datetime.datetime.strptime(info["end"][:10], "%Y-%m-%d").date()
                        if reference_end is None or dt > reference_end:
                            reference_end = dt
                    except ValueError:
                        pass

    # グループ化: (member, period_type) ごとに候補を集約
    groups: dict[tuple[str, str], list[dict]] = {}
    for (member, period_type, ctx), data in temp_candidate.items():
        gkey = (member, period_type)
        if gkey not in groups:
            groups[gkey] = []

        # 片方の指標しか存在しない場合は、存在する値だけを保持 (別contextから補完しない)
        # 必要なのは best_cand になった context 単位での sales / profit のみ
        groups[gkey].append(data)

    # quarter に応じた期待 duration 日数
    target_duration = 365
    if estimated_quarter == "1Q":
        target_duration = 90
    elif estimated_quarter == "2Q":
        target_duration = 180
    elif estimated_quarter == "3Q":
        target_duration = 270
    elif estimated_quarter in ("FY", "4Q"):
        target_duration = 365

    result: dict[tuple[str, str], dict] = {}
    for gkey, candidates in groups.items():
        member, period_type = gkey

        valid_candidates = []
        for cand in candidates:
            ctx = cand["context_ref"]
            info = global_context_map.get(ctx, {}) if global_context_map else {}
            actual_duration = info.get("duration_days")
            actual_end_str = info.get("end")

            # 1. duration 差の算出
            if actual_duration is not None:
                diff_duration = abs(actual_duration - target_duration)
            else:
                diff_duration = 9999

            # 2. 期待終了日差の算出
            expected_end_date = expected_end if expected_end is not None else reference_end
            if expected_end_date and period_type == "previous":
                try:
                    if expected_end_date.month == 2 and expected_end_date.day == 29:
                        expected_end_date = datetime.date(expected_end_date.year - 1, 2, 28)
                    else:
                        expected_end_date = datetime.date(expected_end_date.year - 1, expected_end_date.month, expected_end_date.day)
                except ValueError:
                    pass

            diff_end = 9999
            if actual_end_str and expected_end_date:
                try:
                    actual_end_date = datetime.datetime.strptime(actual_end_str[:10], "%Y-%m-%d").date()
                    diff_end = abs((actual_end_date - expected_end_date).days)
                except ValueError:
                    pass

            # 40日許容差チェック (期待durationまたは期待終了日との差が40日を超えるものは除外)
            if diff_duration > 40 or diff_end > 40:
                continue

            both_exist = 0 if (cand["sales"] is not None and cand["profit"] is not None) else 1
            sort_key = (
                diff_duration,   # 1. 期待durationとの差が小さい
                diff_end,        # 2. 対象期の期待終了日との差が小さい
                both_exist,      # 3. salesとprofitの両方が存在
                ctx              # 4. context_refによる安定した順序
            )
            valid_candidates.append((sort_key, cand))

        if not valid_candidates:
            continue

        # 最適な候補をソート順で決定
        valid_candidates.sort(key=lambda x: x[0])
        best_sort_key, best_cand = valid_candidates[0]

        diff_duration, diff_end, both_exist, ctx = best_sort_key
        best_cand["selection_reason"] = f"duration_diff={diff_duration}, end_diff={diff_end}, both_exist={both_exist == 0}"

        result[gkey] = best_cand

    # 有効なデータのみ集約して返却
    aggregated: dict[tuple[str, str], dict] = {
        k: v for k, v in result.items() if v
    }
    return aggregated


def _camel_to_readable(member_name: str) -> str:
    """CamelCase member名を読みやすい形にする。

    例: DomesticBeverageBusiness → Domestic Beverage Business
    ただし、日本語ラベルが iXBRL label に含まれている場合はそちらを優先（将来対応）。
    """
    if not member_name:
        return ""
    parts = _CAMEL_SPLIT.split(member_name)
    return " ".join(parts)
