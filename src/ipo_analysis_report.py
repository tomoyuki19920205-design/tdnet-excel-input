"""Deterministic, source-linked IPO analysis reports for Company Viewer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
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
JPX_TPM_LISTING_URL = "https://www.jpx.co.jp/equities/products/tpm/issues/"
NSE_LISTING_URL = "https://www.nse.or.jp/listing/new/"
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
        native = [page.extract_text() or "" for page in pdf.pages]
        if sum(len(value.strip()) for value in native) >= max(100, len(pdf.pages) * 20):
            return len(pdf.pages), "\n".join(
                f"[[PDF_PAGE:{index}]]\n{text}" for index, text in enumerate(native, 1)
            )
        if os.name != "nt":
            return len(pdf.pages), "\n".join(native)
        script = Path(__file__).resolve().parents[1] / "tools" / "windows_ocr_pdf_pages.ps1"
        if not script.exists():
            return len(pdf.pages), "\n".join(native)
        with tempfile.TemporaryDirectory(prefix="ipo-pdf-ocr-") as directory:
            for index, page in enumerate(pdf.pages, 1):
                # 300 dpi is required for the small Japanese table glyphs used
                # by scanned TOKYO PRO Market primary documents.
                page.to_image(resolution=300).save(Path(directory) / f"page-{index:04d}.png", format="PNG")
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Directory", directory], capture_output=True, check=True, timeout=300,
            )
            pages = json.loads(completed.stdout.decode("utf-8-sig"))
        return len(pdf.pages), "\n".join(
            f"[[PDF_PAGE:{index}]]\n{row.get('text', '')}" for index, row in enumerate(pages, 1)
        )


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
                         "exchange_company_name": anchor_cells[1],
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


def collect_exchange_listing_sources(ticker: str, session: requests.Session) -> list[dict[str, Any]]:
    """Discover market-specific primary documents from TPM and Nagoya listings."""
    normalized = normalize_ticker(ticker)
    results: list[dict[str, Any]] = []
    for exchange, listing_url in (("JPX TOKYO PRO Market", JPX_TPM_LISTING_URL),
                                  ("名古屋証券取引所", NSE_LISTING_URL)):
        response = session.get(listing_url, timeout=60)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
        row = next((tr for tr in soup.select("tr")
                    if re.search(rf"(?<![0-9A-Z]){re.escape(normalized)}(?![0-9A-Z])",
                                 unicodedata.normalize("NFKC", tr.get_text(" ", strip=True)).upper())), None)
        if row is None:
            continue
        candidate_rows = [row]
        sibling = row.find_next_sibling("tr")
        if exchange == "JPX TOKYO PRO Market" and sibling is not None and normalized not in unicodedata.normalize("NFKC", sibling.get_text(" ", strip=True)).upper():
            candidate_rows.append(sibling)
        results.append({"title": f"{exchange} 新規上場会社情報", "url": listing_url,
                        "issuer": exchange, "document_id": f"{exchange}-{normalized}",
                        "published_at": None, "version_relation": "current", "kind": "html",
                        "raw_bytes": response.content,
                        "text": " ".join(item.get_text(" ", strip=True) for item in candidate_rows),
                        "page_count": None})
        seen: set[str] = set()
        for row_index, candidate in enumerate(candidate_rows):
            for anchor in candidate.select("a[href]"):
                href = anchor.get("href", "")
                url = urljoin(listing_url, href)
                if not url or url in seen or not ("pdf" in href.lower() or href.lower().endswith(".pdf")):
                    continue
                seen.add(url)
                anchor_title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
                if exchange == "JPX TOKYO PRO Market":
                    cell = anchor.find_parent("td")
                    cells = candidate.find_all("td", recursive=False)
                    cell_index = cells.index(cell) if cell in cells else -1
                    title_by_cell = ({4: "特定証券情報（発行者情報）", 5: "新規上場会社概要",
                                      6: "コーポレート・ガバナンス報告書", 7: "代表インタビュー"}
                                     if row_index == 0 else
                                     {2: "J-Adviser宣誓書", 3: "定款"})
                    anchor_title = title_by_cell.get(cell_index, anchor_title)
                results.append({"title": f"{exchange} {anchor_title or Path(url).stem}", "url": url,
                                "issuer": exchange, "document_id": Path(url).stem,
                                "published_at": None, "version_relation": "current", "kind": "pdf"})
    return results


def collect_sources(ticker: str, listing_date: str, tdnet_documents: Iterable[Any], *,
                    company_name: str = "", session: requests.Session | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
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
    documents.extend(collect_exchange_listing_sources(normalized, client))
    manifests: list[dict[str, Any]] = []
    source_text: dict[str, Any] = {}
    source_binary: dict[str, dict[str, bytes]] = {}
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
            source_binary[source_id] = {"pdf": raw}
        except Exception as exc:
            manifests.append({**document, "source_id": source_id, "fetch_status": "failed",
                              "sha256": None, "page_count": None, "used_pages": [],
                              "used_pdf_pages": [], "used_printed_pages": [],
                              "error": str(exc)[:300]})
    # Resolve EDINET identity from already-fetched official application documents before
    # falling back to normalized issuer-name matching. This also handles romanized names.
    from src.ipo_due_diligence import (discover_edinet_documents, download_edinet_document,
                                       extract_due_diligence, extract_exchange_text_due_diligence)
    edinet_codes = sorted({match for text in source_text.values() if isinstance(text, str)
                           for match in re.findall(r"E\d{5}", text)})
    try:
        edinet_documents, discovery = discover_edinet_documents(
            company_name=str(offering.get("exchange_company_name") or company_name),
            listing_date=listing_date, edinet_codes=edinet_codes,
            session=client, api_key=os.environ.get("EDINET_API_KEY", ""),
        )
    except Exception as exc:
        edinet_documents, discovery = [], {"status": "failed", "reason_code": "SOURCE_FETCH_FAILED",
                                            "detail": str(exc)[:300]}
    for document in edinet_documents:
        source_id = f"P{len(manifests) + 1:02d}"
        try:
            pdf_bytes, xbrl_bytes = download_edinet_document(
                document, session=client, api_key=os.environ.get("EDINET_API_KEY", ""))
            pages, text = _pdf_page_count_and_text(pdf_bytes)
            stored = {key: value for key, value in document.items() if key not in {"api_pdf_url", "is_correction"}}
            manifests.append({**stored, "source_id": source_id, "fetch_status": "success",
                              "sha256": _sha256(pdf_bytes), "page_count": pages,
                              "used_pages": [], "used_pdf_pages": [], "used_printed_pages": [],
                              "content_type": "application/pdf"})
            source_text[source_id] = text
            source_binary[source_id] = {"pdf": pdf_bytes, "xbrl": xbrl_bytes}
        except Exception as exc:
            stored = {key: value for key, value in document.items() if key not in {"api_pdf_url", "is_correction"}}
            manifests.append({**stored, "source_id": source_id, "fetch_status": "failed",
                              "sha256": None, "page_count": None, "used_pages": [],
                              "used_pdf_pages": [], "used_printed_pages": [], "error": str(exc)[:300]})
    offering["source_discovery"] = discovery
    for source in manifests:
        if "新規上場会社概要" in source.get("title", ""):
            match = re.search(r"上場時発行済株式総数\s*([0-9,]+)株", str(source_text.get(source["source_id"], "")))
            if match:
                offering["post_listing_shares"] = float(match.group(1).replace(",", ""))
    all_source_text = "\n".join(str(value) for value in source_text.values())
    compact_source_text = re.sub(r"\s+", "", all_source_text)
    if ("特定投資家向け取得勧誘及び特定投資家向け売付け勧誘の予定" in compact_source_text
            and re.search(r"特定投資家向け取得勧誘.{0,120}?なし", compact_source_text)):
        evidence_source = next((source for source in manifests
                                if "新規上場会社概要" in source.get("title", "")
                                and source.get("fetch_status") == "success"), None)
        offering["not_applicable_evidence"] = {
            "reason_code": "NOT_APPLICABLE", "detail": "特定投資家向け取得・売付け勧誘の予定なし",
            "source_id": evidence_source.get("source_id") if evidence_source else None,
            "pdf_page": 1 if evidence_source else None,
        }
    successful_edinet = [source for source in manifests
                         if source.get("fetch_status") == "success" and source.get("document_id", "").startswith("S100")]
    if successful_edinet:
        base = next((source for source in successful_edinet if source.get("version_relation") == "initial"), successful_edinet[0])
        latest = successful_edinet[-1]
        try:
            offering["due_diligence"] = extract_due_diligence(
                base_xbrl=source_binary[base["source_id"]]["xbrl"],
                base_pdf=source_binary[base["source_id"]]["pdf"], base_source_id=base["source_id"],
                latest_xbrl=source_binary[latest["source_id"]]["xbrl"],
                latest_pdf=source_binary[latest["source_id"]]["pdf"], latest_source_id=latest["source_id"],
                post_listing_shares=offering.get("post_listing_shares"),
            )
            used_by_source: dict[str, set[int]] = defaultdict(set)
            def record_pages(value: Any) -> None:
                if isinstance(value, dict):
                    if value.get("source_id") and value.get("pdf_page"):
                        used_by_source[value["source_id"]].add(int(value["pdf_page"]))
                    for nested in value.values(): record_pages(nested)
                elif isinstance(value, list):
                    for nested in value: record_pages(nested)
            record_pages(offering["due_diligence"])
            for source in manifests:
                if source["source_id"] in used_by_source:
                    pages = sorted(used_by_source[source["source_id"]])
                    source["used_pages"] = pages
                    source["used_pdf_pages"] = pages
        except Exception as exc:
            offering["due_diligence_error"] = {"reason_code": "TABLE_EXTRACTION_FAILED", "detail": str(exc)[:500]}
    else:
        primary = next((source for source in manifests if "特定証券情報" in source.get("title", "")
                        and source.get("fetch_status") == "success"), None)
        overview = next((source for source in manifests if "新規上場会社概要" in source.get("title", "")
                         and source.get("fetch_status") == "success"), None)
        overview_text = str(source_text.get(overview["source_id"], "")) if overview else ""
        business_match = re.search(r"事業の内容\s*(.{20,240}?)(?:業種別分類|銘柄略称|発行可能株式)",
                                   re.sub(r"\s+", " ", overview_text), re.S)
        expected = {group: {"source_id": primary.get("source_id") if primary else None,
                            "section": section, "pdf_page": None}
                    for group, section in {
                        "balance_sheet_cash_flow": "財務情報／キャッシュ・フロー",
                        "shareholders_sellers": "株主の状況",
                        "lockup": "ロックアップ",
                        "stock_options": "新株予約権等の状況",
                        "kpi_growth": "事業の状況／重要KPI",
                        "risks": "事業等のリスク",
                    }.items()}
        primary_text = str(source_text.get(primary["source_id"], "")) if primary else ""
        if primary and primary_text:
            diligence = extract_exchange_text_due_diligence(primary_text, primary["source_id"])
            if not diligence.get("business_model") and business_match:
                diligence["business_model"] = business_match.group(1).strip()
            if offering.get("not_applicable_evidence"):
                evidence = dict(offering["not_applicable_evidence"])
                evidence["source_page"] = evidence.pop("pdf_page", None)
                diligence.setdefault("absence_evidence", {})["lockup"] = evidence
            diligence["expected_locations"] = expected
            offering["due_diligence"] = diligence
        else:
            offering["due_diligence"] = {
                "business_model": business_match.group(1).strip() if business_match else None,
                "shareholders": [], "lockups": [], "stock_options": [], "kpis": [],
                "financial_position": {}, "financial_facts": [], "offering_terms": {},
                "expected_locations": expected,
            }
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


def _fact_citation(fact: dict[str, Any], manifests: list[dict[str, Any]]) -> str:
    source = next((item for item in manifests if item.get("source_id") == fact.get("source_id")), None)
    if not source:
        return ""
    page = str(fact.get("source_page") or fact.get("pdf_page") or "")
    return _source_link(source, page)


def _table_fmt(value: Any, digits: int = 3) -> str:
    return "―" if value is None else _fmt(value, digits)


def _period_label(period: Any, quarter: str | None = None) -> str:
    value = str(period or "")
    match = re.match(r"(20\d{2})-(\d{2})-(\d{2})", value)
    label = f"{match.group(1)}/{match.group(2)}期" if match else value
    if quarter and quarter != "FY":
        label += f" {quarter}累計"
    return label


def _date_label(value: Any) -> str:
    match = re.match(r"(20\d{2})-(\d{2})-(\d{2})", str(value or ""))
    return f"{match.group(1)}年{int(match.group(2))}月{int(match.group(3))}日" if match else str(value or "")


def _compact_sentence(value: Any, limit: int = 180) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    sentences = [part.strip() for part in re.split(r"(?<=[。！？])", text) if part.strip()]
    result = "".join(sentences[:2]) if sentences else text
    return result if len(result) <= limit else result[:limit].rstrip("、 ,") + "…"


def _append_section(lines: list[str], title: str, body: list[str]) -> None:
    content = [line for line in body if line is not None]
    if not any(line.strip() for line in content):
        return
    if lines and lines[-1] != "":
        lines.append("")
    lines.extend([f"## {title}", *content])


def _source_note(source: dict[str, Any] | None) -> str:
    return f"出典: {_source_link(source)}" if source else ""


def _display_kpis(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suppress rounded duplicates while retaining genuinely different KPI definitions."""
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        name = re.sub(r"（表(?:・[^）]*)?）", "", str(row.get("metric_name_normalized") or row.get("metric_name_original") or ""))
        key = (name, row.get("as_of_date"), row.get("period_start"), row.get("period_end"),
               row.get("cumulative_or_period"))
        current = selected.get(key)
        exact = not str(row.get("unit_original") or "").startswith("万")
        current_exact = current is not None and not str(current.get("unit_original") or "").startswith("万")
        if current is None or (exact and not current_exact):
            selected[key] = row
    return list(selected.values())


