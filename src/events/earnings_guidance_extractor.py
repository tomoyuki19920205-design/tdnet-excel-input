#!/usr/bin/env python3
"""earnings_guidance_extractor.py — 4Q決算短信 来期ガイダンス + 見通し抽出

ゼロから再実装。テスト駆動。
"""
from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("guidance_extractor")

# ============================================================
# 定数
# ============================================================

_EPS_ABSURD_THRESHOLD = 10_000

# Forecast 用 XBRL タグマッピング
_FORECAST_TAG_MAP = {
    # Sales
    "NetSales": "sales",
    "Revenue": "sales",
    "OperatingRevenue": "sales",
    "OperatingRevenuesREIT": "sales",
    # Operating profit
    "OperatingIncome": "operating_profit",
    "OperatingProfit": "operating_profit",
    # Ordinary profit
    "OrdinaryIncome": "ordinary_profit",
    # Net income
    "NetIncome": "net_income",
    "ProfitLoss": "net_income",
    "ProfitLossAttributableToOwnersOfParent": "net_income",
}

# 内部メトリック名 → GuidanceData フィールド名
_METRIC_TO_FIELD = {
    "sales": "sales_forecast",
    "operating_profit": "op_forecast",
    "ordinary_profit": "ordinary_forecast",
    "net_income": "net_income_forecast",
    "eps": "eps_forecast",
}

# EPS XBRL タグ
_EPS_BASIC_TAGS = {
    "EarningsPerShare",
    "BasicEarningsPerShare",
    "BasicEarningsLossPerShare",
    "NetIncomePerShare",
}
_EPS_DILUTED_TAGS = {
    "DilutedEarningsPerShare",
    "DilutedEarningsLossPerShare",
}
_EPS_ALL_TAGS = _EPS_BASIC_TAGS | _EPS_DILUTED_TAGS

# iXBRL / XBRL 拡張子
_IXBRL_EXTS = ("-ixbrl.htm", ".ixbrl.htm", "-ixbrl.html", ".ixbrl.html", ".ixbrl")
_XBRL_EXTS = (".xbrl",)
_ZIP_SIG = b"PK\x03\x04"


# ============================================================
# GuidanceData
# ============================================================
@dataclass
class GuidanceData:
    """来期ガイダンスデータ"""
    sales_forecast: Optional[float] = None
    op_forecast: Optional[float] = None
    ordinary_forecast: Optional[float] = None
    net_income_forecast: Optional[float] = None
    eps_forecast: Optional[float] = None

    sales_actual: Optional[float] = None
    op_actual: Optional[float] = None
    eps_actual: Optional[float] = None

    outlook_text: str = ""
    outlook_summary: str = ""
    outlook_factors: dict = field(default_factory=dict)

    @property
    def has_guidance(self) -> bool:
        return any(v is not None for v in [
            self.sales_forecast, self.op_forecast, self.eps_forecast,
        ])

    @property
    def has_outlook(self) -> bool:
        return bool(self.outlook_summary)

    @property
    def sales_yoy(self) -> Optional[float]:
        if self.sales_forecast is None or self.sales_actual is None or self.sales_actual == 0:
            return None
        return (self.sales_forecast / self.sales_actual) - 1.0

    @property
    def op_yoy(self) -> Optional[float]:
        if self.op_forecast is None or self.op_actual is None or self.op_actual == 0:
            return None
        return (self.op_forecast / self.op_actual) - 1.0

    @property
    def eps_yoy(self) -> Optional[float]:
        if self.eps_forecast is None or self.eps_actual is None or self.eps_actual == 0:
            return None
        return (self.eps_forecast / self.eps_actual) - 1.0


# ============================================================
# コンテキスト分類
# ============================================================

def _classify_period_type(ctx: str) -> str:
    """contextRef から期間種別を判定する。

    Returns: "full_year" / "q2_cumulative" / "unknown"
    """
    if "AccumulatedQ2" in ctx or "SecondQuarterMember" in ctx:
        return "q2_cumulative"
    if "YearDuration" in ctx and "QuarterMember" not in ctx:
        return "full_year"
    return "unknown"


def _classify_horizon(ctx: str) -> str:
    """contextRef から来期/当期を判定する。

    Returns: "next_year" / "current_year" / "unknown"
    """
    if "Next" in ctx:
        return "next_year"
    if "Current" in ctx:
        return "current_year"
    return "unknown"


# ============================================================
# 候補選択
# ============================================================

