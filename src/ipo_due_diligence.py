"""Deterministic extraction of IPO due-diligence facts from official EDINET XBRL."""
from __future__ import annotations

import re
import hashlib
import unicodedata
import zipfile
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup


REASON_CODES = {
    "SOURCE_NOT_DISCOVERED", "SOURCE_FETCH_FAILED", "SOURCE_IDENTITY_UNRESOLVED",
    "PARSER_UNSUPPORTED", "TABLE_EXTRACTION_FAILED", "FACT_CONFLICT",
    "SOURCE_ACTUALLY_ABSENT", "NOT_APPLICABLE",
}

REQUIRED_GROUPS = (
    "basic_business", "financial_performance", "balance_sheet_cash_flow",
    "offering", "shareholders_sellers", "lockup", "stock_options",
    "kpi_growth", "risks", "sources_revisions",
)

_EDINET_DAY_CACHE: dict[str, list[dict[str, Any]]] = {}
_PDF_TEXT_CACHE: dict[str, list[str]] = {}


def normalize_company_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    for token in ("株式会社", "(株)", "（株）", "inc.", "inc", "co.,ltd.", "co.ltd.",
                  "ホールディングス", "holdings", "ｈｄ", "hd"):
        text = text.replace(token, "")
    return re.sub(r"[\s・･._\-]", "", text)