def render_report(*, ticker: str, company_name: str, listing_date: str, market: str | None,
                  status: str, manifests: list[dict[str, Any]], financials: list[dict[str, Any]],
                  offering: dict[str, Any], calculations: list[dict[str, Any]], ai_text: str | None,
                  missing: list[str], generated_at: str, diligence: dict[str, Any] | None = None,
                  completeness_gate: dict[str, Any] | None = None) -> str:
    diligence = diligence or {}
    absence_evidence = diligence.get("absence_evidence", {})
    sources_ok = [source for source in manifests if source.get("fetch_status") == "success"]
    financial_source = next((source for source in sources_ok if "決算情報" in source.get("title", "")),
                            sources_ok[0] if sources_ok else None)
    listing_source = next((source for source in sources_ok if "新規上場会社情報" in source.get("title", "")), None)
    calc_by_name = {row["name"]: row for row in calculations}
    terms = diligence.get("offering_terms", {})
    actuals = [row for row in financials if row["kind"] == "actual"]
    forecasts = [row for row in financials if row["kind"] == "forecast"]
    holders = diligence.get("shareholders", [])
    lockups = diligence.get("lockups", [])
    options = diligence.get("stock_options", [])
    lines: list[str] = []

    basic = [f"- 証券コード: {ticker}", f"- 上場日: {_date_label(listing_date)}"]
    if market:
        basic.append(f"- 上場市場: {market}")
    if listing_source:
        basic.append(_source_note(listing_source))
    _append_section(lines, "01 基本情報", basic)

    business_body: list[str] = []
    business = diligence.get("business_model")
    business_fact = {"source_id": diligence.get("source_id"), "pdf_page": diligence.get("business_model_page")}
    if business:
        business_body.append(f"- {_compact_sentence(business, 280)} {_fact_citation(business_fact, manifests)}".rstrip())
    if diligence.get("single_segment") and diligence["single_segment"] not in str(business):
        business_body.append(f"- {_compact_sentence(diligence['single_segment'])} {_fact_citation(business_fact, manifests)}".rstrip())
    _append_section(lines, "02 事業内容", business_body)

    if actuals:
        body = ["金額：百万円、EPS：円", "", "|期間|区分|売上高|営業利益|純利益|EPS|",
                "|---|---|---:|---:|---:|---:|"]
        fy_actuals = [row for row in actuals if row["quarter"] == "FY"]
        latest_actual = max((row["period"] for row in fy_actuals), default=None)
        for row in actuals:
            metrics = row["metrics"]
            category = ("直近通期実績" if row["quarter"] == "FY" and row["period"] == latest_actual
                        else "前期実績" if row["quarter"] == "FY" else "累計実績")
            body.append(f"|{_period_label(row['period'], row['quarter'])}|{category}|{_table_fmt(metrics.get('sales'))}|"
                        f"{_table_fmt(metrics.get('operating_profit'))}|{_table_fmt(metrics.get('net_income'))}|"
                        f"{_table_fmt(metrics.get('eps'), 2)}|")
        if financial_source:
            body += ["", _source_note(financial_source)]
        _append_section(lines, "03 業績実績", body)

    if forecasts:
        body = ["金額：百万円、EPS：円", "", "|期間|区分|売上高|営業利益|純利益|EPS|",
                "|---|---|---:|---:|---:|---:|"]
        for row in forecasts:
            metrics = row["metrics"]
            category = "通期予想" if row["quarter"] == "FY" else "累計予想"
            body.append(f"|{_period_label(row['period'], row['quarter'])}|{category}|{_table_fmt(metrics.get('sales'))}|"
                        f"{_table_fmt(metrics.get('operating_profit'))}|{_table_fmt(metrics.get('net_income'))}|"
                        f"{_table_fmt(metrics.get('eps'), 2)}|")
        if financial_source:
            body += ["", _source_note(financial_source)]
        _append_section(lines, "04 業績予想", body)

    offering_body: list[str] = []
    if offering.get("offering_price") is not None:
        for key, label, unit in (("offering_price", "公開価格", "円"),
                                 ("public_offering_shares", "公募株数", "株"),
                                 ("secondary_shares", "売出株数", "株"), ("oa_shares", "OA株数", "株")):
            if offering.get(key) is not None:
                offering_body.append(f"- {label}: {_fmt(offering[key], 0)}{unit}")
        for key, label, unit in (("underwriting_price", "引受価額", "円"),
                                 ("company_law_payment_price", "会社法上の払込金額", "円"),
                                 ("capital_per_share", "資本組入額（1株当たり）", "円"),
                                 ("net_proceeds_thousand_yen", "差引手取概算額", "千円")):
            if terms.get(key) is not None:
                offering_body.append(f"- {label}: {_fmt(terms[key], 2 if unit == '円' else 0)}{unit} {_fact_citation(terms, manifests)}".rstrip())
        for key, label, divisor, unit in (("public_shares_including_oa", "公開株数（OA含む）", 1, "株"),
                                          ("absorption_amount_jpy", "吸収金額", 1_000_000, "百万円"),
                                          ("market_cap_jpy", "公開時時価総額", 1_000_000, "百万円"),
                                          ("public_float_ratio_pct", "公開株比率", 1, "%")):
            if key in calc_by_name:
                offering_body.append(f"- {label}: {_fmt(calc_by_name[key]['value'] / divisor, 2)}{unit}")
        if listing_source:
            offering_body.append(_source_note(listing_source))
    elif offering.get("not_applicable_evidence"):
        evidence = offering["not_applicable_evidence"]
        offering_body.append(f"- 公募・売出しなし {_fact_citation(evidence, manifests)}".rstrip())
    _append_section(lines, "06 公募・売出し・公開規模", offering_body)

    supply_body: list[str] = []
    if diligence.get("effective_potential_shares") is not None:
        supply_body.append(f"- 潜在株式は{_fmt(diligence['effective_potential_shares'], 0)}株、上場時株式数比は"
                           f"{_table_fmt(diligence.get('potential_dilution_pct_of_listing_shares'), 2)}%。")
    if "public_float_ratio_pct" in calc_by_name:
        supply_body.append(f"- OAを含む公開株比率は{_fmt(calc_by_name['public_float_ratio_pct']['value'], 2)}%。")
    if lockups:
        periods = "・".join(sorted({f"{row['days']}日" for row in lockups}, reverse=True))
        supply_body.append(f"- ロックアップは{periods}。価格解除条件は株主ごとに異なります。 {_fact_citation(lockups[0], manifests)}".rstrip())
    if not supply_body and offering.get("not_applicable_evidence"):
        supply_body.append("- 公募・売出しなし。")
    _append_section(lines, "07 需給・重要注意点", supply_body)

    if holders:
        body = ["|株主|上場前保有株|売出・親引け増減|売出後残高|", "|---|---:|---:|---:|"]
        for row in holders:
            change = -row["sold_or_allotted_shares"]
            body.append(f"|{row['name']}|{_fmt(row['before_shares'], 0)}|{change:+,.0f}|{_fmt(row['after_shares'], 0)}|")
        refs = list(dict.fromkeys(_fact_citation(row, manifests) for row in holders if _fact_citation(row, manifests)))
        if refs:
            body += ["", "出典: " + "、".join(refs)]
        _append_section(lines, "08 主要株主と売出後残高", body)

    lockup_body: list[str] = []
    if lockups:
        lockup_body = ["|期間|期限|価格解除|対象|", "|---:|---|---|---|"]
        for row in lockups:
            release = f"公開価格の{row['price_release_multiple']}倍" if row.get("price_release_multiple") else "なし"
            lockup_body.append(f"|{row['days']}日|{row['until']}|{release}|{_compact_sentence(row.get('holders_text'), 100)}|")
        lockup_body += ["", "出典: " + "、".join(dict.fromkeys(_fact_citation(row, manifests) for row in lockups if _fact_citation(row, manifests)))]
    elif absence_evidence.get("lockup"):
        lockup_body = [f"- ロックアップ対象なし {_fact_citation(absence_evidence['lockup'], manifests)}".rstrip()]
    _append_section(lines, "09 期間別ロックアップ", lockup_body)

    terms_body: list[str] = []
    term_values = (("greenshoe_shares", "OA／グリーンシュー対象株数", "株"),
                   ("parent_allotment_shares", "親引け株数", "株"),
                   ("oa_lenders", "OA貸株元", ""),
                   ("greenshoe_exercise_deadline", "グリーンシュー行使期限", ""),
                   ("syndicate_cover_period", "シンジケートカバー取引期間", ""))
    for key, label, unit in term_values:
        value = terms.get(key)
        if value is not None:
            shown = _fmt(value, 0) if isinstance(value, (int, float)) else value
            terms_body.append(f"- {label}: {shown}{unit} {_fact_citation(terms, manifests)}".rstrip())
    if terms.get("parent_holding_condition"):
        terms_body.append(f"- 親引け継続保有条件: {_compact_sentence(terms['parent_holding_condition'])} {_fact_citation(terms, manifests)}".rstrip())
    _append_section(lines, "10 OA・親引け", terms_body)

    vc_rows = [row for row in holders if re.search(r"ファンド|Fund|Capital|キャピタル|ベンチャー", row["name"], re.I)]
    _append_section(lines, "11 売却可能株・VC", [
        f"- {row['name']}: 売出後 {_fmt(row['after_shares'], 0)}株（上場前 {_fmt(row['before_shares'], 0)}株） {_fact_citation(row, manifests)}".rstrip()
        for row in vc_rows])

    option_body: list[str] = []
    if options:
        option_body = ["|回号|発行潜在株|失効|有効潜在株|行使価格|", "|---|---:|---:|---:|---:|"]
        for row in options:
            option_body.append(f"|{row['series']}|{_table_fmt(row.get('issued_potential_shares'), 0)}|"
                               f"{_table_fmt(row.get('forfeited_shares'), 0)}|{_table_fmt(row.get('effective_potential_shares'), 0)}|"
                               f"{_table_fmt(row.get('exercise_price_yen'), 0)}|")
        if diligence.get("effective_potential_shares") is not None:
            option_body += ["", f"- 合計有効潜在株式: {_fmt(diligence['effective_potential_shares'], 0)}株"]
        refs = list(dict.fromkeys(_fact_citation(row, manifests) for row in options if _fact_citation(row, manifests)))
        if refs:
            option_body.append("出典: " + "、".join(refs))
    elif absence_evidence.get("stock_options"):
        option_body = [f"- SOなし {_fact_citation(absence_evidence['stock_options'], manifests)}".rstrip()]
    _append_section(lines, "12 ストックオプション", option_body)

    _append_section(lines, "13 SOの行使・売却条件", [
        f"- {row['series']}: {_compact_sentence(row['exercise_period'])} {_fact_citation(row, manifests)}".rstrip()
        for row in options if row.get("exercise_period")])

    kpi_body: list[str] = []
    for row in _display_kpis(diligence.get("kpis", [])):
        period = _date_label(row.get("as_of_date")) if row.get("as_of_date") else (
            f"{_date_label(row.get('period_start'))}〜{_date_label(row.get('period_end'))}")
        value = f"{row.get('value_original')}{row.get('unit_original')}"
        yoy = f"、前年比{row['yoy_pct']:+.1f}%" if row.get("yoy_pct") is not None else ""
        definition = _compact_sentence(row.get("definition_note"), 110)
        note = f"。{definition}" if definition else ""
        kpi_body.append(f"- {row.get('metric_name_original')}: {value}（{period}{yoy}）{note} {_fact_citation(row, manifests)}".rstrip())
    for row in diligence.get("kpi_definitions", []):
        kpi_body.append(f"- {row['metric_name_original']}: {_compact_sentence(row['definition_note'], 120)} {_fact_citation(row, manifests)}".rstrip())
    _append_section(lines, "14 成長性・重要KPI", kpi_body)

    concentration_body: list[str] = []
    if diligence.get("customer_concentration"):
        fact = {"source_id": diligence.get("source_id"), "pdf_page": diligence.get("customer_concentration_page")}
        concentration_body.append(f"- {_compact_sentence(diligence['customer_concentration'])} {_fact_citation(fact, manifests)}".rstrip())
    _append_section(lines, "16 競争・主要顧客・海外", concentration_body)

    growth_body: list[str] = []
    if terms.get("net_proceeds_thousand_yen") is not None:
        growth_body.append(f"- 差引手取概算額: {_fmt(terms['net_proceeds_thousand_yen'], 0)}千円 {_fact_citation(terms, manifests)}".rstrip())
    for key in ("proceeds_use", "growth_investment_allocation"):
        if terms.get(key):
            growth_body.append(f"- {_compact_sentence(terms[key])} {_fact_citation(terms, manifests)}".rstrip())
    _append_section(lines, "17 強み・成長投資", growth_body)

    risk_body: list[str] = []
    if diligence.get("risk_excerpt"):
        fact = {"source_id": diligence.get("source_id"), "pdf_page": diligence.get("risk_page")}
        risk_body.append(f"- {_compact_sentence(diligence['risk_excerpt'])} {_fact_citation(fact, manifests)}".rstrip())
    if diligence.get("technology_excerpt"):
        fact = {"source_id": diligence.get("source_id"), "pdf_page": diligence.get("technology_page")}
        risk_body.append(f"- {_compact_sentence(diligence['technology_excerpt'])} {_fact_citation(fact, manifests)}".rstrip())
    _append_section(lines, "18 懸念・技術／AIの影響", risk_body)

    rendered = "\n".join(lines)
    used_citations = re.findall(r"\[(P\d{2})(?: PDF p\.([^\]]+))?\]", rendered)
    used_ids = {source_id for source_id, _ in used_citations}
    source_body: list[str] = []
    for source in sources_ok:
        if source.get("source_id") not in used_ids:
            continue
        details = []
        if source.get("published_at"):
            details.append(f"公開日: {_date_label(str(source['published_at'])[:10])}")
        pages = sorted({page.strip() for source_id, value in used_citations if source_id == source.get("source_id")
                        for page in value.split(",") if page.strip()},
                       key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        if not pages:
            pages = source.get("used_pdf_pages") or source.get("used_pages") or []
        if pages:
            details.append("使用ページ: " + ", ".join(f"p.{page}" for page in pages))
        suffix = f"（{'、'.join(details)}）" if details else ""
        source_body.append(f"- [{source['title']}]({source['url']}){suffix}")
    _append_section(lines, "20 出典", source_body)
    return "\n".join(lines).strip() + "\n"


def generate_ai_explanation(facts: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """AI may phrase verified facts, but may not introduce any number or citation."""
    model = os.environ.get("IPO_ANALYSIS_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY is not configured", model
    prompt = (
        "次の検証済みfact JSONだけを根拠に、IPO時点の投資家向け分析を日本語で作成してください。"
        "事業モデル、需給、利益の質、競争・顧客依存、強み・成長投資、リスク、AI・技術変化の七分類について、"
        "根拠がある分類だけ各二～五個の短い箇条書きにしてください。数字、会社名、引用記号、出典番号、推測、"
        "一般論、根拠のない評価語を一切書かず、各行を[分類名]で始めてください。\n"
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


def validate_report(markdown: str, manifests: list[dict[str, Any]], calculations: list[dict[str, Any]],
                    financials: list[dict[str, Any]], diligence: dict[str, Any] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    diligence = diligence or {}
    if not markdown.startswith("## 01 基本情報"):
        errors.append("report_does_not_start_with_basic_information")
    for number in ("05", "15", "19"):
        if re.search(rf"^## {number}\b", markdown, re.M):
            errors.append(f"hidden_section_rendered:{number}")
    forbidden_labels = ("[確認済み]", "[未確認]", "[計算値]", "[分析]", "[解析未完了]",
                        "[NOT_APPLICABLE]", "作成日時:", "調査状態:", "使用資料数:", "## 目次")
    for label in forbidden_labels:
        if label in markdown:
            errors.append(f"internal_label_rendered:{label}")
    if "## 20 出典・" in markdown or "SHA-256" in markdown or "quality_gate" in markdown:
        errors.append("technical_source_metadata_rendered")
    source_ids = {source["source_id"] for source in manifests}
    for reference in re.findall(r"\[(P\d{2})(?:(?: PDF)? p\.[^\]]+)?\]", markdown):
        if reference not in source_ids:
            errors.append(f"unresolved_source:{reference}")
    if "base64" in markdown.lower() or re.search(r"data:image/", markdown, re.I):
        errors.append("embedded_image_forbidden")
    if any(period.get("quarter") not in {"FY", "1Q", "2Q", "3Q"} for period in financials):
        errors.append("invalid_period_kind")
    if any(calc.get("unrounded_value") is None or not calc.get("formula") for calc in calculations):
        errors.append("invalid_calculation")
    for section_number in ("03", "04"):
        match = re.search(rf"^## {section_number}[^\n]*\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
        if match:
            body = match.group(1)
            expected = "|期間|区分|売上高|営業利益|純利益|EPS|"
            if expected not in body:
                errors.append(f"financial_table_columns_invalid:{section_number}")
            if any(label in body for label in ("売上総利益", "経常利益", "百万円|", "円|")):
                errors.append(f"financial_table_is_verbose:{section_number}")
    from src.ipo_due_diligence import ocr_readability_issues
    for issue in ocr_readability_issues(str(diligence.get("business_model") or "")):
        errors.append(f"ocr_readability:{issue}")
    required_financial = {
        "period_start", "period_end", "as_of_date", "period_type", "fiscal_year", "quarter",
        "consolidation_scope", "accounting_standard", "source_id", "source_page", "statement_type",
    }
    for index, fact in enumerate(diligence.get("financial_facts", [])):
        missing_fields = [field for field in required_financial if fact.get(field) in (None, "")]
        if missing_fields:
            errors.append(f"financial_fact_missing_metadata:{index}:{','.join(sorted(missing_fields))}")
        if fact.get("statement_type") == "BS" and fact.get("as_of_date") != fact.get("period_end"):
            errors.append(f"balance_sheet_period_mismatch:{index}")
        if fact.get("statement_type") == "CF" and fact.get("period_end") != fact.get("as_of_date"):
            errors.append(f"cash_flow_period_mismatch:{index}")
    required_kpi = {
        "metric_name_original", "metric_name_normalized", "value_original", "unit_original",
        "value_normalized", "unit_normalized", "as_of_date", "period_start", "period_end",
        "cumulative_or_period", "definition_note", "source_id", "source_page",
    }
    unit_multipliers = {"万人": ("人", 10_000), "万台": ("台", 10_000), "千円": ("円", 1_000),
                        "百万円": ("円", 1_000_000), "億円": ("円", 100_000_000)}
    kpi_identity: dict[tuple[Any, ...], tuple[str, str]] = {}
    for index, fact in enumerate(diligence.get("kpis", [])):
        absent_keys = [field for field in required_kpi if field not in fact]
        if absent_keys:
            errors.append(f"kpi_missing_metadata:{index}:{','.join(sorted(absent_keys))}")
        if not fact.get("as_of_date") and not (fact.get("period_start") and fact.get("period_end")):
            errors.append(f"kpi_period_missing:{index}")
        multiplier = unit_multipliers.get(fact.get("unit_original"))
        original_number = parse_number(fact.get("value_original"))
        if multiplier and original_number is not None:
            expected_unit, factor = multiplier
            if fact.get("unit_normalized") != expected_unit or abs(float(fact.get("value_normalized")) - original_number * factor) > 1e-6:
                errors.append(f"kpi_unit_multiplier_mismatch:{index}")
        key = (fact.get("metric_name_normalized"), fact.get("as_of_date"), fact.get("period_start"), fact.get("period_end"))
        identity = (str(fact.get("metric_name_original")), str(fact.get("unit_original")))
        if key in kpi_identity and kpi_identity[key] != identity:
            errors.append(f"kpi_definition_merged:{index}")
        kpi_identity[key] = identity
    for index, fact in enumerate(diligence.get("kpi_definitions", [])):
        if not all(fact.get(field) not in (None, "") for field in (
                "metric_name_original", "metric_name_normalized", "category",
                "definition_note", "source_id", "source_page")):
            errors.append(f"kpi_definition_missing_metadata:{index}")
    return {"passed": not errors, "errors": errors, "checks": {
        "visible_section_count": len(re.findall(r"^## \d{2} ", markdown, re.M)),
        "compact_display_policy": not any(error.startswith(("hidden_section_", "internal_label_", "technical_source_", "financial_table_")) for error in errors),
        "ocr_readability": not any(error.startswith("ocr_readability:") for error in errors),
        "all_citations_resolve": not any(error.startswith("unresolved_source") for error in errors),
        "no_embedded_images": "embedded_image_forbidden" not in errors,
        "period_kinds_valid": "invalid_period_kind" not in errors,
        "calculation_formulas_present": "invalid_calculation" not in errors,
        "financial_period_metadata_valid": not any(error.startswith(("financial_fact_", "balance_sheet_", "cash_flow_")) for error in errors),
        "kpi_definition_and_units_valid": not any(error.startswith("kpi_") for error in errors),
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
        if not isinstance(text, str):
            continue
        match = re.search(r"上場時発行済株式総数\s*([0-9,]+)株", text)
        if match:
            offering["post_listing_shares"] = float(match.group(1).replace(",", ""))
    diligence = offering.get("due_diligence", {})
    terms = diligence.get("offering_terms", {})
    for key in ("offering_price", "underwriting_price", "company_law_payment_price", "capital_per_share"):
        if offering.get(key) is None and terms.get(key) is not None:
            offering[key] = terms[key]
    effective_options = diligence.get("effective_potential_shares")
    if effective_options and offering.get("post_listing_shares"):
        diligence["potential_dilution_pct_of_listing_shares"] = (
            float(effective_options) / float(offering["post_listing_shares"]) * 100
        )
    calculations = calculate_offering(offering)
    failed_sources = [source["source_id"] for source in manifests if source["fetch_status"] != "success"]
    from src.ipo_due_diligence import build_completeness
    completeness = build_completeness(manifests=manifests, financials=financials,
                                      offering=offering, diligence=diligence,
                                      discovery=offering.get("source_discovery"))
    missing = []
    for row in completeness["missing"]:
        location = row.get("expected_location") or {}
        suffix = ""
        if location:
            page = f" PDF {location['pdf_page']}ページ" if location.get("pdf_page") else " ページ未特定"
            suffix = f"（{location.get('source_id')} {location.get('section')}{page}）"
        missing.append(f"{row['group']}: {row['reason_code']}{suffix}")
    if offering.get("due_diligence_error"):
        missing.append("due_diligence: " + offering["due_diligence_error"]["reason_code"])
    if failed_sources:
        missing.append("取得失敗資料: " + ", ".join(failed_sources))
    facts = {"ticker": normalized, "company_name": company_name, "listing_date": listing_date,
             "market": offering.get("market"), "financial_periods": financials,
             "offering": {key: value for key, value in offering.items() if key not in {"due_diligence"}},
             "due_diligence": diligence,
             "source_count": len([s for s in manifests if s["fetch_status"] == "success"])}
    # Analytical bullets are rendered deterministically from source-linked facts.
    # This prevents fluent but unsupported generic prose from becoming report data.
    ai_text, ai_error, model = None, None, "deterministic-fact-renderer-v1"
    generated_at = datetime.now(JST).isoformat(timespec="seconds")
    status = "completed" if completeness["passed"] else "partial"
    markdown = render_report(ticker=normalized, company_name=company_name, listing_date=listing_date,
                             market=offering.get("market"), status=status, manifests=manifests,
                             financials=financials, offering=offering, calculations=calculations,
                             ai_text=ai_text, missing=missing, generated_at=generated_at,
                             diligence=diligence, completeness_gate=completeness)
    validation = validate_report(markdown, manifests, calculations, financials, diligence)
    validation["integrity_gate"] = {"passed": validation["passed"], "errors": list(validation["errors"])}
    validation["completeness_gate"] = completeness
    if not validation["passed"] or not completeness["passed"]:
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
            manifests, offering, source_text = collect_sources(
                ticker, date, docs, company_name=report["company_name"])
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
                            "source_documents": [{"source_id": source["source_id"], "document_id": source.get("document_id"),
                                                  "title": source["title"], "url": source["url"],
                                                  "fetch_status": source["fetch_status"],
                                                  "used_pages": source.get("used_pdf_pages", [])}
                                                 for source in payload["source_manifest"]],
                            "financial_periods": payload["facts"]["financial_periods"],
                            "integrity_gate": payload["validation_result"]["integrity_gate"],
                            "completeness_gate": payload["validation_result"]["completeness_gate"],
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