def _select_best_candidates(candidates: list[dict]) -> dict:
    """候補リストからメトリックごとに最適候補を選ぶ。

    Returns: {"sales": value_or_None, "op": ..., "eps": ..., ...}
    """
    by_metric: dict[str, list[dict]] = {}
    for c in candidates:
        by_metric.setdefault(c["metric"], []).append(c)

    result: dict[str, float | None] = {}
    for metric, cands in by_metric.items():
        best = _pick_best(cands, is_eps=(metric == "eps"))
        result[metric] = best["value"] if best else None
    return result


def _pick_best(candidates: list[dict], is_eps: bool = False) -> dict | None:
    """単一メトリックの候補から最適を選ぶ。

    ルール:
    - q2_cumulative は常に除外
    - EPS は full_year のみ採用
    - non-EPS は full_year > unknown（q2 除外済み）
    - horizon: next_year > current_year
    - 連結優先
    """
    # q2_cumulative は常に除外
    filtered = [c for c in candidates if c["period_type"] != "q2_cumulative"]
    if not filtered:
        return None

    if is_eps:
        # EPS: full_year のみ採用
        fy = [c for c in filtered if c["period_type"] == "full_year"]
        if not fy:
            return None
        filtered = fy

    def _score(c: dict) -> int:
        s = 0
        if c["period_type"] == "full_year":
            s += 100
        elif c["period_type"] == "unknown":
            s += 10
        if c["horizon"] == "next_year":
            s += 50
        elif c["horizon"] == "current_year":
            s += 30
        if c.get("is_consol"):
            s += 5
        if c.get("is_basic", True):
            s += 2
        return s

    filtered.sort(key=_score, reverse=True)
    return filtered[0]


# ============================================================
# EPS テキスト正規化
# ============================================================

def _normalize_eps_text(text: str) -> float | None:
    """EPS テキストを float に変換する。

    "237.98" → 237.98, "120円50銭" → 120.5, "△10.5円" → -10.5
    """
    if text is None:
        return None
    s = unicodedata.normalize("NFKC", text).strip()
    if not s:
        return None

    is_negative = False
    for c in "△▲":
        if c in s:
            is_negative = True
            s = s.replace(c, "")
    s = s.strip()
    if s.startswith("-") or s.startswith("−"):
        is_negative = True
        s = s.lstrip("-−").strip()

    # 円銭: "120円50銭" → 120.50
    m = re.match(r"([\d,.]+)\s*円\s*(\d+)\s*銭", s)
    if m:
        yen = float(m.group(1).replace(",", ""))
        sen = float(m.group(2))
        val = yen + sen / 100
        return -val if is_negative else val

    # 円のみ: "120円"
    m = re.match(r"([\d,.]+)\s*円", s)
    if m:
        val = float(m.group(1).replace(",", ""))
        return -val if is_negative else val

    # プレーン数値
    s = s.replace(",", "")
    if not s:
        return None
    try:
        val = float(s)
        return -val if is_negative else val
    except ValueError:
        return None


def _classify_eps_text_period(context_text: str) -> str:
    """テキスト行の期間キーワードを判定する。"""
    s = unicodedata.normalize("NFKC", context_text)
    if re.search(r"通期|年間", s):
        return "full_year"
    if re.search(r"第2四半期累計|2Q累計|中間", s):
        return "q2_cumulative"
    return "unknown"


# ============================================================
# EPS テキスト抽出（forecast table fallback）
# ============================================================

_EPS_HEADER_KEYWORDS = [
    "1株当たり当期純利益",
    "1株当たり純利益",
]
_DIVIDEND_KEYWORDS = ["配当", "1株当たり配当金"]