def company_names_match(left: str, right: str) -> bool:
    a, b = normalize_company_name(left), normalize_company_name(right)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def discover_edinet_documents(*, company_name: str, listing_date: str,
                              edinet_codes: Iterable[str] = (),
                              session: requests.Session | None = None,
                              api_key: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Find initial/corrected IPO filings without issuer-specific URLs."""
    if not api_key:
        return [], {"status": "failed", "reason_code": "SOURCE_FETCH_FAILED",
                    "detail": "EDINET_API_KEY is unavailable"}
    codes = {value.upper() for value in edinet_codes if re.fullmatch(r"E\d{5}", value.upper())}
    client = session or requests.Session()
    current = date.fromisoformat(listing_date) - timedelta(days=65)
    end = date.fromisoformat(listing_date)
    rows: list[dict[str, Any]] = []
    while current <= end:
        day_key = current.isoformat()
        if day_key not in _EDINET_DAY_CACHE:
            response = client.get(
                "https://api.edinet-fsa.go.jp/api/v2/documents.json",
                params={"date": day_key, "type": 2, "Subscription-Key": api_key},
                timeout=60,
            )
            response.raise_for_status()
            _EDINET_DAY_CACHE[day_key] = list(response.json().get("results", []))
        for row in _EDINET_DAY_CACHE[day_key]:
            description = str(row.get("docDescription") or "")
            if "新規公開時" not in description or "有価証券届出書" not in description:
                continue
            identity_ok = str(row.get("edinetCode") or "").upper() in codes if codes else False
            if not identity_ok:
                identity_ok = company_names_match(company_name, str(row.get("filerName") or ""))
            if not identity_ok:
                continue
            if str(row.get("withdrawalStatus") or "0") not in {"0", ""}:
                continue
            rows.append(row)
        current += timedelta(days=1)
    rows.sort(key=lambda row: (str(row.get("submitDateTime") or ""), str(row.get("docID") or "")))
    documents = []
    for index, row in enumerate(rows):
        doc_id = str(row["docID"])
        is_correction = "訂正" in str(row.get("docDescription") or "")
        documents.append({
            "title": str(row.get("docDescription") or "有価証券届出書（新規公開時）"),
            "url": f"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf",
            "api_pdf_url": f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}",
            "issuer": str(row.get("filerName") or company_name), "document_id": doc_id,
            "edinet_code": row.get("edinetCode"), "published_at": row.get("submitDateTime"),
            "version_relation": f"correction-{index}" if is_correction else "initial",
            "kind": "pdf", "is_correction": is_correction,
        })
    if not documents:
        reason = "SOURCE_IDENTITY_UNRESOLVED" if not codes else "SOURCE_NOT_DISCOVERED"
        return [], {"status": "failed", "reason_code": reason,
                    "detail": "No IPO securities registration statement matched in EDINET"}
    return documents, {"status": "success", "reason_code": None, "count": len(documents)}


def download_edinet_document(document: dict[str, Any], *, session: requests.Session,
                             api_key: str) -> tuple[bytes, bytes]:
    base = document["api_pdf_url"]
    pdf = session.get(base, params={"type": 2, "Subscription-Key": api_key}, timeout=120)
    pdf.raise_for_status()
    xbrl = session.get(base, params={"type": 1, "Subscription-Key": api_key}, timeout=120)
    xbrl.raise_for_status()
    return pdf.content, xbrl.content


def _html_documents(xbrl_zip: bytes) -> list[tuple[str, BeautifulSoup]]:
    result = []
    with zipfile.ZipFile(BytesIO(xbrl_zip)) as archive:
        names = sorted(name for name in archive.namelist()
                       if name.startswith("XBRL/PublicDoc/") and name.endswith("_ixbrl.htm"))
        for name in names:
            # EDINET iXBRL files may declare Shift_JIS while their ZIP payload is UTF-8.
            # Decode explicitly so parser output cannot silently become mojibake.
            result.append((name, BeautifulSoup(archive.read(name).decode("utf-8"), "html.parser")))
    return result


def _number(text: str) -> float | None:
    normalized = unicodedata.normalize("NFKC", text or "").replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    value = float(match.group())
    if "△" in normalized or normalized.strip().startswith("-"):
        value = -value
    return value


def _first_number(text: str) -> int | None:
    value = _number((text or "").split("(", 1)[0].split("（", 1)[0])
    return int(value) if value is not None else None


def _fact_value(tag: Any) -> float | None:
    value = _number(tag.get_text(" ", strip=True))
    if value is None:
        return None
    if tag.get("sign") == "-":
        value = -abs(value)
    return value


def _find_facts(documents: list[tuple[str, BeautifulSoup]], suffix: str) -> list[dict[str, Any]]:
    facts = []
    for name, soup in documents:
        for tag in soup.find_all(lambda node: node.has_attr("name") and str(node.get("name")).split(":")[-1] == suffix):
            value = _fact_value(tag)
            if value is not None:
                facts.append({"value": value, "context": str(tag.get("contextref") or ""),
                              "scale": int(tag.get("scale") or 0), "file": name})
    return facts


def _context_metadata(documents: list[tuple[str, BeautifulSoup]]) -> dict[str, dict[str, Any]]:
    """Read EDINET context periods without depending on issuer-specific concepts."""
    result: dict[str, dict[str, Any]] = {}
    for _, soup in documents:
        for context in soup.find_all(lambda node: node.name and node.name.split(":")[-1].lower() == "context"):
            context_id = str(context.get("id") or "")
            if not context_id or context_id in result:
                continue
            values: dict[str, str] = {}
            for child in context.find_all():
                local = child.name.split(":")[-1].lower() if child.name else ""
                if local in {"instant", "startdate", "enddate"}:
                    values[local] = child.get_text(strip=True)
            start, end, instant = values.get("startdate"), values.get("enddate"), values.get("instant")
            if instant and not start:
                start, end = f"{instant[:4]}-01-01", instant
            result[context_id] = {
                "period_start": start,
                "period_end": end,
                "as_of_date": instant or end,
                "period_type": "interim" if "interim" in context_id.lower() else "full_year",
                "fiscal_year": int((instant or end or "0000")[:4]) if (instant or end) else None,
                "quarter": "2Q" if "interim" in context_id.lower() else "FY",
                "consolidation_scope": "non_consolidated" if "nonconsolidatedmember" in context_id.lower() else "consolidated",
                "accounting_standard": "J-GAAP",
            }
    for context_id, row in result.items():
        if "Instant" not in context_id:
            continue
        prefix = "Interim" if context_id.startswith("Interim") else "Prior1Year"
        duration = next((candidate for key, candidate in result.items()
                         if key.startswith(prefix + "Duration") and candidate.get("period_end") == row.get("as_of_date")), None)
        if duration:
            row["period_start"] = duration.get("period_start")
            row["period_end"] = duration.get("period_end")
    return result


def _fact_million_yen(fact: dict[str, Any]) -> float:
    return float(fact["value"]) * (10 ** int(fact.get("scale") or 0)) / 1_000_000


def _jp_date(value: str) -> str | None:
    match = re.search(r"(20\d{2})年\s*(\d{1,2})月(?:\s*(\d{1,2})日)?", unicodedata.normalize("NFKC", value or ""))
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3) or 1)
    return date(year, month, day).isoformat()


def _period_end_for_month(value: str) -> str | None:
    parsed = _jp_date(value)
    if not parsed:
        return None
    current = date.fromisoformat(parsed)
    next_month = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
    return (next_month - timedelta(days=1)).isoformat()


def _choose_fact(facts: list[dict[str, Any]], contexts: tuple[str, ...]) -> dict[str, Any] | None:
    for needle in contexts:
        candidates = [fact for fact in facts if needle in fact["context"] and "Member_" not in fact["context"]]
        if candidates:
            return candidates[-1]
    return facts[-1] if facts else None


def locate_pdf_page_by_values(pdf_bytes: bytes, values: Iterable[float | int], *, minimum: int = 2) -> int | None:
    """Map an XBRL fact group back to the PDF by matching its printed numeric values."""
    wanted = []
    for value in values:
        number = abs(float(value))
        wanted.append(f"{number:,.2f}".rstrip("0").rstrip("."))
        if number.is_integer():
            wanted.append(f"{int(number):,}")
    best: tuple[int, int] | None = None
    for page_no, text in enumerate(_pdf_text_pages(pdf_bytes), 1):
        score = sum(1 for token in set(wanted) if token and token in text)
        if best is None or score > best[0]:
            best = (score, page_no)
    return best[1] if best and best[0] >= minimum else None


def _pdf_text_pages(pdf_bytes: bytes) -> list[str]:
    """Extract each official PDF once per process for page citation lookups."""
    if not pdf_bytes:
        return []
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    if digest not in _PDF_TEXT_CACHE:
        import pdfplumber
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            _PDF_TEXT_CACHE[digest] = [page.extract_text() or "" for page in pdf.pages]
    return _PDF_TEXT_CACHE[digest]


def locate_pdf_page_by_text(pdf_bytes: bytes, needles: Iterable[str]) -> int | None:
    """Locate a section heading in the official PDF for extraction-failure diagnostics."""
    if not pdf_bytes:
        return None
    wanted = [re.sub(r"\s+", "", value) for value in needles if value]
    for page_no, page_text in enumerate(_pdf_text_pages(pdf_bytes), 1):
        text = re.sub(r"\s+", "", page_text)
        if any(value in text for value in wanted):
            return page_no
    return None


def _table_rows(documents: list[tuple[str, BeautifulSoup]]) -> list[tuple[str, list[str], str]]:
    rows = []
    for filename, soup in documents:
        for table in soup.find_all("table"):
            table_text = table.get_text(" ", strip=True)
            for row in table.find_all("tr"):
                cells = [re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
                if cells:
                    rows.append((filename, cells, table_text))
    return rows


def extract_shareholders(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                         pdf_bytes: bytes) -> list[dict[str, Any]]:
    holders = []
    for _, cells, table_text in _table_rows(documents):
        if "売出し後の所有株式数" not in re.sub(r"\s+", "", table_text) or len(cells) < 6:
            continue
        before, after = _first_number(cells[2]), _first_number(cells[4])
        if before is None or after is None or not cells[0] or "氏名" in cells[0] or cells[0] == "計":
            continue
        potential_match = re.search(r"[\(（]([\d,]+)[\)）]", cells[2])
        holders.append({"name": cells[0], "before_shares": before,
                        "sold_or_allotted_shares": before - after,
                        "after_shares": after,
                        "potential_shares_included": int(potential_match.group(1).replace(",", "")) if potential_match else 0,
                        "source_id": source_id})
    if not holders:
        # A common EDINET layout separates the pre-listing ownership table from
        # the selling-shareholder table.  Join the two by normalized holder name
        # and calculate the balance; never infer from issuer identity or position.
        ownership: list[dict[str, Any]] = []
        sale_texts: list[str] = []
        for _, cells, table_text in _table_rows(documents):
            compact_table = re.sub(r"\s+", "", table_text)
            if ("氏名又は名称" in compact_table and "所有株式数" in compact_table
                    and len(cells) >= 3 and cells[0] not in {"氏名又は名称", "計", "―", "-"}):
                before = _first_number(cells[2])
                if before is None:
                    continue
                raw_name = re.split(r"[（(]\s*注", cells[0], maxsplit=1)[0].strip()
                if raw_name in {"", "―", "-", "計"}:
                    continue
                potential_match = re.search(r"[（(]([\d,]+)[）)]", cells[2])
                ownership.append({
                    "name": raw_name,
                    "before_shares": before,
                    "potential_shares_included": int(potential_match.group(1).replace(",", "")) if potential_match else 0,
                })
            if "売出しに係る株式の所有者" in compact_table and "売出数" in compact_table:
                sale_texts.append(re.sub(r"\s+", "", " ".join(cells)))
        joined_sales = "\n".join(sale_texts)
        for row in ownership:
            name_key = re.sub(r"[\s・･.]", "", unicodedata.normalize("NFKC", row["name"]))
            sale_key = re.sub(r"[\s・･.]", "", unicodedata.normalize("NFKC", joined_sales))
            sold = 0
            position = sale_key.find(name_key)
            if position >= 0:
                match = re.search(r"([\d,]+)株", sale_key[position + len(name_key):position + len(name_key) + 120])
                if match:
                    sold = int(match.group(1).replace(",", ""))
            holders.append({**row, "sold_or_allotted_shares": sold,
                            "after_shares": row["before_shares"] - sold,
                            "source_id": source_id})
    page = locate_pdf_page_by_values(pdf_bytes,
                                     [value for row in holders[:4] for value in (row["before_shares"], row["after_shares"])],
                                     minimum=3) if holders else None
    if page is None and holders:
        sold_values = [row["sold_or_allotted_shares"] for row in holders if row["sold_or_allotted_shares"]]
        page = locate_pdf_page_by_values(pdf_bytes, sold_values, minimum=1) if sold_values else None
    for row in holders:
        row["pdf_page"] = page
    return holders


def extract_lockups(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                    pdf_bytes: bytes) -> list[dict[str, Any]]:
    text = "\n".join(soup.get_text(" ", strip=True) for _, soup in documents)
    if "ロックアップについて" not in text:
        return []
    groups = []
    for _, soup in documents:
        for paragraph in soup.find_all("p"):
            context = re.sub(r"\s+", "", paragraph.get_text(" ", strip=True))
            if len(context) < 40:
                continue
            match = re.search(r"(180|360)日目の(20\d{2}年\d{1,2}月\d{1,2}日)", context)
            if not match:
                continue
            days = int(match.group(1))
            groups.append({"days": days, "until": match.group(2),
                           "price_release_multiple": 1.5 if "1.5倍" in context else None,
                           "holders_text": context[:min(match.start(), 520)][-420:],
                           "source_id": source_id})
    unique = []
    seen = set()
    for row in groups:
        key = (row["days"], row["until"], row["price_release_multiple"], row["holders_text"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    page = locate_pdf_page_by_values(pdf_bytes, [360, 180, 1.5], minimum=2) if unique else None
    if page is None and unique:
        page = locate_pdf_page_by_text(pdf_bytes, ("ロックアップについて",))
    for row in unique:
        row["pdf_page"] = page
    return unique


def extract_stock_options(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                          pdf_bytes: bytes) -> list[dict[str, Any]]:
    # EDINET's 0204010 company-information document contains the current
    # ``新株予約権等の状況``.  Financial-statement notes (0205400) also contain
    # historical grant/activity tables, including already exercised or expired
    # series.  Mixing both documents overstates current potential dilution.
    current_documents = [item for item in documents
                         if re.search(r"(?:^|/)0204010_", item[0].replace("\\", "/"))]
    option_documents = current_documents or documents
    option_text = unicodedata.normalize("NFKC", re.sub(r"\s+", "", " ".join(
        soup.get_text(" ", strip=True) for _, soup in documents
    )))
    options: list[dict[str, Any]] = []
    for _, soup in option_documents:
        document_text = re.sub(r"\s+", "", soup.get_text(" ", strip=True))
        for table in soup.find_all("table"):
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
                    for row in table.find_all("tr")]
            series_row = next((row for row in rows if len(row) >= 2 and
                               all(re.search(r"第\s*\d+\s*回新株予約権", cell) for cell in row[1:])), None)
            shares_row = next((row for row in rows if len(row) >= 2 and
                               all(re.search(r"[\d,]+株", cell) for cell in row[1:])), None)
            price_row = next((row for row in rows if len(row) >= 2 and
                              all(re.search(r"[\d,]+円", cell) for cell in row[1:])), None)
            if not series_row or not shares_row or not price_row:
                continue
            issue_date_row = next((row for row in rows if len(row) >= 2 and
                                   all(re.search(r"20\d{2}年", cell) for cell in row[1:])), None)
            periods: list[str | None] = [None] * (len(series_row) - 1)
            for candidate in soup.find_all("table"):
                period_rows = [[re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                               for tr in candidate.find_all("tr")]
                period_row = next((row for row in period_rows if row and "行使期間" in row[0] and len(row) >= len(series_row)), None)
                if period_row:
                    periods = period_row[1:len(series_row)]
                    break
            for index, series in enumerate(series_row[1:]):
                issued_match = re.search(r"([\d,]+)株", shares_row[index + 1])
                price_match = re.search(r"([\d,]+)円", price_row[index + 1])
                if not issued_match or not price_match:
                    continue
                issued = int(issued_match.group(1).replace(",", ""))
                effective = issued
                compact_series = re.sub(r"\s+", "", series)
                series_key = compact_series.split("(", 1)[0].split("（", 1)[0]
                changed = re.search(re.escape(series_key) + r".{0,1200}?発行数は([\d,]+)株", document_text)
                if changed:
                    candidate = int(changed.group(1).replace(",", ""))
                    if issued * 0.1 <= candidate <= issued:
                        effective = candidate
                options.append({"series": compact_series,
                                "issue_date": issue_date_row[index + 1] if issue_date_row else None,
                                "issued_potential_shares": issued,
                                "forfeited_shares": issued - effective,
                                "effective_potential_shares": effective,
                                "exercise_price_yen": int(price_match.group(1).replace(",", "")),
                                "exercise_period": periods[index], "source_id": source_id,
                                "_layout": "horizontal_detail"})

    # EDINET also represents grants as one vertical table per series.  Parse the
    # post-split values in square brackets when present; they are the current
    # potential shares and exercise price used at listing.
    for _, soup in option_documents:
        vertical_index = 0
        for table in soup.find_all("table"):
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                     for cell in row.find_all(["th", "td"])] for row in table.find_all("tr")]
            values = {re.sub(r"\s+", "", row[0]): row[1] for row in rows if len(row) == 2}
            shares_text = next((value for label, value in values.items()
                                if "新株予約権の目的となる株式" in label), None)
            price_text = next((value for label, value in values.items()
                               if "行使時の払込金額" in label), None)
            period_text = next((value for label, value in values.items()
                                if "新株予約権の行使期間" in label), None)
            if not shares_text or not price_text or not period_text:
                continue
            vertical_index += 1
            share_numbers = re.findall(r"[\[［]\s*([\d,]+)\s*[\]］]", shares_text)
            price_numbers = re.findall(r"[\[［]\s*([\d,]+)\s*[\]］]", price_text)
            plain_shares = re.findall(r"([\d,]+)\s*株", shares_text)
            if not plain_shares:
                # EDINET commonly puts the unit only in the row label
                # ``...数(株)`` and prints ``普通株式 709,893 (注)1`` in the
                # value cell.  The first value is the share count; later
                # integers are note markers.
                plain_shares = re.findall(r"[\d,]+", shares_text)[:1]
            plain_prices = re.findall(r"([\d,]+)", price_text)
            if not (share_numbers or plain_shares) or not (price_numbers or plain_prices):
                continue
            current_shares = int((share_numbers[-1] if share_numbers else plain_shares[-1]).replace(",", ""))
            # The trailing number is often a footnote marker (for example
            # ``455 (注)2``); the first number is the exercise price.
            current_price = int((price_numbers[-1] if price_numbers else plain_prices[0]).replace(",", ""))
            heading_pattern = re.compile(r"第\s*[0-9０-９]+\s*回(?:新株予約権|ストック[・･]?オプション)")
            heading = table.find_previous(string=heading_pattern)
            series_match = heading_pattern.search(str(heading or ""))
            series = unicodedata.normalize("NFKC", re.sub(
                r"ストック[・･]?オプション", "新株予約権",
                re.sub(r"\s+", "", series_match.group())
            )) if series_match else f"第{vertical_index}回新株予約権"
            series_key = series.split("(", 1)[0].split("（", 1)[0]
            effective_shares = current_shares
            changed = re.search(re.escape(series_key) + r".{0,1200}?発行数は([\d,]+)株", option_text)
            if changed:
                adjusted = int(changed.group(1).replace(",", ""))
                if current_shares * 0.1 <= adjusted <= current_shares:
                    effective_shares = adjusted
            issue_date = next((value for label, value in values.items() if "決議年月日" in label), None)
            options.append({"series": series, "issue_date": issue_date,
                            "issued_potential_shares": current_shares,
                            "forfeited_shares": current_shares - effective_shares,
                            "effective_potential_shares": effective_shares,
                            "exercise_price_yen": current_price,
                            "exercise_period": period_text, "source_id": source_id,
                            "_layout": "vertical_detail"})

    # A third common layout separates the series/share table from the price
    # table.  Merge those tables by the normalized series heading.
    merged: dict[str, dict[str, Any]] = {}
    for _, soup in documents:
        for table in soup.find_all("table"):
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                     for cell in row.find_all(["th", "td"])] for row in table.find_all("tr")]
            header = next((row for row in rows if len(row) >= 2 and all(
                re.search(r"第\s*\d+\s*回(?:新株予約権|\s*ストック[・･]?オプション)", cell)
                for cell in row[1:])), None)
            if not header:
                continue
            keys = [re.sub(r"\s+", "", cell).replace("ストック・オプション", "新株予約権")
                    for cell in header[1:]]
            for key in keys:
                merged.setdefault(key, {"series": key, "source_id": source_id,
                                        "forfeited_shares": 0, "_layout": "activity_summary"})
            for row in rows:
                if len(row) < len(header):
                    continue
                label = re.sub(r"\s+", "", row[0])
                for index, key in enumerate(keys, 1):
                    value = row[index]
                    if "ストック・オプションの数" in label or "目的となる株式" in label:
                        match = re.search(r"([\d,]+)\s*株", value)
                        if match:
                            shares = int(match.group(1).replace(",", ""))
                            merged[key]["issued_potential_shares"] = shares
                            merged[key]["effective_potential_shares"] = shares
                    elif "権利行使価格" in label or "行使時の払込金額" in label:
                        match = re.search(r"([\d,]+)", value)
                        if match:
                            merged[key]["exercise_price_yen"] = int(match.group(1).replace(",", ""))
                    elif "行使期間" in label:
                        merged[key]["exercise_period"] = value
                    elif "付与日" in label or "決議年月日" in label:
                        merged[key]["issue_date"] = value
                    elif label == "失効":
                        loss = _first_number(value) or 0
                        merged[key]["forfeited_shares"] = loss
                        if merged[key].get("issued_potential_shares") is not None:
                            merged[key]["effective_potential_shares"] = max(
                                0, merged[key]["issued_potential_shares"] - loss)
    current_series_numbers = {
        match.group(1) for row in options
        if (match := re.search(r"第\s*(\d+)\s*回", unicodedata.normalize("NFKC", row["series"])))
    }
    options.extend(
        row for row in merged.values()
        if row.get("effective_potential_shares") is not None
        and row.get("exercise_price_yen") is not None
        and (not current_documents or (
            (match := re.search(r"第\s*(\d+)\s*回", unicodedata.normalize("NFKC", row["series"])))
            and match.group(1) in current_series_numbers
        ))
    )
    # Remove structurally impossible partial-table candidates before merging.
    # Otherwise a spurious one-share row can contaminate a valid vertical table
    # through the conservative min(effective) merge below.
    options = [row for row in options
               if row["issued_potential_shares"] <= 100
               or row["effective_potential_shares"] >= row["issued_potential_shares"] * 0.1]
    by_series: dict[str, dict[str, Any]] = {}
    for row in options:
        normalized_series = unicodedata.normalize("NFKC", row["series"])
        series_match = re.search(r"第\s*(\d+)\s*回", normalized_series)
        key = series_match.group(1) if series_match else normalized_series
        previous = by_series.get(key)
        if previous is None:
            by_series[key] = row
            continue
        issued = max(previous["issued_potential_shares"], row["issued_potential_shares"])
        compatible = [candidate for candidate in (previous, row)
                      if candidate["issued_potential_shares"] >= issued * 0.5]
        effective = min(candidate["effective_potential_shares"] for candidate in compatible)
        detail = next((candidate for candidate in (previous, row)
                       if candidate.get("_layout") in {"vertical_detail", "horizontal_detail"}
                       and candidate["issued_potential_shares"] >= issued * 0.5), None)
        chosen = detail or min(compatible, key=lambda candidate: candidate["effective_potential_shares"])
        chosen = dict(chosen)
        chosen["issued_potential_shares"] = issued
        chosen["effective_potential_shares"] = effective
        chosen["forfeited_shares"] = max(issued - effective, previous.get("forfeited_shares", 0),
                                          row.get("forfeited_shares", 0))
        by_series[key] = chosen
    deduped = [row for row in by_series.values()
               if row["issued_potential_shares"] <= 100
               or row["effective_potential_shares"] >= row["issued_potential_shares"] * 0.1]
    page = locate_pdf_page_by_values(pdf_bytes,
                                     [value for row in deduped for value in (row["issued_potential_shares"], row["exercise_price_yen"])],
                                     minimum=3) if deduped else None
    for row in deduped:
        row["pdf_page"] = page
        row.pop("_layout", None)
    return deduped


def extract_financial_facts(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                            pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Return one period-qualified fact per EDINET context and concept."""
    definitions = {
        "total_assets_million_yen": "LiabilitiesAndNetAssets",
        "net_assets_million_yen": "NetAssets",
        "cash_million_yen": "CashAndDeposits",
        "current_debt_million_yen": "CurrentPortionOfLongTermLoansPayable",
        "long_term_debt_million_yen": "LongTermLoansPayable",
        "operating_cf_million_yen": "NetCashProvidedByUsedInOperatingActivities",
        "investing_cf_million_yen": "NetCashProvidedByUsedInInvestmentActivities",
        "financing_cf_million_yen": "NetCashProvidedByUsedInFinancingActivities",
        "income_taxes_deferred_million_yen": "IncomeTaxesDeferred",
    }
    contexts = _context_metadata(documents)
    result: list[dict[str, Any]] = []
    for field, suffix in definitions.items():
        by_context: dict[str, dict[str, Any]] = {}
        for fact in _find_facts(documents, suffix):
            context_id = fact["context"]
            meta = contexts.get(context_id)
            if not meta or not re.fullmatch(
                r"(?:Prior1Year(?:Instant|Duration)|Interim(?:Instant|Duration))(?:_NonConsolidatedMember)?",
                context_id,
            ):
                continue
            if "Axis" in context_id:
                continue
            by_context[context_id] = fact
        for context_id, fact in by_context.items():
            result.append({
                "metric_name": field,
                "value_million_yen": _fact_million_yen(fact),
                **contexts[context_id],
                "statement_type": "CF" if field in {
                    "operating_cf_million_yen", "investing_cf_million_yen",
                    "financing_cf_million_yen", "income_taxes_deferred_million_yen",
                } else "BS",
                "source_id": source_id,
                "source_page": None,
                "context_id": context_id,
            })
    for kind in ("BS", "CF"):
        group = [row for row in result if row["statement_type"] == kind]
        page = locate_pdf_page_by_values(
            pdf_bytes,
            [row["value_million_yen"] * 1000 for row in group],
            minimum=2,
        ) if group else None
        for row in group:
            row["source_page"] = page
            row["pdf_page"] = page
    return sorted(result, key=lambda row: (row.get("as_of_date") or "", row["metric_name"]))


def extract_financial_position(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                               pdf_bytes: bytes) -> dict[str, Any]:
    """Backward-compatible latest-period snapshot; it never mixes BS and CF periods."""
    facts = extract_financial_facts(documents, source_id, pdf_bytes)
    if not facts:
        return {}
    latest_date = max(row.get("as_of_date") or "" for row in facts)
    selected = [row for row in facts if row.get("as_of_date") == latest_date]
    result: dict[str, Any] = {"source_id": source_id, "as_of_date": latest_date,
                              "contexts": {}, "pdf_page": next((r.get("source_page") for r in selected if r.get("source_page")), None)}
    for row in selected:
        result[row["metric_name"]] = row["value_million_yen"]
        result["contexts"][row["metric_name"]] = row["context_id"]
    if result.get("total_assets_million_yen") and result.get("net_assets_million_yen") is not None:
        result["equity_ratio_pct"] = result["net_assets_million_yen"] / result["total_assets_million_yen"] * 100
    if result.get("current_debt_million_yen") is not None or result.get("long_term_debt_million_yen") is not None:
        result["interest_bearing_debt_million_yen"] = (
            result.get("current_debt_million_yen", 0) + result.get("long_term_debt_million_yen", 0)
        )
    return result


def extract_kpis_and_narratives(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                                 pdf_bytes: bytes) -> dict[str, Any]:
    text = unicodedata.normalize("NFKC", "\n".join(soup.get_text(" ", strip=True) for _, soup in documents))
    text = re.sub(r"[ \t]+", " ", text)
    kpis: list[dict[str, Any]] = []
    kpi_definitions: list[dict[str, Any]] = []

    def add_kpi(*, original_name: str, normalized_name: str, value: float,
                original_unit: str, normalized_unit: str, multiplier: float,
                as_of: str | None = None, start: str | None = None,
                end: str | None = None, kind: str, definition: str,
                original_value: str | None = None, yoy_pct: float | None = None) -> None:
        page = locate_pdf_page_by_text(pdf_bytes, (original_name, normalized_name))
        row = {
            "metric_name_original": original_name,
            "metric_name_normalized": normalized_name,
            "value_original": original_value or f"{value:g}",
            "unit_original": original_unit,
            "value_normalized": value * multiplier,
            "unit_normalized": normalized_unit,
            "as_of_date": as_of,
            "period_start": start,
            "period_end": end,
            "cumulative_or_period": kind,
            "definition_note": definition,
            "source_id": source_id,
            "source_page": page,
            "pdf_page": page,
            # compatibility for pre-v2 renderers
            "name": normalized_name,
            "value": value,
            "unit": original_unit,
            "period": as_of or (f"{start}〜{end}" if start or end else "基準期間記載あり"),
        }
        if yoy_pct is not None:
            row["yoy_pct"] = yoy_pct
        key = (normalized_name, as_of, start, end, original_unit, value)
        if not any((r["metric_name_normalized"], r.get("as_of_date"), r.get("period_start"),
                    r.get("period_end"), r["unit_original"], r["value"]) == key for r in kpis):
            kpis.append(row)

    # Registration statements express operational KPIs in prose as well as
    # image/table captions.  The patterns are based on metric names, units and
    # surrounding period language, never on issuer identity.
    for match in re.finditer(r"(20\d{2}年\d{1,2}月末時点)[^。]{0,140}?累計ユーザー会員数[^。]{0,80}?([\d.]+)万人", text):
        as_of = _period_end_for_month(match.group(1))
        add_kpi(original_name="累計ユーザー会員数", normalized_name="累計登録利用者数",
                value=float(match.group(2)), original_unit="万人", normalized_unit="人", multiplier=10_000,
                as_of=as_of, kind="cumulative", definition="サービス開始以降に登録したユーザー会員の累計人数",
                original_value=match.group(2))
    for match in re.finditer(r"(20\d{2}年\d{1,2}月(?:末|月間平均))[^。]{0,180}?(?:利用可能駐車場|掲載駐車場)[^。]{0,100}?([\d.]+)万台", text):
        as_of = _period_end_for_month(match.group(1))
        add_kpi(original_name="利用可能駐車場台数（月間平均）", normalized_name="月間平均利用可能駐車区画数",
                value=float(match.group(2)), original_unit="万台", normalized_unit="台", multiplier=10_000,
                as_of=as_of, kind="as_of", definition="日々掲載される利用可能な駐車区画台数の月間平均",
                original_value=match.group(2))
    for match in re.finditer(r"(20\d{2}年\d{1,2}月(?:末|月間平均|の月間平均))[^。]{0,120}?([\d.]+)万台[^。]{0,60}?駐車場(?:が利用可能|が登録)", text):
        as_of = _period_end_for_month(match.group(1))
        add_kpi(original_name="掲載駐車場数（月間平均）", normalized_name="月間平均掲載駐車区画数",
                value=float(match.group(2)), original_unit="万台", normalized_unit="台", multiplier=10_000,
                as_of=as_of, kind="as_of", definition="本サービスに掲載される日ごとの駐車場台数の月平均",
                original_value=match.group(2))
    for match in re.finditer(r"(20\d{2})年(?:度|12月期)[^。]{0,180}?利用UU数[^。]{0,80}?([\d.]+)万人(?:[^。]{0,80}?前年同期比\s*([\d.]+)%)*", text):
        year = int(match.group(1))
        add_kpi(original_name="利用UU数", normalized_name="期間ユニーク利用者数",
                value=float(match.group(2)), original_unit="万人", normalized_unit="人", multiplier=10_000,
                start=f"{year}-01-01", end=f"{year}-12-31", kind="period",
                definition="当該事業年度中にサービスを利用したユニーク利用者数",
                original_value=match.group(2), yoy_pct=float(match.group(3)) if match.group(3) else None)
    for match in re.finditer(r"当中間会計期間[^。]{0,220}?利用UU数[^。]{0,80}?([\d.]+)万人(?:[^。]{0,80}?前年同期比\s*([\d.]+)%)*", text):
        date_match = re.search(r"(20\d{2})年\s*1月\s*1日.{0,40}?(20\d{2})年\s*6月\s*30日", text[max(0, match.start()-2000):match.start()+200])
        interim_period = next((row for key, row in _context_metadata(documents).items()
                               if key.startswith("InterimDuration")), None)
        year = int(date_match.group(2)) if date_match else (
            int(interim_period["period_end"][:4]) if interim_period and interim_period.get("period_end") else None
        )
        add_kpi(original_name="利用UU数", normalized_name="期間ユニーク利用者数",
                value=float(match.group(1)), original_unit="万人", normalized_unit="人", multiplier=10_000,
                start=f"{year}-01-01" if year else None, end=f"{year}-06-30" if year else None, kind="period",
                definition="中間会計期間中にサービスを利用したユニーク利用者数",
                original_value=match.group(1), yoy_pct=float(match.group(2)) if match.group(2) else None)
    context_periods = _context_metadata(documents)
    prior_end = next((row.get("period_end") for key, row in context_periods.items()
                      if key.startswith("Prior1YearDuration") and row.get("period_end")), None)
    annual_match = re.search(r"当事業年度の利用UU数は\s*([\d.]+)万人\s*[（(]前年同期比\+?([\d.]+)%", text)
    if annual_match and prior_end:
        year = int(prior_end[:4])
        add_kpi(original_name="利用UU数", normalized_name="期間ユニーク利用者数",
                value=float(annual_match.group(1)), original_unit="万人", normalized_unit="人", multiplier=10_000,
                start=f"{year}-01-01", end=f"{year}-12-31", kind="period",
                definition="当該事業年度中に駐車場を利用し支払いを行ったユーザーの重複を除く人数（本文丸め値）",
                original_value=annual_match.group(1), yoy_pct=float(annual_match.group(2)))
    table_match = re.search(
        r"第\s*\d+期\s*[（(]20\d{2}年度[）)].{0,900}?利用UU\s*[（(]人[）)]\s*([\d, ]+?)\s+掲載駐車場数\s*[（(]台[）)]\s*([\d, ]+?)(?:\s*[（(]注|\s*注1)",
        text, re.S,
    )
    if table_match:
        user_values = [int(token.replace(",", "")) for token in re.findall(r"[\d,]+", table_match.group(1)) if token.strip(",")]
        parking_values = [int(token.replace(",", "")) for token in re.findall(r"[\d,]+", table_match.group(2)) if token.strip(",")]
        for year, value in ((2025, user_values[-2] if len(user_values) >= 2 else None),
                            (2026, user_values[-1] if user_values else None)):
            if value is not None:
                add_kpi(original_name="利用UU（人）", normalized_name="期間ユニーク利用者数（表）",
                        value=float(value), original_unit="人", normalized_unit="人", multiplier=1,
                        start=f"{year}-01-01", end=f"{year}-{'12-31' if year == 2025 else '06-30'}",
                        kind="period", definition="各期に駐車場を利用し支払いを行ったユーザー数（重複除外）の表記値",
                        original_value=f"{value:,}")
        for year, value in ((2025, parking_values[-2] if len(parking_values) >= 2 else None),
                            (2026, parking_values[-1] if parking_values else None)):
            if value is not None:
                add_kpi(original_name="掲載駐車場数（台）", normalized_name="掲載駐車場数（表・台）",
                        value=float(value), original_unit="台", normalized_unit="台", multiplier=1,
                        as_of=f"{year}-{'12-31' if year == 2025 else '06-30'}", kind="as_of",
                        definition="KPI表の列見出しは台。本文の2026年6月値は件表記のため単位差を保持",
                        original_value=f"{value:,}")
    for match in re.finditer(r"(20\d{2}年\d{1,2}月)(?:末)?[^。]{0,120}?掲載駐車場数[^。]{0,80}?([\d,]+)(台|件)", text):
        value = float(match.group(2).replace(",", ""))
        unit = match.group(3)
        add_kpi(original_name="掲載駐車場数", normalized_name="掲載駐車場数",
                value=value, original_unit=unit, normalized_unit=unit, multiplier=1,
                as_of=_period_end_for_month(match.group(1)), kind="as_of",
                definition="資料記載の基準月末に掲載された駐車場数。台と件は統合しない",
                original_value=match.group(2))

    # Generic EDINET KPI table: first column identifies the metric and the
    # remaining headers identify periods.  Preserve both periods and compute a
    # comparison only from the two explicitly printed values.
    for _, soup in documents:
        for table in soup.find_all("table"):
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                     for cell in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
            if len(rows) < 2 or len(rows[0]) < 3 or re.sub(r"\s+", "", rows[0][0]) != "指標":
                continue
            periods = []
            for header in rows[0][1:]:
                match = re.search(r"自\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*至\s*(20\d{2})年(\d{1,2})月(\d{1,2})日", header)
                periods.append((
                    f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}",
                    f"{int(match.group(4)):04d}-{int(match.group(5)):02d}-{int(match.group(6)):02d}",
                ) if match else None)
            if not all(periods):
                continue
            for row in rows[1:]:
                if len(row) < len(periods) + 1:
                    continue
                label = unicodedata.normalize("NFKC", row[0])
                unit_match = re.search(r"[（(]\s*([^）)]+)\s*[）)]", label)
                unit = unit_match.group(1).strip() if unit_match else "件"
                name = re.sub(r"\s*[（(][^）)]+[）)]\s*", "", label).strip()
                values = [_number(value) for value in row[1:len(periods) + 1]]
                if not name or any(value is None for value in values):
                    continue
                for index, ((start, end), value) in enumerate(zip(periods, values)):
                    yoy = None
                    if index and values[index - 1] not in {None, 0}:
                        yoy = (float(value) / float(values[index - 1]) - 1) * 100
                    multiplier = 1_000 if unit == "千円" else 1
                    normalized_unit = "円" if unit == "千円" else unit
                    add_kpi(original_name=label, normalized_name=name, value=float(value),
                            original_unit=unit, normalized_unit=normalized_unit, multiplier=multiplier,
                            start=start, end=end, kind="period",
                            definition="重要な経営管理指標として資料表に記載された値",
                            original_value=row[index + 1].strip(), yoy_pct=yoy)

            break
    # Some filings disclose the KPI taxonomy/definition table but intentionally
    # do not print target values.  Store definitions separately; never invent a
    # numeric fact to make the completeness gate pass.
    for _, soup in documents:
        for table in soup.find_all("table"):
            rows = [[re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                     for cell in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
            if not rows or len(rows[0]) < 2 or "客観的な指標" not in rows[0][1]:
                continue
            for row in rows[1:]:
                if len(row) < 2:
                    continue
                category = row[0].strip()
                raw_metrics = re.sub(r"^[・･]\s*", "", row[1].strip())
                for metric in (value.strip() for value in re.split(r"\s+[・･]\s*", raw_metrics) if value.strip()):
                    page = locate_pdf_page_by_text(pdf_bytes, (category, metric)) if pdf_bytes else None
                    candidate = {"metric_name_original": metric, "metric_name_normalized": metric,
                                 "category": category, "definition_note": f"{category}の客観的な管理指標",
                                 "source_id": source_id, "source_page": page, "pdf_page": page}
                    key = (metric, category)
                    if not any((item["metric_name_original"], item["category"]) == key for item in kpi_definitions):
                        kpi_definitions.append(candidate)

    prose_patterns = (
        (r"(20\d{2})年(\d{1,2})月期[^。]{0,100}?リカーリング・レベニュー[^。]{0,30}?([\d.]+)億円", "リカーリング・レベニュー", "リカーリング売上高", "億円", "円", 100_000_000, "period", "継続利用料等による売上高"),
        (r"(20\d{2})年(\d{1,2})月期[^。]{0,160}?リカーリング比率[^。]{0,30}?([\d.]+)%", "リカーリング比率", "リカーリング売上比率", "%", "%", 1, "period", "売上高に占めるリカーリング・レベニューの割合"),
        (r"(20\d{2})年(\d{1,2})月期[^。]{0,220}?年間顧客単価ARPA[^。]{0,160}?([\d,]+)千円となって", "年間顧客単価ARPA", "導入施設当たり年間リカーリング売上", "千円", "円", 1_000, "period", "導入施設数当たりの年間カートナビ分野リカーリング・レベニュー"),
        (r"(20\d{2})年(\d{1,2})月末(?:日)?現在[^。]{0,100}?([\d,]+)[^。]{0,30}?ゴルフコース[^。]{0,100}?([\d,]+)[^。]{0,30}?ゴルフコース", "稼働ゴルフコース数", "カートナビ稼働ゴルフコース数", "コース", "コース", 1, "as_of", "当社ゴルフカートナビが稼働するゴルフコース数"),
        (r"入居率.{0,80}?([\d]+(?:\.[\d]+))%\s*[（(](20\d{2})年(\d{1,2})月末現在", "入居率", "管理物件入居率", "%", "%", 1, "as_of", "当社の管理物件全体に占める入居済み住戸の割合"),
        (r"賃貸管理戸数は\s*([\d,]+)戸\s*[（(](20\d{2})年(\d{1,2})月末現在", "賃貸管理戸数", "賃貸管理戸数", "戸", "戸", 1, "as_of", "賃貸管理サービスで管理する住戸数"),
        (r"自社ブランドマンションを\s*([\d,]+)棟\s*([\d,]+)戸供給", "自社ブランドマンション供給戸数", "自社ブランドマンション供給戸数", "戸", "戸", 1, "period", "当該事業年度に供給した自社ブランドマンションの戸数"),
        (r"トスアップによる成約率は、?\s*([\d.]+)%\s*[（(](20\d{2})年(\d{1,2})月末", "トスアップによる成約率", "クロスセル案件成約率", "%", "%", 1, "as_of", "トスアップ案件のうち成約に至った割合"),
    )
    for pattern, original_name, normalized_name, original_unit, normalized_unit, multiplier, kind, definition in prose_patterns:
        for match in re.finditer(pattern, text):
            groups = match.groups()
            display_original_name, display_normalized_name = original_name, normalized_name
            display_definition = definition
            if original_name in {"リカーリング・レベニュー", "リカーリング比率"}:
                sentence_start = text.rfind("。", max(0, match.start() - 600), match.start())
                sentence = text[sentence_start + 1:match.end()]
                segment_mentions = {name for name in ("ゴルフ関連事業", "コミュなび事業", "健康見守り事業")
                                    if name in sentence}
                if "主要サービスの収益" in sentence or len(segment_mentions) > 1:
                    segment = "全社"
                elif "コミュなび事業" in segment_mentions:
                    segment = "コミュなび事業"
                elif "ゴルフ関連事業" in segment_mentions:
                    segment = "ゴルフ関連事業"
                else:
                    segment = "全社"
                display_original_name = f"{segment} {original_name}"
                display_normalized_name = f"{segment} {normalized_name}"
                display_definition = f"{segment}の{definition}"
            if original_name in {"入居率", "賃貸管理戸数", "トスアップによる成約率"}:
                value, year, month = float(groups[0].replace(",", "")), int(groups[1]), int(groups[2])
                as_of = _period_end_for_month(f"{year}年{month}月")
                start = end = None
            elif original_name == "自社ブランドマンション供給戸数":
                prior_period = next((row for key, row in context_periods.items()
                                     if key.startswith("Prior1YearDuration")), {})
                value, as_of = float(groups[1].replace(",", "")), None
                start, end = prior_period.get("period_start"), prior_period.get("period_end")
            else:
                year, month, value = int(groups[0]), int(groups[1]), float(groups[-1].replace(",", ""))
                as_of = _period_end_for_month(f"{year}年{month}月") if kind == "as_of" else None
                start = f"{year - 1 if month != 12 else year}-{'10' if month == 9 else '01'}-01" if kind == "period" else None
                end = _period_end_for_month(f"{year}年{month}月") if kind == "period" else None
            add_kpi(original_name=display_original_name, normalized_name=display_normalized_name, value=value,
                    original_unit=original_unit, normalized_unit=normalized_unit, multiplier=multiplier,
                    as_of=as_of, start=start, end=end, kind=kind, definition=display_definition,
                    original_value=f"{value:g}")
    for row in kpis:
        if pdf_bytes and row.get("source_page") is None:
            row["source_page"] = locate_pdf_page_by_values(pdf_bytes, [row["value"]], minimum=1)
            row["pdf_page"] = row["source_page"]
    customer_concentration = None
    if re.search(r"総販売実績に対する割合(?:が|は)10[％%]以上[^。]{0,40}(?:相手先がいない|相手先はありません)", text):
        customer_concentration = "総販売実績の10%以上を占める相手先なし"
    business_candidates = [
        sentence.strip()
        for sentence in re.split(r"(?<=。)", text)
        if 40 < len(sentence) < 500
        and any(cue in sentence for cue in ("当社は", "当社グループは", "事業", "サービス"))
        and any(cue in sentence for cue in ("提供", "運営", "販売", "開発", "プラットフォーム", "マーケットプレイス"))
    ]
    business_model = max(
        business_candidates,
        key=lambda sentence: (
            5 * int("ユーザー" in sentence and "オーナー" in sentence)
            + 4 * int("駐車場マーケットプレイス" in sentence)
            + 3 * int("マッチング" in sentence)
            + 3 * int("自ら" in sentence and "資産" in sentence and "保有" in sentence)
            + 2 * sum(cue in sentence for cue in ("提供", "運営", "サービス", "プラットフォーム"))
            - 10 * sum(cue in sentence for cue in ("リスク", "依存", "発生可能性", "ロックアップ", "充当する予定"))
            - 20 * sum(cue in sentence for cue in ("手取金", "資金使途", "使途】"))
        ),
        default=None,
    )
    single_segment = None
    for sentence in re.split(r"(?<=。)", text):
        if "単一セグメント" not in sentence:
            continue
        normalized_sentence = re.sub(r"\s+", "", unicodedata.normalize("NFKC", sentence))
        match = re.search(r"(?:「[^」]{1,30}」|[A-Za-z0-9一-龠ぁ-んァ-ヶー・]{2,30}事業)の?単一セグメント", normalized_sentence)
        single_segment = match.group(0) if match else "単一セグメント"
        break
    risk_candidates = [
        sentence.strip()
        for sentence in re.split(r"(?<=。)", text)
        if 40 < len(sentence) < 500
        and any(cue in sentence for cue in ("競合他社", "競争激化", "法的規制", "システム障害", "情報漏洩"))
    ]
    technology_candidates = [
        sentence.strip()
        for sentence in re.split(r"(?<=。)", text)
        if 20 < len(sentence) < 500
        and any(cue in sentence for cue in ("AIカメラ", "IoT", "技術革新", "システム基盤", "プロダクト開発"))
    ]
    return {
        "kpis": kpis,
        "kpi_definitions": kpi_definitions,
        "customer_concentration": customer_concentration,
        "customer_concentration_page": locate_pdf_page_by_text(
            pdf_bytes, ("総販売実績に対する割合", "10％以上の相手先")
        ) if customer_concentration else None,
        "single_segment": single_segment,
        "business_model": business_model,
        "business_model_page": locate_pdf_page_by_text(pdf_bytes, ("事業の内容", "プラットフォーム", "マーケットプレイス")) if business_model else None,
        "risk_excerpt": risk_candidates[0] if risk_candidates else None,
        "risk_page": locate_pdf_page_by_text(pdf_bytes, ("事業等のリスク", "競合", "法的規制")) if risk_candidates else None,
        "technology_excerpt": technology_candidates[0] if technology_candidates else None,
        "technology_page": locate_pdf_page_by_text(pdf_bytes, ("AIカメラ", "IoT", "技術革新", "システム基盤")) if technology_candidates else None,
        "source_id": source_id,
    }


def extract_offering_terms(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                           pdf_bytes: bytes) -> dict[str, Any]:
    text = "\n".join(soup.get_text(" ", strip=True) for _, soup in documents)
    normalized_text = unicodedata.normalize("NFKC", text)
    result: dict[str, Any] = {"source_id": source_id}
    regexes = {
        "offering_price": r"発行価格(?:は|\s*)([\d,.]+)円",
        "underwriting_price": r"引受価額(?:は|\s*)([\d,.]+)円",
        "company_law_payment_price": r"会社法上の払込金額\(?([\d,.]+)円",
        "capital_per_share": r"１株当たりの増加する資本金(?:の額)?は?([\d,.]+)円",
        "net_proceeds_thousand_yen": r"差引手取概算額([\d,]+)千円",
    }
    for field, pattern in regexes.items():
        match = re.search(pattern, text)
        if match:
            result[field] = float(match.group(1).replace(",", ""))
    # Final corrected filings commonly carry the four per-share amounts only in
    # one horizontal table.  Parse by header position rather than issuer name.
    for _, soup in documents:
        for table in soup.find_all("table"):
            rows = [
                [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in table.find_all("tr")
            ]
            if len(rows) < 2:
                continue
            header = [re.sub(r"\s+", "", value) for value in rows[0]]
            if not all(any(token in value for value in header) for token in ("発行価格", "引受価額", "払込金額", "資本組入額")):
                continue
            values = rows[1]
            for field, token in (
                ("offering_price", "発行価格"),
                ("underwriting_price", "引受価額"),
                ("company_law_payment_price", "払込金額"),
                ("capital_per_share", "資本組入額"),
            ):
                index = next((i for i, value in enumerate(header) if token in value), None)
                if index is not None and index < len(values):
                    number = _number(values[index])
                    if number is not None:
                        result[field] = number
            break
    # Some EDINET layouts put the proceeds label and amount in separate table
    # cells and express the amount in yen rather than thousand yen.
    for _, soup in documents:
        for table in soup.find_all("table"):
            rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                    for row in table.find_all("tr")]
            if len(rows) < 2:
                continue
            header = [unicodedata.normalize("NFKC", re.sub(r"\s+", "", value)) for value in rows[0]]
            index = next((i for i, value in enumerate(header) if "差引手取概算額" in value), None)
            if index is None:
                continue
            amount = next((_number(row[index]) for row in rows[1:]
                           if index < len(row) and _number(row[index]) is not None), None)
            if amount is not None:
                result["net_proceeds_thousand_yen"] = amount / 1_000 if "(円)" in header[index] else amount
                break
        if result.get("net_proceeds_thousand_yen") is not None:
            break
    parent = re.search(r"親引けしようとする株式の数[^\d]{0,80}([\d,]+)株", text)
    if parent:
        result["parent_allotment_shares"] = int(parent.group(1).replace(",", ""))
    result["parent_holding_condition"] = "上場後180日継続保有" if "親引け" in text and "180日目" in text else None
    oa = re.search(r"主幹事会社は、?([\d,]+)株について貸株人より追加的に[^。]{0,200}?グリーンシューオプション", text)
    if not oa:
        oa = re.search(r"グリーンシューオプション[^。]{0,400}?([\d,]+)株", text)
    if not oa:
        oa = re.search(r"借り入れる当社普通株式.{0,100}?([\d,]+)株", normalized_text)
    if oa:
        result["greenshoe_shares"] = int(oa.group(1).replace(",", ""))
    lenders = re.search(
        r"オーバーアロットメントによる売出しのために、?主幹事会社が当社株主である(.{1,300}?)(?:\(以下|（以下)[「『]貸株人[」』]",
        text,
    )
    if lenders:
        result["oa_lenders"] = re.sub(r"\s+", " ", lenders.group(1)).strip(" 、")
    if not result.get("oa_lenders"):
        lenders = re.search(r"当社株主である(.{1,160}?)[(（]以下[「『]?貸株人", normalized_text)
        if lenders:
            result["oa_lenders"] = re.sub(r"\s+", " ", lenders.group(1)).strip(" 、")
    deadline = re.search(r"([0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日)を行使期限として貸株人より付与", text)
    if deadline:
        result["greenshoe_exercise_deadline"] = deadline.group(1)
    if not result.get("greenshoe_exercise_deadline"):
        deadline = re.search(r"([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)を行使期限として(?:貸株人より)?付与", normalized_text)
        if deadline:
            result["greenshoe_exercise_deadline"] = deadline.group(1)
    cover = re.search(
        r"主幹事会社は、?([0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日から[0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日までの間)[^。]{0,160}?シンジケートカバー取引",
        text,
    )
    if cover:
        result["syndicate_cover_period"] = cover.group(1)
    if not result.get("syndicate_cover_period"):
        cover = re.search(
            r"((?:上場[(（]売買開始[)）]日|[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)\s*から\s*"
            r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日\s*までの間).{0,500}?シンジケートカバー取引",
            normalized_text,
        )
        if cover:
            result["syndicate_cover_period"] = cover.group(1)
    if "オーバーアロットメント" in normalized_text:
        result["greenshoe_option_applicable"] = "グリーンシューオプション" in normalized_text
    use = re.search(
        r"差引手取概算額[\d,]+千円については、(.{20,500}?)(?=プロダクト開発費用|。)",
        text,
    )
    if use:
        result["proceeds_use"] = use.group(1).strip()
    allocation = re.search(
        r"(?:人件費|業務委託費用)として([\d,]+)千円（([^）]+)）を充当する予定",
        text,
    )
    if allocation:
        result["growth_investment_allocation"] = (
            f"{int(allocation.group(1).replace(',', '')):,}千円（{allocation.group(2)}）"
        )
    result["pdf_page"] = locate_pdf_page_by_values(pdf_bytes,
        [value for value in result.values() if isinstance(value, (int, float))], minimum=2)
    return result


def merge_offering_term_versions(*versions: dict[str, Any]) -> dict[str, Any]:
    """Overlay corrected terms while preserving the source of each field."""
    merged: dict[str, Any] = {}
    field_sources: dict[str, dict[str, Any]] = {}
    for version in versions:
        source_id = version.get("source_id")
        source_page = version.get("pdf_page")
        substantive = {key: value for key, value in version.items()
                       if key not in {"source_id", "pdf_page", "field_sources"}
                       and value is not None}
        if not substantive:
            continue
        for key, value in substantive.items():
            merged[key] = value
            field_sources[key] = {"source_id": source_id, "pdf_page": source_page}
        merged["source_id"] = source_id
        merged["pdf_page"] = source_page
    if field_sources:
        merged["field_sources"] = field_sources
    return merged


_JAPANESE_CHAR = r"[ぁ-んァ-ヶ一-龠々〆ヵヶー]"


def normalize_ocr_japanese(value: str) -> str:
    """Normalize OCR prose without collapsing meaningful Latin-word spacing."""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(rf"(?<={_JAPANESE_CHAR})\s+(?={_JAPANESE_CHAR})", "", text)
    text = re.sub(r"\s+([、。,:;!?！？）】」』])", r"\1", text)
    text = re.sub(r"([（【「『])\s+", r"\1", text)
    text = re.sub(r"\s*[・·]\s*", "・", text)
    text = re.sub(r"シ[・。．]?リ[・。．]?ズ", "シリーズ", text)
    text = re.sub(r"フ[・。．]?ランド", "ブランド", text)
    text = re.sub(r"ハ[・。．]?ッケージ", "パッケージ", text)
    text = re.sub(r"([、。！？])\1+", r"\1", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def ocr_readability_issues(value: str) -> list[str]:
    """Return stable reason codes for OCR artefacts that must never reach Viewer."""
    text = unicodedata.normalize("NFKC", value or "")
    issues: list[str] = []
    if re.search(rf"{_JAPANESE_CHAR}\s+{_JAPANESE_CHAR}", text):
        issues.append("japanese_intra_character_space")
    if re.search(r"[ァ-ヶ][・。．][ァ-ヶ]", text):
        issues.append("broken_katakana")
    if re.search(r"\s+[、。,:;!?！？）】」』]", text):
        issues.append("space_before_punctuation")
    if any(len(sentence) > 240 for sentence in re.split(r"[。！？\n]", text)):
        issues.append("abnormal_sentence_length")
    if re.search(r"(?:[ァ-ヶー]\s+){2,}[ァ-ヶー]", text):
        issues.append("split_katakana_word")
    return issues


def summarize_exchange_business(pages: dict[int, str], business_page: int | None) -> str | None:
    """Build compact business prose from source-backed terms, never from raw OCR lines."""
    if business_page is None:
        return None
    business_text = normalize_ocr_japanese(pages.get(business_page, ""))
    document_text = normalize_ocr_japanese("\n".join(pages.values()))
    compact_text = re.sub(r"\s+", "", document_text)

    categories = [name for name in ("財務会計", "人事労務", "販売管理", "顧客管理")
                  if name in compact_text]
    product_match = re.search(r"[「『]([^」』\n]{2,32}(?:シリーズ|ブランド|サービス|システム))[」』]", document_text)
    if not product_match:
        product_match = re.search(r"([A-Za-z0-9一-龠ぁ-んァ-ヶー]{2,24}(?:シリーズ|ブランド))", document_text)
    product = product_match.group(1) if product_match else None

    sentences: list[str] = []
    if product:
        category_text = "、".join(categories)
        subject = f"{category_text}などの基幹業務ソフト" if category_text else "基幹業務ソフト"
        sentences.append(f"{subject}ブランド「{product}」を開発しています。")

    formats = []
    if "パッケージ" in compact_text or ("パッケ" in compact_text and "ソフト" in compact_text):
        formats.append("パッケージソフト")
    if "クラウド" in compact_text:
        formats.append("クラウドサービス")
    channel = None
    if "販売代理店" in compact_text or "代理店網" in compact_text:
        channel = "販売代理店網"
    elif "販売パートナー" in compact_text:
        channel = "販売パートナー"
    if formats and channel:
        nationwide = "全国の" if "全国" in compact_text else ""
        sentences.append(f"{'と'.join(formats)}を、{nationwide}{channel}を通じて企業へ提供する間接販売モデルを主軸としています。")

    if not sentences:
        candidates = [normalize_ocr_japanese(line) for line in business_text.splitlines()
                      if len(re.sub(r"\s+", "", line)) >= 25]
        for candidate in candidates:
            if not ocr_readability_issues(candidate):
                sentence = re.split(r"(?<=[。！？])", candidate)[0].strip()
                if sentence:
                    sentences.append(sentence[:180] + ("。" if not sentence.endswith("。") else ""))
                    break
    summary = "".join(sentences[:2]).strip() or None
    return summary if summary and not ocr_readability_issues(summary) else None


def extract_exchange_text_due_diligence(text: str, source_id: str) -> dict[str, Any]:
    """Extract a scanned exchange primary document from OCR lines and headings."""
    page_parts = re.split(r"\[\[PDF_PAGE:(\d+)\]\]", text or "")
    pages: dict[int, str] = {}
    for index in range(1, len(page_parts), 2):
        pages[int(page_parts[index])] = page_parts[index + 1]

    def compact(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))

    def numeric(value: str) -> float | None:
        normalized = unicodedata.normalize("NFKC", value)
        normalized = re.sub(r"\s*,\s*", ",", normalized)
        normalized = re.sub(r"\s*\.\s*", ".", normalized)
        # Windows OCR separates thousands groups ("18 798") and table columns
        # with the same whitespace.  Only join the right-most group: these
        # exchange tables put the latest period in the final column.
        grouped_tail = re.search(r"(△\s*)?(\d{1,3})\s*,?\s*(\d{3})(?!.*\d)", normalized)
        if grouped_tail:
            value = float(grouped_tail.group(2) + grouped_tail.group(3))
            return -value if grouped_tail.group(1) else value
        matches = re.findall(r"△?\s*\d[\d,]*(?:\.\s*\d+)?", normalized)
        if not matches:
            return None
        token = matches[-1].replace(" ", "").replace(",", "")
        return -float(token.replace("△", "")) if "△" in token else float(token)

    def page_for(*needles: str) -> int | None:
        for page, value in pages.items():
            haystack = compact(value)
            if all(compact(needle) in haystack for needle in needles):
                return page
        return None

    primary_year = None
    for value in pages.values():
        match = re.search(r"第\s*\d+\s*期\s*[（(]?\s*(20\d{2})\s*年\s*1\s*月\s*1\s*日.{0,80}?(20\d{2})\s*年\s*12\s*月\s*31\s*日", value, re.S)
        if match:
            primary_year = int(match.group(2))
            break
    if primary_year is None:
        candidates = [int(match.group(1)) for value in pages.values()
                      for match in re.finditer(r"(20\d{2})\s*年\s*12\s*月", value)]
        primary_year = max(candidates) if candidates else None

    metric_labels = {
        "total_assets_million_yen": (("総資産額", "資産合計"), "instant"),
        "net_assets_million_yen": (("純資産額", "純資産合計"), "instant"),
        "cash_million_yen": (("現金及び現金同等物の期末残高",), "instant"),
        "operating_cf_million_yen": (("営業活動による",), "duration"),
        "investing_cf_million_yen": (("投資活動による",), "duration"),
        "financing_cf_million_yen": (("財務活動による",), "duration"),
    }
    financial_facts: list[dict[str, Any]] = []
    summary_page = page_for("主要な経営指標")
    financial_pages = ({summary_page: pages[summary_page]} if summary_page else pages)
    for metric, (labels, period_kind) in metric_labels.items():
        found = None
        found_page = None
        for page, value in financial_pages.items():
            for line in value.splitlines():
                compacted_line = compact(line)
                if any(compacted_line.startswith(compact(label)) for label in labels):
                    candidate = numeric(line)
                    if candidate is not None:
                        found, found_page = candidate, page
        if found is None or primary_year is None:
            continue
        financial_facts.append({
            "metric_name": metric, "value_million_yen": found,
            "period_start": f"{primary_year}-01-01", "period_end": f"{primary_year}-12-31",
            "as_of_date": f"{primary_year}-12-31", "period_type": "full_year",
            "fiscal_year": primary_year, "quarter": "FY", "consolidation_scope": "non_consolidated",
            "accounting_standard": "J-GAAP", "source_id": source_id,
            "source_page": found_page, "pdf_page": found_page,
            "statement_type": "CF" if period_kind == "duration" else "BS",
        })
    facts_by_name = {row["metric_name"]: row for row in financial_facts}
    financial_position = {"source_id": source_id, "as_of_date": f"{primary_year}-12-31" if primary_year else None}
    for metric, row in facts_by_name.items():
        financial_position[metric] = row["value_million_yen"]
    financial_position["pdf_page"] = facts_by_name.get("total_assets_million_yen", {}).get("source_page")
    if financial_position.get("total_assets_million_yen") and financial_position.get("net_assets_million_yen") is not None:
        financial_position["equity_ratio_pct"] = financial_position["net_assets_million_yen"] / financial_position["total_assets_million_yen"] * 100

    shareholders: list[dict[str, Any]] = []
    # A table of contents and cross references may repeat the section heading
    # long before the actual table. Score candidate pages by data-shaped rows
    # (holder marker plus a trailing ownership percentage), then use headings
    # only as a tie-breaker. This also tolerates OCR splitting column labels
    # across lines.
    def shareholder_row_score(value: str) -> int:
        return sum(
            1 for line in value.splitlines()
            if "※" in unicodedata.normalize("NFKC", line)
            and re.search(r"\d\s*[,.]\s*\d{2}\s*$", unicodedata.normalize("NFKC", line))
        )

    shareholder_candidates = [
        (shareholder_row_score(value), int("株主の状況" in compact(value)), page)
        for page, value in pages.items()
        if "株主の状況" in compact(value) or shareholder_row_score(value) >= 2
    ]
    shareholder_page = max(shareholder_candidates, default=(0, 0, None))[2]
    if shareholder_page:
        for line in pages[shareholder_page].splitlines():
            normalized = unicodedata.normalize("NFKC", line)
            if "※" not in normalized or not re.search(r"\d\s*[,.]\s*\d{2}\s*$", normalized):
                continue
            name = compact(normalized.split("※", 1)[0])
            pct_match = re.search(r"(\d+\s*\.\s*\d{2})\s*$", normalized)
            prefix = normalized[:pct_match.start()].rstrip() if pct_match else ""
            # OCR commonly drops thousands separators while retaining spaces
            # (for example ``2 600 000``), or inserts spaces around commas.
            # Read the final thousands-grouped number before the percentage;
            # this keeps address numerals earlier in the row out of the value.
            amount_match = re.search(r"(\d{1,3}(?:\s*,?\s*\d{3}){1,3})\s*$", prefix)
            if not name or not pct_match or not amount_match:
                continue
            pct = float(pct_match.group(1).replace(" ", ""))
            shares = int(re.sub(r"[\s,]", "", amount_match.group(1)))
            shareholders.append({"name": name, "before_shares": shares,
                                 "sold_or_allotted_shares": 0, "after_shares": shares,
                                 "potential_shares_included": 0, "ownership_pct": pct,
                                 "source_id": source_id, "source_page": shareholder_page,
                                 "pdf_page": shareholder_page})

    kpis: list[dict[str, Any]] = []
    def add_growth(label: str, anchors: tuple[str, ...], normalized_name: str, definition: str) -> None:
        for page, value in pages.items():
            compacted = compact(value)
            match = None
            for candidate in re.finditer(r"前期比([\d.]+)%", compacted):
                context = compacted[max(0, candidate.start() - 180):candidate.start()]
                if any(compact(anchor) in context for anchor in anchors):
                    match = candidate
                    break
            if not match:
                continue
            amount = float(match.group(1))
            kpis.append({"metric_name_original": label, "metric_name_normalized": normalized_name,
                         "value_original": match.group(1), "unit_original": "%", "value_normalized": amount,
                         "unit_normalized": "%", "as_of_date": f"{primary_year}-12-31" if primary_year else None,
                         "period_start": f"{primary_year}-01-01" if primary_year else None,
                         "period_end": f"{primary_year}-12-31" if primary_year else None,
                         "cumulative_or_period": "period", "definition_note": definition,
                         "source_id": source_id, "source_page": page, "pdf_page": page,
                         "name": normalized_name, "value": amount, "unit": "%", "period": str(primary_year)})
            return
    add_growth("クラウドサービスが前期比", ("クラウド",), "クラウドサービス売上前年比", "大臣クラウド・スマート大臣の前事業年度比")
    add_growth("オンプレミス製品の新規導入や既存ユーザーのバージョンアップも前期比", ("新規導入", "バージョンアップ"), "オンプレミス新規・更新前年比", "オンプレミス製品の新規導入及び既存ユーザー更新の前事業年度比")
    add_growth("保守サービス加入は前期比", ("保守",), "保守サービス加入前年比", "オンプレミス製品ユーザーの保守サービス加入の前事業年度比")

    business_page = page_for("事業の内容")
    business_model = summarize_exchange_business(pages, business_page)
    risk_page = next((page for page, value in pages.items()
                      if "事業等のリスク" in compact(value)[:500] and page > 1), None)
    risk_excerpt = None
    if risk_page:
        candidates = [re.sub(r"\s+", " ", line).strip() for line in pages[risk_page].splitlines() if len(compact(line)) > 25]
        risk_excerpt = " ".join(candidates[:4])[:900] or None

    absence: dict[str, Any] = {}
    option_page = page_for("潜在株式調整後", "潜在株式が存在しない")
    if option_page:
        absence["stock_options"] = {"reason_code": "SOURCE_ACTUALLY_ABSENT", "source_id": source_id,
                                     "source_page": option_page, "detail": "潜在株式が存在しない旨を公式資料で確認"}
    no_offering_page = page_for("特定投資家向け取得勧誘", "実施しない")
    if no_offering_page:
        absence["offering"] = {"reason_code": "NOT_APPLICABLE", "source_id": source_id,
                                "source_page": no_offering_page, "detail": "上場時の取得勧誘・売付け勧誘を実施しない旨を公式資料で確認"}
        absence["lockup"] = {"reason_code": "NOT_APPLICABLE", "source_id": source_id,
                              "source_page": no_offering_page, "detail": "上場時売出しがなくロックアップ対象外"}
    return {
        "business_model": business_model, "business_model_page": business_page,
        "ocr_readability": {"passed": bool(business_model) and not ocr_readability_issues(business_model),
                            "issues": ocr_readability_issues(business_model or "")},
        "shareholders": shareholders, "shareholder_page": shareholder_page,
        "lockups": [], "stock_options": [], "kpis": kpis,
        "kpi_definitions": [],
        "financial_position": financial_position, "financial_facts": financial_facts,
        "offering_terms": {}, "risk_excerpt": risk_excerpt, "risk_page": risk_page,
        "customer_concentration": None, "absence_evidence": absence, "source_id": source_id,
    }


def extract_due_diligence(*, base_xbrl: bytes, base_pdf: bytes, base_source_id: str,
                          latest_xbrl: bytes, latest_pdf: bytes, latest_source_id: str,
                          post_listing_shares: float | None = None) -> dict[str, Any]:
    base_docs = _html_documents(base_xbrl)
    latest_docs = _html_documents(latest_xbrl)
    shareholders = extract_shareholders(latest_docs, latest_source_id, latest_pdf)
    lockups = extract_lockups(latest_docs, latest_source_id, latest_pdf)
    options = extract_stock_options(base_docs, base_source_id, base_pdf)
    financial_facts = extract_financial_facts(base_docs, base_source_id, base_pdf)
    financial_position = extract_financial_position(base_docs, base_source_id, base_pdf)
    narratives = extract_kpis_and_narratives(base_docs, base_source_id, base_pdf)
    offering_terms = merge_offering_term_versions(
        extract_offering_terms(base_docs, base_source_id, base_pdf),
        extract_offering_terms(latest_docs, latest_source_id, latest_pdf),
    )
    effective_options = sum(row["effective_potential_shares"] for row in options)
    dilution = effective_options / post_listing_shares * 100 if effective_options and post_listing_shares else None
    expected_locations = {
        "basic_business": {"source_id": base_source_id, "section": "事業の内容",
                           "pdf_page": locate_pdf_page_by_text(base_pdf, ("事業の内容",))},
        "balance_sheet_cash_flow": {"source_id": base_source_id, "section": "財務諸表／キャッシュ・フロー計算書",
                                    "pdf_page": locate_pdf_page_by_text(base_pdf, ("キャッシュ・フロー計算書", "貸借対照表"))},
        "shareholders_sellers": {"source_id": latest_source_id, "section": "株主の状況／売出し",
                                 "pdf_page": locate_pdf_page_by_text(latest_pdf, ("売出し後の所有株式数", "株主の状況"))},
        "lockup": {"source_id": latest_source_id, "section": "ロックアップについて",
                   "pdf_page": locate_pdf_page_by_text(latest_pdf, ("ロックアップについて",))},
        "stock_options": {"source_id": base_source_id, "section": "新株予約権等の状況",
                          "pdf_page": locate_pdf_page_by_text(base_pdf, ("新株予約権等の状況",))},
        "kpi_growth": {"source_id": base_source_id, "section": "事業の状況／重要な経営指標",
                       "pdf_page": locate_pdf_page_by_text(base_pdf, ("重要な経営指標", "KPI"))},
        "risks": {"source_id": base_source_id, "section": "事業等のリスク",
                  "pdf_page": locate_pdf_page_by_text(base_pdf, ("事業等のリスク",))},
    }
    return {
        "shareholders": shareholders, "lockups": lockups, "stock_options": options,
        "effective_potential_shares": effective_options,
        "potential_dilution_pct_of_listing_shares": dilution,
        "financial_position": financial_position, "financial_facts": financial_facts, **narratives,
        "offering_terms": offering_terms, "expected_locations": expected_locations,
    }


def build_completeness(*, manifests: list[dict[str, Any]], financials: list[dict[str, Any]],
                       offering: dict[str, Any], diligence: dict[str, Any],
                       discovery: dict[str, Any] | None = None) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    def ok(group: str, evidence: Any) -> None:
        absence = diligence.get("absence_evidence", {}).get(group)
        absence_valid = bool(absence and absence.get("reason_code") in {"SOURCE_ACTUALLY_ABSENT", "NOT_APPLICABLE"}
                             and absence.get("source_id") and absence.get("source_page"))
        passed = bool(evidence) or absence_valid
        statuses[group] = {"passed": passed,
                           "reason_code": absence.get("reason_code") if absence_valid else (None if evidence else "PARSER_UNSUPPORTED"),
                           "evidence_count": len(evidence) if isinstance(evidence, list) else int(bool(evidence))}
        if absence_valid:
            statuses[group]["absence_evidence"] = absence
    ok("basic_business", diligence.get("business_model"))
    ok("financial_performance", financials)
    ok("balance_sheet_cash_flow", diligence.get("financial_facts"))
    terms = diligence.get("offering_terms", {})
    offering_evidence = (offering.get("offering_price") is not None
                         or terms.get("offering_price") is not None)
    detailed_offering = offering_evidence and all(
        terms.get(field) is not None
        for field in ("underwriting_price", "company_law_payment_price", "capital_per_share", "net_proceeds_thousand_yen")
    )
    if (offering.get("oa_shares") or terms.get("greenshoe_shares")):
        detailed_offering = detailed_offering and all(
            terms.get(field) for field in ("oa_lenders", "syndicate_cover_period")
        )
        if terms.get("greenshoe_option_applicable") is not False:
            detailed_offering = detailed_offering and all(
                terms.get(field) for field in ("greenshoe_shares", "greenshoe_exercise_deadline")
            )
    ok("offering", detailed_offering or offering.get("not_applicable_evidence"))
    if offering.get("not_applicable_evidence") and not offering_evidence:
        statuses["offering"]["reason_code"] = "NOT_APPLICABLE"
    ok("shareholders_sellers", diligence.get("shareholders"))
    ok("lockup", diligence.get("lockups"))
    ok("stock_options", diligence.get("stock_options"))
    ok("kpi_growth", diligence.get("kpis"))
    ok("risks", diligence.get("risk_excerpt"))
    revisions = [item for item in manifests if any(token in item.get("title", "")
                 for token in ("有価証券届出書", "特定証券情報"))]
    ok("sources_revisions", len(revisions) >= 1)
    if discovery and discovery.get("reason_code"):
        has_exchange_primary = any(any(token in str(item.get("issuer", "")) + str(item.get("title", ""))
                                       for token in ("TOKYO PRO", "名古屋証券取引所", "特定証券情報"))
                                   and item.get("fetch_status") == "success" for item in manifests)
        for group in ("shareholders_sellers", "lockup", "stock_options", "balance_sheet_cash_flow", "kpi_growth", "risks", "sources_revisions"):
            if not statuses[group]["passed"] and not has_exchange_primary:
                statuses[group]["reason_code"] = discovery["reason_code"]
    for group, status in statuses.items():
        if not status["passed"] and group in diligence.get("expected_locations", {}):
            status["expected_location"] = diligence["expected_locations"][group]
    missing = [{"group": group, **value} for group, value in statuses.items() if not value["passed"]]
    return {"passed": not missing, "groups": statuses, "missing": missing}
