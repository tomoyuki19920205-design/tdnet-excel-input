"""Deterministic, source-linked IPO analysis reports for Company Viewer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from types import SimpleNamespace
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.common_ticker import strip_tdnet_trailing_zero

logger = logging.getLogger("tdnet.ipo_analysis")

SCHEMA_VERSION = "ipo_analysis_v1"
REPORT_VERSION = 1
PROMPT_VERSION = "ipo-analysis-explanation-v1"
REPORT_TYPE = "ipo_analysis"
JST = timezone(timedelta(hours=9))
JPX_LISTING_URL = "https://www.jpx.co.jp/listing/stocks/new/"
VALID_STATUSES = {"pending", "collecting", "completed", "partial", "failed"}

RELATED_TITLE_TERMS = (
    "上場に伴う当社決算情報", "上場に伴う決算情報", "事業計画及び成長可能性",
    "会社説明及び今後の戦略", "主要株主", "上場目的の開示", "上場のお知らせ",
)

SECTION_TITLES = (
    "01 基本情報", "02 事業内容", "03 業績実績", "04 業績予想・配当・株価指標",
    "05 財務・キャッシュフロー", "06 公募・売出し・公開規模", "07 需給・重要注意点",
    "08 主要株主と売出後残高", "09 期間別ロックアップ", "10 OA・親引け",
    "11 売却可能株・VC", "12 ストックオプション全回号", "13 SOの行使・売却条件",
    "14 成長性・重要KPI", "15 利益の質・キャッシュフロー", "16 競争・主要顧客・海外",
    "17 強み・成長投資", "18 懸念・技術／AIの影響",
    "19 資料間差異・最終採用値・計算根拠", "20 出典・未確認事項",
)


def normalize_ticker(value: str) -> str:
    return strip_tdnet_trailing_zero(unicodedata.normalize("NFKC", str(value or "")).strip().upper())


def stable_key(ticker: str, listing_date: str, version: int = REPORT_VERSION) -> str:
    return f"{normalize_ticker(ticker)}|{listing_date}|{REPORT_TYPE}|v{version}"


def stable_report_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"company-viewer:{key}"))


def is_related_source_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(title or "")))
    return any(term in normalized for term in RELATED_TITLE_TERMS)


def _field(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def parse_number(value: str | int | float | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    negative = text.startswith("△") or (text.startswith("(") and text.endswith(")"))
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    return -abs(number) if negative else number


def to_base_units(value: str | int | float, unit: str) -> float:
    number = parse_number(value)
    if number is None:
        raise ValueError(f"not numeric: {value!r}")
    normalized = unicodedata.normalize("NFKC", unit)
    multiplier = {"円": 1, "千円": 1_000, "百万円": 1_000_000, "億円": 100_000_000,
                  "株": 1, "千株": 1_000, "%": 1}.get(normalized)
    if multiplier is None:
        raise ValueError(f"unsupported unit: {unit}")
    return number * multiplier


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pdf_page_count_and_text(data: bytes) -> tuple[int, str]:
    import pdfplumber
    from io import BytesIO
    with pdfplumber.open(BytesIO(data)) as pdf:
        return len(pdf.pages), "\n".join((page.extract_text() or "") for page in pdf.pages)


def _download_source(url: str, session: requests.Session) -> tuple[bytes, str, int | None, str]:
    response = session.get(url, timeout=60)
    response.raise_for_status()
    data = response.content
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        pages, text = _pdf_page_count_and_text(data)
        return data, content_type, pages, text
    return data, content_type, None, response.text


def _financial_source_pages(data: bytes) -> list[int]:
    """Return the 1-based PDF pages that supported canonical IPO PL extraction."""
    from src.ipo_listing_financials import extract_ipo_listing_financials

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            temporary.write(data)
            temporary_path = temporary.name
        extraction = extract_ipo_listing_financials(temporary_path)
        return list(extraction.source_pages)
    except Exception as exc:
        logger.warning("could not identify IPO financial source pages: %s", exc)
        return []
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def collect_jpx_listing_sources(ticker: str, session: requests.Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect the official JPX row and its linked primary PDFs without ticker-specific URLs."""
    response = session.get(JPX_LISTING_URL, timeout=60)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")
    normalized = normalize_ticker(ticker)
    anchor_row = next((tr for tr in soup.select("tr") if normalized in tr.get_text(" ", strip=True)), None)
    if anchor_row is None:
        return [], {}
    detail_row = anchor_row.find_next_sibling("tr")
    anchor_cells = [td.get_text(" ", strip=True) for td in anchor_row.find_all("td", recursive=False)]
    detail_cells = [td.get_text(" ", strip=True) for td in detail_row.find_all("td", recursive=False)] if detail_row else []
    offering: dict[str, Any] = {}
    if len(anchor_cells) >= 8:
        offering.update({"listing_date": anchor_cells[0].split()[0].replace("/", "-"),
                         "indicated_range": anchor_cells[-3],
                         "public_offering_shares": to_base_units(anchor_cells[-2], "千株")})
    if len(detail_cells) >= 5:
        offering["market"] = detail_cells[0]
        offering["offering_price"] = parse_number(detail_cells[-3])
        secondary = detail_cells[-2]
        main_match = re.match(r"([0-9,.]+)", secondary)
        oa_match = re.search(r"OA\s*([0-9,.]+)", secondary, re.I)
        if main_match:
            offering["secondary_shares"] = to_base_units(main_match.group(1), "千株")
        if oa_match:
            offering["oa_shares"] = to_base_units(oa_match.group(1), "千株")
    links: list[dict[str, Any]] = [{
        "title": "JPX 新規上場会社情報", "url": JPX_LISTING_URL, "issuer": "日本取引所グループ",
        "document_id": f"JPX-LIST-{normalized}", "published_at": offering.get("listing_date"),
        "version_relation": "current", "kind": "html", "raw_bytes": response.content,
        "page_count": None, "text": anchor_row.get_text(" ", strip=True) + " " + (detail_row.get_text(" ", strip=True) if detail_row else ""),
    }]
    seen: set[str] = set()
    for link in [*anchor_row.select("a[href$='.pdf']"), *(detail_row.select("a[href$='.pdf']") if detail_row else [])]:
        url = urljoin(JPX_LISTING_URL, link.get("href", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        name = Path(url).name.lower()
        title = "新規上場会社概要" if "outline" in name else (
            "新規上場申請のための有価証券報告書" if "1s" in name else
            "新規上場申請書類" if "1ts" in name else
            "コーポレート・ガバナンス報告書" if "cg" in name else "適正性確認書"
        )
        links.append({"title": title, "url": url, "issuer": "日本取引所グループ",
                      "document_id": Path(url).stem, "published_at": None,
                      "version_relation": "current", "kind": "pdf"})
    return links, offering


def collect_sources(ticker: str, listing_date: str, tdnet_documents: Iterable[Any], *, session: requests.Session | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    client = session or requests.Session()
    normalized = normalize_ticker(ticker)
    documents: list[dict[str, Any]] = []
    for item in tdnet_documents:
        item_ticker = normalize_ticker(_field(item, "ticker"))
        title = _field(item, "title")
        if item_ticker != normalized or not is_related_source_title(title):
            continue
        documents.append({
            "title": title,
            "url": _field(item, "doc_url", _field(item, "url")),
            "issuer": _field(item, "company_name"),
            "document_id": _field(item, "disc_no", _field(item, "source_doc_id")),
            "published_at": _field(item, "published_at", None),
            "version_relation": f"revision-{_field(item, 'rev_no', '1')}", "kind": "pdf",
        })
    jpx_sources, offering = collect_jpx_listing_sources(normalized, client)
    documents.extend(jpx_sources)
    manifests: list[dict[str, Any]] = []
    source_text: dict[str, str] = {}
    for index, document in enumerate(documents, 1):
        source_id = f"P{index:02d}"
        try:
            raw = document.pop("raw_bytes", None)
            text = document.pop("text", "")
            page_count = document.pop("page_count", None)
            if raw is None:
                raw, content_type, page_count, text = _download_source(document["url"], client)
            else:
                content_type = "text/html"
            used_pdf_pages = (
                _financial_source_pages(raw)
                if "pdf" in content_type.lower() and "決算情報" in document["title"]
                else []
            )
            manifests.append({**document, "source_id": source_id, "fetch_status": "success",
                              "sha256": _sha256(raw), "page_count": page_count,
                              "used_pages": used_pdf_pages,
                              "used_pdf_pages": used_pdf_pages,
                              # The source PDFs do not expose a reliable printed-page mapping.
                              # Keep it explicitly unresolved rather than assuming it equals the PDF index.
                              "used_printed_pages": [], "content_type": content_type})
            source_text[source_id] = text
        except Exception as exc:
            manifests.append({**document, "source_id": source_id, "fetch_status": "failed",
                              "sha256": None, "page_count": None, "used_pages": [],
                              "used_pdf_pages": [], "used_printed_pages": [],
                              "error": str(exc)[:300]})
    return manifests, offering, source_text


def calculate_offering(offering: dict[str, Any]) -> list[dict[str, Any]]:
    required = ("offering_price", "public_offering_shares", "secondary_shares", "oa_shares")
    if any(offering.get(key) is None for key in required):
        return []
    price = float(offering["offering_price"])
    public = float(offering["public_offering_shares"])
    secondary = float(offering["secondary_shares"])
    oa = float(offering["oa_shares"])
    public_ex_oa = public + secondary
    public_in_oa = public_ex_oa + oa
    result = [
        _calc("public_shares_ex_oa", public_ex_oa, "shares", "public_offering_shares + secondary_shares", [public, secondary]),
        _calc("public_shares_including_oa", public_in_oa, "shares", "public_shares_ex_oa + oa_shares", [public_ex_oa, oa]),
        _calc("absorption_amount_jpy", public_in_oa * price, "JPY", "public_shares_including_oa * offering_price", [public_in_oa, price]),
        _calc("new_issue_gross_jpy", public * price, "JPY", "public_offering_shares * offering_price", [public, price]),
        _calc("secondary_gross_jpy", secondary * price, "JPY", "secondary_shares * offering_price", [secondary, price]),
    ]
    post = offering.get("post_listing_shares")
    if post:
        post = float(post)
        result.extend([
            _calc("public_float_ratio_pct", public_in_oa / post * 100, "%", "public_shares_including_oa / post_listing_shares * 100", [public_in_oa, post]),
            _calc("market_cap_jpy", post * price, "JPY", "post_listing_shares * offering_price", [post, price]),
        ])
    return result


def _calc(name: str, value: float, unit: str, formula: str, inputs: list[float]) -> dict[str, Any]:
    return {"name": name, "value": value, "unit": unit, "formula": formula,
            "inputs": inputs, "unrounded_value": value}


def group_financials(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        kind = "forecast" if row.get("source") == "tdnet_forecast" else "actual"
        key = (str(row.get("period")), str(row.get("quarter")), kind)
        group = grouped.setdefault(key, {"period": key[0], "quarter": key[1], "kind": kind,
                                         "metrics": {}, "source_row_keys": []})
        group["metrics"][str(row.get("metric"))] = row.get("value")
        group["source_row_keys"].append(row.get("source_row_key"))
    return [grouped[key] for key in sorted(grouped)]


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "未確認"
    number = float(value)
    formatted = f"{number:,.{digits}f}"
    return formatted if digits == 0 else formatted.rstrip("0").rstrip(".")


def _source_link(source: dict[str, Any], pages: str = "") -> str:
    if not pages:
        pages = ",".join(str(page) for page in source.get("used_pdf_pages", source.get("used_pages", [])))
    label = source["source_id"] + (f" PDF p.{pages}" if pages else "")
    return f"[{label}]({source['url']})"


def render_report(*, ticker: str, company_name: str, listing_date: str, market: str | None,
                  status: str, manifests: list[dict[str, Any]], financials: list[dict[str, Any]],
                  offering: dict[str, Any], calculations: list[dict[str, Any]], ai_text: str | None,
                  missing: list[str], generated_at: str) -> str:
    sources_ok = [source for source in manifests if source["fetch_status"] == "success"]
    financial_source = next((source for source in sources_ok if "決算情報" in source["title"]), sources_ok[0] if sources_ok else None)
    citation = _source_link(financial_source) if financial_source else "[未確認]"
    calc_by_name = {row["name"]: row for row in calculations}
    actuals = [row for row in financials if row["kind"] == "actual"]
    forecasts = [row for row in financials if row["kind"] == "forecast"]
    lines = [f"# {company_name}（{ticker}）IPO分析", "", f"- 作成日時: {generated_at}",
             f"- 上場日: {listing_date}", f"- 市場: {market or '未確認'}", "- 決算期: 資料の各期間欄を参照",
             "- 連結／単体: 資料上確認できず", "- 会計基準: 資料上確認できず",
             f"- 調査状態: {status}", f"- 使用資料数: {len(sources_ok)}",
             "- 注意: 添付収録資料に基づく上場時点の分析であり、現在株価の分析ではありません。", "",
             "## 目次", *[f"- {title}" for title in SECTION_TITLES], ""]
    lines += ["## 01 基本情報", f"- [確認済み] 証券コード: {ticker}", f"- [確認済み] 上場日: {listing_date}",
              f"- [確認済み] 上場市場: {market or '未確認'}", "- [未確認] 設立日、本店所在地、代表者、主幹事、監査法人はsource manifestの会社概要を参照。", ""]
    lines += ["## 02 事業内容", f"- {ai_text}" if ai_text else "- [未確認] 検証済み説明文を生成できませんでした。", ""]
    lines += ["## 03 業績実績", "|期間|区分|売上高|売上総利益|営業利益|経常利益|純利益|EPS|出典|",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in actuals:
        m = row["metrics"]
        lines.append(f"|{row['period']} {row['quarter']}|累計実績|{_fmt(m.get('sales'))}百万円|{_fmt(m.get('gross_profit'))}百万円|{_fmt(m.get('operating_profit'))}百万円|{_fmt(m.get('ordinary_profit'))}百万円|{_fmt(m.get('net_income'))}百万円|{_fmt(m.get('eps'),2)}円|{citation}|")
    lines += ["", "## 04 業績予想・配当・株価指標"]
    if forecasts:
        lines += ["|期間|売上高|営業利益|経常利益|純利益|EPS|出典|", "|---|---:|---:|---:|---:|---:|---|"]
        for row in forecasts:
            m = row["metrics"]
            lines.append(f"|{row['period']} {row['quarter']}|{_fmt(m.get('sales'))}百万円|{_fmt(m.get('operating_profit'))}百万円|{_fmt(m.get('ordinary_profit'))}百万円|{_fmt(m.get('net_income'))}百万円|{_fmt(m.get('eps'),2)}円|{citation}|")
    else:
        lines.append("- [未確認] 会社公表予想を確認できませんでした。")
    lines += ["- [未確認] 配当予想、公開価格ベースPER・PSR、経営陣持株比率は資料照合未完了。", "",
              "## 05 財務・キャッシュフロー", "- [未確認] 総資産、純資産、現金、有利子負債、各CFの構造化照合は未完了。", "",
              "## 06 公募・売出し・公開規模"]
    if offering.get("offering_price") is not None:
        lines += [f"- [確認済み] 公開価格: {_fmt(offering['offering_price'],0)}円",
                  f"- [確認済み] 公募株数: {_fmt(offering.get('public_offering_shares'),0)}株",
                  f"- [確認済み] 売出株数: {_fmt(offering.get('secondary_shares'),0)}株",
                  f"- [確認済み] OA株数: {_fmt(offering.get('oa_shares'),0)}株"]
        for name, label in (("public_shares_including_oa", "公開株数（OA含む）"), ("absorption_amount_jpy", "吸収金額"),
                            ("market_cap_jpy", "公開時時価総額"), ("public_float_ratio_pct", "公開株比率")):
            if name in calc_by_name:
                row = calc_by_name[name]
                lines.append(f"- [計算値] {label}: {_fmt(row['value'],3)} {row['unit']}（式: `{row['formula']}`）")
    else:
        lines.append("- [未確認] 公募・売出し条件を構造化確認できませんでした。")
    placeholders = {
        "07 需給・重要注意点": "VC、ロックアップ、親引け、潜在株の構造化照合は未完了。",
        "08 主要株主と売出後残高": "上位株主・売出人別残高の構造化照合は未完了。",
        "09 期間別ロックアップ": "期間・解除条件別株数の構造化照合は未完了。",
        "10 OA・親引け": "OAの貸株元・グリーンシュー・親引け条件は未確認。",
        "11 売却可能株・VC": "時点別潜在売却可能株数とVC残高は未確認。",
        "12 ストックオプション全回号": "全回号の潜在株数・行使価格・期間は未確認。",
        "13 SOの行使・売却条件": "上場・退職・段階行使・売却制限条件は未確認。",
        "14 成長性・重要KPI": "定義と期間を検証できたKPIはありません。",
        "15 利益の質・キャッシュフロー": "営業CFと利益の乖離、一時損益、運転資本の照合は未完了。",
        "16 競争・主要顧客・海外": "競合名、市場シェア、顧客集中、海外売上は推測せず未確認。",
        "17 強み・成長投資": "競争優位と資金使途は一次資料の追加構造化が必要。",
        "18 懸念・技術／AIの影響": "資料に基づくAI影響の確認ができず、推測を避けました。",
    }
    for title, message in placeholders.items():
        lines += ["", f"## {title}", f"- [未確認] {message}"]
    lines += ["", "## 19 資料間差異・最終採用値・計算根拠",
              "- [確認済み] 業績値は上場時決算資料の期間・実績／予想区分を維持して採用。",
              "- [未確認] 訂正届出書を含む株式数・SO・ロックアップの版間照合は未完了。", "",
              "## 20 出典・未確認事項"]
    for source in manifests:
        suffix = f" / SHA-256 `{source['sha256']}`" if source.get("sha256") else ""
        lines.append(f"- {source['source_id']}: [{source['title']}]({source['url']}) / {source['fetch_status']}{suffix}")
    lines += ["", "### 未確認事項", *[f"- {item}" for item in missing]]
    return "\n".join(lines).strip() + "\n"


def generate_ai_explanation(facts: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """AI may phrase verified facts, but may not introduce any number or citation."""
    model = os.environ.get("IPO_ANALYSIS_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY is not configured", model
    prompt = (
        "次の検証済みfact JSONだけを根拠に、IPO時点の事業説明を日本語で二文以内にしてください。"
        "数字、固有の数値、引用記号、出典番号、推測を一切書かず、各文を[分析]で始めてください。\n"
        + json.dumps(facts, ensure_ascii=False, sort_keys=True)
    )
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "input": prompt, "temperature": 0}, timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    text = payload.get("output_text")
    if not text:
        chunks = []
        for output in payload.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        text = "\n".join(chunks).strip()
    if not text or re.search(r"\d", text):
        return None, "AI output was empty or introduced a numeric token", model
    return text.strip(), None, model


def validate_report(markdown: str, manifests: list[dict[str, Any]], calculations: list[dict[str, Any]], financials: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    for title in SECTION_TITLES:
        if f"## {title}" not in markdown:
            errors.append(f"missing_section:{title}")
    source_ids = {source["source_id"] for source in manifests}
    for reference in re.findall(r"\[(P\d{2})(?: p\.[^\]]+)?\]", markdown):
        if reference not in source_ids:
            errors.append(f"unresolved_source:{reference}")
    if "base64" in markdown.lower() or re.search(r"data:image/", markdown, re.I):
        errors.append("embedded_image_forbidden")
    if any(period.get("quarter") not in {"FY", "1Q", "2Q", "3Q"} for period in financials):
        errors.append("invalid_period_kind")
    if any(calc.get("unrounded_value") is None or not calc.get("formula") for calc in calculations):
        errors.append("invalid_calculation")
    return {"passed": not errors, "errors": errors, "checks": {
        "fixed_20_sections": len([title for title in SECTION_TITLES if f"## {title}" in markdown]),
        "all_citations_resolve": not any(error.startswith("unresolved_source") for error in errors),
        "no_embedded_images": "embedded_image_forbidden" not in errors,
        "period_kinds_valid": "invalid_period_kind" not in errors,
        "calculation_formulas_present": "invalid_calculation" not in errors,
    }}


def build_report_payload(*, event_id: str, ticker: str, company_name: str, listing_date: str,
                         manifests: list[dict[str, Any]], offering: dict[str, Any],
                         financial_rows: list[dict[str, Any]], source_text: dict[str, str] | None = None) -> dict[str, Any]:
    normalized = normalize_ticker(ticker)
    financials = group_financials(financial_rows)
    # Generic extraction from JPX outline, when available.
    for source in manifests:
        if source.get("title") != "新規上場会社概要" or not source_text:
            continue
        text = source_text.get(source["source_id"], "")
        match = re.search(r"上場時発行済株式総数\s*([0-9,]+)株", text)
        if match:
            offering["post_listing_shares"] = float(match.group(1).replace(",", ""))
    calculations = calculate_offering(offering)
    failed_sources = [source["source_id"] for source in manifests if source["fetch_status"] != "success"]
    missing = []
    if not offering.get("offering_price"):
        missing.append("公開価格・公募・売出・OAの確定値")
    if offering.get("post_listing_shares") is None:
        missing.append("上場時発行済株式数")
    missing += ["主要株主・売出後残高", "ロックアップ期間別株数", "SO全回号・失効分・希薄化率",
                "財務・キャッシュフロー詳細", "KPI定義と時系列", "訂正届出書を含む版間差異"]
    if failed_sources:
        missing.append("取得失敗資料: " + ", ".join(failed_sources))
    facts = {"ticker": normalized, "company_name": company_name, "listing_date": listing_date,
             "market": offering.get("market"), "financial_periods": financials,
             "offering": offering, "source_count": len([s for s in manifests if s["fetch_status"] == "success"])}
    ai_text, ai_error, model = generate_ai_explanation({
        "company_name": company_name, "market": offering.get("market"),
        "source_titles": [s["title"] for s in manifests if s["fetch_status"] == "success"],
        "has_actual_financials": any(p["kind"] == "actual" for p in financials),
        "has_forecast_financials": any(p["kind"] == "forecast" for p in financials),
    })
    if ai_error:
        missing.append("AI説明文: " + ai_error)
    generated_at = datetime.now(JST).isoformat(timespec="seconds")
    status = "partial" if missing else "completed"
    markdown = render_report(ticker=normalized, company_name=company_name, listing_date=listing_date,
                             market=offering.get("market"), status=status, manifests=manifests,
                             financials=financials, offering=offering, calculations=calculations,
                             ai_text=ai_text, missing=missing, generated_at=generated_at)
    validation = validate_report(markdown, manifests, calculations, financials)
    if not validation["passed"]:
        status = "partial"
    key = stable_key(normalized, listing_date)
    content_hash = _sha256(markdown.encode("utf-8"))
    return {"id": stable_report_id(key), "stable_key": key, "schema_version": SCHEMA_VERSION,
            "report_type": REPORT_TYPE, "report_version": REPORT_VERSION, "event_id": event_id,
            "ticker": normalized, "company_name": company_name, "listing_date": listing_date,
            "market": offering.get("market"), "status": status, "source_manifest": manifests,
            "facts": facts, "calculations": calculations, "validation_result": validation,
            "report_markdown": markdown, "generation_model": model if ai_text else None,
            "prompt_version": PROMPT_VERSION, "content_sha256": content_hash,
            "last_error": ai_error, "generated_at": generated_at, "updated_at": generated_at}


def payload_storage_bytes(payload: dict[str, Any]) -> int:
    stored = {key: payload.get(key) for key in ("source_manifest", "facts", "calculations", "validation_result", "report_markdown", "generation_model", "prompt_version")}
    return len(json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def enqueue_ipo_analysis(item: Any, event_id: str | None = None, *, upsert=None, select_event=None) -> dict[str, Any]:
    """Create an idempotent pending report row after the notification is safe."""
    from lib.pipeline.db import supabase_upsert
    ticker = normalize_ticker(getattr(item, "ticker", ""))
    listing_date = str(getattr(item, "published_at", ""))[:10]
    if not event_id and select_event:
        event_id = select_event(item)
    if not event_id:
        return {"ok": False, "error": "event_id_not_resolved"}
    key = stable_key(ticker, listing_date)
    now = datetime.now(JST).isoformat(timespec="seconds")
    row = {"id": stable_report_id(key), "stable_key": key, "schema_version": SCHEMA_VERSION,
           "report_type": REPORT_TYPE, "report_version": REPORT_VERSION, "event_id": event_id,
           "ticker": ticker, "company_name": getattr(item, "company_name", ""),
           "listing_date": listing_date, "status": "pending", "prompt_version": PROMPT_VERSION,
           "updated_at": now}
    writer = upsert or supabase_upsert
    return writer("ipo_analysis_reports", row, on_conflict="stable_key")


def _rest_config() -> tuple[str, dict[str, str]]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("Supabase write configuration is unavailable")
    return url + "/rest/v1", {"apikey": key, "Authorization": f"Bearer {key}"}


def _rest_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    rest, headers = _rest_config()
    response = requests.get(f"{rest}/{table}", headers=headers, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected {table} response")
    return data


def _rest_patch(table: str, filters: dict[str, str], payload: dict[str, Any]) -> list[dict[str, Any]]:
    rest, headers = _rest_config()
    response = requests.patch(
        f"{rest}/{table}", headers={**headers, "Prefer": "return=representation"},
        params=filters, json=payload, timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def ensure_pending_report(item: Any, event_id: str | None = None) -> dict[str, Any]:
    """Resolve the card and create one pending queue/report row without downgrading an existing report."""
    from lib.pipeline.db import supabase_upsert
    ticker = normalize_ticker(_field(item, "ticker"))
    listing_date = str(_field(item, "published_at"))[:10]
    key = stable_key(ticker, listing_date)
    existing = _rest_get("ipo_analysis_reports", {"select": "id,status", "stable_key": f"eq.{key}", "limit": "1"})
    if existing:
        return {"ok": True, "action": "existing", "id": existing[0]["id"], "status": existing[0]["status"]}
    if not event_id:
        events = _rest_get("tdnet_events", {
            "select": "id", "ticker": f"eq.{ticker}", "source_url": f"eq.{_field(item, 'doc_url')}", "limit": "1",
        })
        event_id = events[0]["id"] if events else None
    if not event_id:
        return {"ok": False, "action": "deferred", "error": "event_id_not_resolved"}
    result = enqueue_ipo_analysis(item, event_id, upsert=supabase_upsert)
    return {**result, "action": "inserted" if result.get("ok") else "error", "id": stable_report_id(key)}


def process_pending_reports(*, listing_date: str | None = None, tickers: Iterable[str] | None = None,
                            apply: bool = True, pending_only: bool = True,
                            retry_partial: bool = False) -> dict[str, Any]:
    """Collect, validate and idempotently publish pending IPO reports."""
    from lib.pipeline.db import supabase_upsert
    from src.jquants.adapter import fetch_jquants_disclosures
    params = {"select": "*", "order": "listing_date.asc,ticker.asc", "limit": "100"}
    if listing_date:
        params["listing_date"] = f"eq.{listing_date}"
    if pending_only:
        params["status"] = "in.(pending,collecting,failed,partial)" if retry_partial else "in.(pending,collecting,failed)"
    normalized_tickers = {normalize_ticker(value) for value in (tickers or [])}
    queued = _rest_get("ipo_analysis_reports", params)
    if normalized_tickers:
        queued = [row for row in queued if row["ticker"] in normalized_tickers]
    documents_by_date: dict[str, list[Any]] = {}
    results: list[dict[str, Any]] = []
    for report in queued:
        date = report["listing_date"]
        ticker = report["ticker"]
        try:
            if apply:
                _rest_patch("ipo_analysis_reports", {"stable_key": f"eq.{report['stable_key']}"},
                            {"status": "collecting", "updated_at": datetime.now(JST).isoformat(timespec="seconds")})
            if date not in documents_by_date:
                documents_by_date[date] = fetch_jquants_disclosures(date.replace("-", ""))
            docs = documents_by_date[date]
            manifests, offering, source_text = collect_sources(ticker, date, docs)
            financial_rows = _rest_get("canonical_financials", {
                "select": "ticker,period,quarter,metric,value,unit,source,filing_id,source_row_key,document_type,period_start,period_end",
                "ticker": f"eq.{ticker}", "document_type": "eq.ipo_listing_financials", "limit": "5000",
            })
            payload = build_report_payload(event_id=report["event_id"], ticker=ticker,
                                           company_name=report["company_name"], listing_date=date,
                                           manifests=manifests, offering=offering,
                                           financial_rows=financial_rows, source_text=source_text)
            payload["previous_content_sha256"] = report.get("content_sha256")
            created = not bool(report.get("content_sha256"))
            if apply:
                write = supabase_upsert("ipo_analysis_reports", payload, on_conflict="stable_key")
                if not write.get("ok"):
                    raise RuntimeError(write.get("error") or "report upsert failed")
            results.append({"ticker": ticker, "id": payload["id"], "status": payload["status"],
                            "created": created, "storage_bytes": payload_storage_bytes(payload),
                            "source_count": payload["facts"]["source_count"],
                            "image_count": 0, "base64_count": 0,
                            "content_sha256": payload["content_sha256"]})
        except Exception as exc:
            logger.warning("[IPO_ANALYSIS] ticker=%s failed independently: %s", ticker, exc, exc_info=True)
            if apply:
                _rest_patch("ipo_analysis_reports", {"stable_key": f"eq.{report['stable_key']}"},
                            {"status": "failed", "last_error": str(exc)[:500],
                             "updated_at": datetime.now(JST).isoformat(timespec="seconds")})
            results.append({"ticker": ticker, "id": report["id"], "status": "failed", "created": False,
                            "error": str(exc)})
    return {"processed": len(results), "created": sum(bool(row.get("created")) for row in results),
            "failed": sum(row["status"] == "failed" for row in results), "reports": results}


def seed_existing_ipo_cards(listing_date: str, tickers: Iterable[str] | None = None) -> dict[str, Any]:
    """Idempotently seed pending reports from existing listing cards for a single JST date."""
    normalized_tickers = {normalize_ticker(value) for value in (tickers or [])}
    day = datetime.fromisoformat(listing_date).replace(tzinfo=JST)
    start = day.astimezone(timezone.utc).isoformat()
    end = (day + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    events = _rest_get("tdnet_events", {
        "select": "id,ticker,company_name,source_url,disclosed_at,display_title,raw_payload",
        "disclosed_at": f"gte.{start}", "and": f"(disclosed_at.lt.{end},display_title.like.新規上場*)", "limit": "100",
    })
    if normalized_tickers:
        events = [row for row in events if normalize_ticker(row["ticker"]) in normalized_tickers]
    results = []
    for event in events:
        raw = event.get("raw_payload") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        source_title = ((raw.get("raw") or {}).get("source_title") if isinstance(raw, dict) else None) or event["display_title"]
        item = SimpleNamespace(ticker=event["ticker"], company_name=event["company_name"],
                               title=source_title, doc_url=event["source_url"],
                               published_at=listing_date + " 08:00")
        results.append(ensure_pending_report(item, event["id"]))
    return {"matched_cards": len(events), "seeded": sum(row.get("action") == "inserted" for row in results),
            "existing": sum(row.get("action") == "existing" for row in results), "results": results}