def _extract_eps_from_forecast_table(plain_text: str) -> list[dict]:
    """業績予想テーブルのプレーンテキストから EPS 候補を抽出する。"""
    lines = plain_text.strip().split("\n")
    header_idx = None
    has_extra_right = False

    # ヘッダー行検索
    for i, line in enumerate(lines):
        norm = unicodedata.normalize("NFKC", line)
        for kw in _EPS_HEADER_KEYWORDS:
            nkw = unicodedata.normalize("NFKC", kw)
            if nkw in norm:
                header_idx = i
                eps_pos = norm.find(nkw)
                after = norm[eps_pos + len(nkw):]
                for dk in _DIVIDEND_KEYWORDS:
                    ndk = unicodedata.normalize("NFKC", dk)
                    if ndk in after:
                        has_extra_right = True
                break
        if header_idx is not None:
            break

    if header_idx is None:
        return []

    candidates: list[dict] = []
    for i in range(header_idx + 1, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        norm = unicodedata.normalize("NFKC", line)
        period_type = _classify_eps_text_period(norm)

        # 数値トークン抽出
        nums_raw = re.findall(r"[-−△▲]?\s*[\d,]+\.?\d*", norm)
        nums: list[float] = []
        for raw in nums_raw:
            v = _normalize_eps_text(raw)
            if v is not None:
                nums.append(v)
        if not nums:
            continue

        # EPS 列の値を選択
        if has_extra_right and len(nums) >= 2:
            eps_val = nums[-2]
        else:
            eps_val = nums[-1]

        # 異常値チェック
        if abs(eps_val) > _EPS_ABSURD_THRESHOLD:
            continue

        candidates.append({
            "metric": "eps",
            "value": eps_val,
            "period_type": period_type,
            "horizon": "next_year",
            "is_consol": True,
            "is_basic": True,
            "ctx": "text",
            "tag": "text_eps",
            "source": "text",
        })

    return candidates


# ============================================================
# 見通し（Outlook）テキスト抽出
# ============================================================

_OUTLOOK_HEADING_RE = re.compile(
    r"(今後の見通し|次期.*見通し|来期.*見通し|業績予想について|今後の.*見通し)",
)
_OUTLOOK_STOP_RE = re.compile(
    r"(継続企業|配当の状況|株主還元|配当予想|その他の経営|会計基準の選択)",
)


def _strip_html_tags(text: str) -> str:
    """簡易 HTML タグ除去。"""
    return re.sub(r"<[^>]+>", "", text)


def _extract_outlook_text(html_text: str) -> str:
    """見通しセクションのテキストを抽出する。

    継続企業・配当区画で打ち切り、품質ガード適用。
    """
    text = _strip_html_tags(html_text)
    lines = text.split("\n")

    start_idx = None
    for i, line in enumerate(lines):
        if _OUTLOOK_HEADING_RE.search(line):
            start_idx = i
            break
    if start_idx is None:
        return ""

    extracted: list[str] = []
    for i in range(start_idx + 1, min(start_idx + 60, len(lines))):
        line = lines[i].strip()
        if _OUTLOOK_STOP_RE.search(line):
            break
        extracted.append(line)

    result = "\n".join(extracted).strip()
    if not _is_outlook_quality_ok(result):
        return ""
    return result


def _is_outlook_quality_ok(text: str) -> bool:
    """見通しテキストの品質ガード。"""
    if len(text) < 20:
        return False
    # 意味のある日本語文字数
    jp_count = sum(1 for c in text if '\u3040' <= c <= '\u9fff' or '\u30a0' <= c <= '\u30ff')
    if jp_count < 10:
        return False
    has_period = "。" in text
    meaningful_lines = [l.strip() for l in text.split("\n") if l.strip()]
    has_multi_lines = len(meaningful_lines) >= 2
    if not has_period and not has_multi_lines:
        return False
    return True


# ============================================================
# make_fallback_summary
# ============================================================

_NOISE_LINE_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_RULED_LINE_RE = re.compile(r"^[━─═＝\-=]{3,}")
_HEADING_LINE_RE = re.compile(
    r"^[\d０-９]*[．.\s]*(?:今後の見通し|見通し|業績予想)"
)


def make_fallback_summary(outlook_text: str, max_len: int = 200) -> str:
    """見通しテキストをノイズ除去して短縮する。"""
    lines = outlook_text.strip().split("\n")
    clean: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _HEADING_LINE_RE.match(line):
            continue
        if _NOISE_LINE_RE.match(line):
            continue
        if _RULED_LINE_RE.match(line):
            continue
        clean.append(line)

    result = "\n".join(clean).strip()
    result = re.sub(r"[ \u3000]{2,}", " ", result)
    if len(result) > max_len:
        result = result[:max_len - 1] + "…"
    return result


# ============================================================
# format_guidance_section
# ============================================================

def _fmt_oku_yen(val: float) -> str:
    """円単位の値を億円表記にフォーマット。"""
    oku = val / 1e8
    if abs(oku) >= 10:
        return f"{oku:,.0f}億円"
    return f"{oku:,.1f}億円"


def _fmt_yoy(ratio: float | None) -> str:
    if ratio is None:
        return ""
    sign = "+" if ratio >= 0 else ""
    return f"{sign}{ratio * 100:.1f}%"


def format_guidance_section(guidance: GuidanceData, clip: float | None = None) -> str:
    """通知用ガイダンスセクションを生成する。

    ガイダンスも見通しもなければ空文字を返す。
    """
    if not guidance.has_guidance and not guidance.has_outlook:
        return ""

    parts: list[str] = []

    if guidance.has_guidance:
        lines = ["■ 来期ガイダンス"]
        if guidance.sales_forecast is not None:
            line = f"売上: {_fmt_oku_yen(guidance.sales_forecast)}"
            if guidance.sales_yoy is not None:
                line += f"（YOY {_fmt_yoy(guidance.sales_yoy)}）"
            lines.append(line)
        if guidance.op_forecast is not None:
            line = f"OP: {_fmt_oku_yen(guidance.op_forecast)}"
            if guidance.op_yoy is not None:
                line += f"（YOY {_fmt_yoy(guidance.op_yoy)}）"
            lines.append(line)
        if guidance.eps_forecast is not None:
            line = f"EPS: {guidance.eps_forecast}円"
            if guidance.eps_yoy is not None:
                line += f"（YOY {_fmt_yoy(guidance.eps_yoy)}）"
            lines.append(line)
        parts.append("\n".join(lines))

    if guidance.has_outlook:
        parts.append(f"■ 見通し\n{guidance.outlook_summary}")

    return "\n\n".join(parts)


# ============================================================
# HTML バイト列のエンコーディング自動検出
# ============================================================

def _decode_html_bytes(raw: bytes) -> str:
    """HTMLバイト列をデコードする。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    head = raw[:2000].lower()
    m = re.search(rb'charset[="\s]+([a-z0-9_-]+)', head)
    if m:
        cs = m.group(1).decode("ascii", errors="ignore")
        cs_map = {"shift_jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
                   "euc-jp": "euc_jp", "iso-2022-jp": "iso2022_jp"}
        cs = cs_map.get(cs, cs)
        try:
            return raw.decode(cs, errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace")


# ============================================================
# XBRL Forecast 候補収集
# ============================================================

def _apply_scale(text: str, scale: str, sign: str) -> float | None:
    """iXBRL scale/sign を適用して float を返す。"""
    s = unicodedata.normalize("NFKC", text).strip().replace(",", "")
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if scale:
        try:
            val = val * (10 ** int(scale))
        except (ValueError, OverflowError):
            pass
    if sign == "-" and val > 0:
        val = -val
    return val


def _collect_forecast_candidates_from_bytes(raw: bytes) -> list[dict]:
    """XBRL/iXBRL バイト列から Forecast 候補を収集する。"""
    try:
        # XBRL クリーン処理
        try:
            from src.xbrl_clean import read_xbrl_bytes
            xml_str = read_xbrl_bytes(raw)
        except ImportError:
            xml_str = raw.decode("utf-8", errors="replace")
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    candidates: list[dict] = []

    # ---- Pass 1: 従来 XBRL ----
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        ctx = elem.get("contextRef", "")
        if "ForecastMember" not in ctx:
            continue

        # 財務指標
        if tag_local in _FORECAST_TAG_MAP:
            field_name = _FORECAST_TAG_MAP[tag_local]
            val_text = (elem.text or "").strip()
            if not val_text:
                continue
            s = unicodedata.normalize("NFKC", val_text).replace(",", "")
            try:
                val = float(s)
            except ValueError:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            candidates.append({
                "metric": field_name,
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": True,
                "ctx": ctx,
                "tag": tag_local,
                "source": "xbrl",
            })

        # EPS
        if tag_local in _EPS_ALL_TAGS:
            val_text = (elem.text or "").strip()
            if not val_text:
                continue
            s = unicodedata.normalize("NFKC", val_text).replace(",", "")
            try:
                val = float(s)
            except ValueError:
                continue
            if abs(val) > _EPS_ABSURD_THRESHOLD:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            is_basic = tag_local in _EPS_BASIC_TAGS
            candidates.append({
                "metric": "eps",
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": is_basic,
                "ctx": ctx,
                "tag": tag_local,
                "source": "xbrl",
            })

    if candidates:
        return candidates

    # ---- Pass 2: iXBRL ----
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        tag_local = tag.split("}")[-1] if "}" in tag else tag
        if tag_local != "nonFraction":
            continue

        concept = elem.get("name", "")
        ctx = elem.get("contextRef", "")
        scale = elem.get("scale", "")
        sign = elem.get("sign", "")
        if not concept or "ForecastMember" not in ctx:
            continue

        concept_local = concept.split(":")[-1] if ":" in concept else concept

        text = (elem.text or "").strip()
        if not text:
            text = "".join(elem.itertext()).strip()
        if not text:
            continue

        # 財務指標
        if concept_local in _FORECAST_TAG_MAP:
            field_name = _FORECAST_TAG_MAP[concept_local]
            val = _apply_scale(text, scale, sign)
            if val is None:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            candidates.append({
                "metric": field_name,
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": True,
                "ctx": ctx,
                "tag": concept_local,
                "source": "ixbrl",
            })

        # EPS
        if concept_local in _EPS_ALL_TAGS:
            val = _apply_scale(text, scale, sign)
            if val is None or abs(val) > _EPS_ABSURD_THRESHOLD:
                continue
            is_consol = "Consolidated" in ctx and "NonConsolidated" not in ctx
            is_basic = concept_local in _EPS_BASIC_TAGS
            candidates.append({
                "metric": "eps",
                "value": val,
                "period_type": _classify_period_type(ctx),
                "horizon": _classify_horizon(ctx),
                "is_consol": is_consol,
                "is_basic": is_basic,
                "ctx": ctx,
                "tag": concept_local,
                "source": "ixbrl",
            })

    return candidates


# ============================================================
# プレーンテキスト抽出（ZIP内 HTML → テキスト）
# ============================================================

def _extract_plain_text_from_zip(zf: zipfile.ZipFile) -> str:
    """ZIP 内の HTML/iXBRL からプレーンテキストを抽出する。"""
    texts: list[str] = []
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            raw = zf.read(name)
            html = _decode_html_bytes(raw)
            plain = _strip_html_tags(html)
            if plain.strip():
                texts.append(plain)
        except Exception:
            continue
    return "\n".join(texts)


# ============================================================
# extract_guidance_from_zip
# ============================================================

def extract_guidance_from_zip(
    xbrl_path: str,
    actual_sales: int | None = None,
    actual_op: int | None = None,
) -> GuidanceData:
    """XBRL ZIP から来期ガイダンスデータを抽出する。"""
    guidance = GuidanceData(
        sales_actual=actual_sales,
        op_actual=actual_op,
    )

    try:
        raw = Path(xbrl_path).read_bytes()
    except Exception as e:
        logger.warning(f"[GUIDANCE] file read failed: {e}")
        return guidance

    if raw[:4] != _ZIP_SIG:
        logger.warning("[GUIDANCE] not a ZIP file")
        return guidance

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), "r")
    except zipfile.BadZipFile:
        logger.warning("[GUIDANCE] bad ZIP file")
        return guidance

    # ---- XBRL/iXBRL 候補収集 ----
    all_candidates: list[dict] = []
    plain_text = ""
    outlook_html = ""

    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        is_ixbrl = any(bn.endswith(ext) for ext in _IXBRL_EXTS)
        is_xbrl = any(bn.endswith(ext) for ext in _XBRL_EXTS)
        if not is_ixbrl and not is_xbrl:
            continue
        try:
            entry_bytes = zf.read(name)
            cands = _collect_forecast_candidates_from_bytes(entry_bytes)
            all_candidates.extend(cands)
        except Exception as e:
            logger.debug(f"[GUIDANCE] parse failed: {name}: {e}")

    # ---- プレーンテキスト + Outlook HTML ----
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            entry_bytes = zf.read(name)
            html = _decode_html_bytes(entry_bytes)
            pt = _strip_html_tags(html)
            if pt.strip():
                plain_text += "\n" + pt
            if not outlook_html and _OUTLOOK_HEADING_RE.search(html):
                outlook_html = html
        except Exception:
            continue

    zf.close()

    # ---- EPS テキスト fallback ----
    if plain_text:
        text_eps = _extract_eps_from_forecast_table(plain_text)
        all_candidates.extend(text_eps)

    # ---- ベスト候補選択 ----
    if all_candidates:
        best = _select_best_candidates(all_candidates)
        guidance.sales_forecast = best.get("sales")
        guidance.op_forecast = best.get("operating_profit")
        guidance.ordinary_forecast = best.get("ordinary_profit")
        guidance.net_income_forecast = best.get("net_income")
        guidance.eps_forecast = best.get("eps")

    # ---- Outlook 抽出 ----
    if outlook_html:
        guidance.outlook_text = _extract_outlook_text(outlook_html)
    elif plain_text:
        guidance.outlook_text = _extract_outlook_text(plain_text)

    logger.info(
        f"[GUIDANCE] extracted: sales={guidance.sales_forecast} "
        f"op={guidance.op_forecast} eps={guidance.eps_forecast} "
        f"outlook_len={len(guidance.outlook_text)}"
    )
    return guidance
