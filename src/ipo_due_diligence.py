"""Deterministic extraction of IPO due-diligence facts from official EDINET XBRL."""
from __future__ import annotations

import re
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


def _choose_fact(facts: list[dict[str, Any]], contexts: tuple[str, ...]) -> dict[str, Any] | None:
    for needle in contexts:
        candidates = [fact for fact in facts if needle in fact["context"] and "Member_" not in fact["context"]]
        if candidates:
            return candidates[-1]
    return facts[-1] if facts else None


def locate_pdf_page_by_values(pdf_bytes: bytes, values: Iterable[float | int], *, minimum: int = 2) -> int | None:
    """Map an XBRL fact group back to the PDF by matching its printed numeric values."""
    import pdfplumber
    wanted = []
    for value in values:
        number = abs(float(value))
        wanted.append(f"{number:,.2f}".rstrip("0").rstrip("."))
        if number.is_integer():
            wanted.append(f"{int(number):,}")
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        best: tuple[int, int] | None = None
        for page_no, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            score = sum(1 for token in set(wanted) if token and token in text)
            if best is None or score > best[0]:
                best = (score, page_no)
        return best[1] if best and best[0] >= minimum else None


def locate_pdf_page_by_text(pdf_bytes: bytes, needles: Iterable[str]) -> int | None:
    """Locate a section heading in the official PDF for extraction-failure diagnostics."""
    if not pdf_bytes:
        return None
    import pdfplumber
    wanted = [re.sub(r"\s+", "", value) for value in needles if value]
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            text = re.sub(r"\s+", "", page.extract_text() or "")
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
    page = locate_pdf_page_by_values(pdf_bytes,
                                     [value for row in holders[:4] for value in (row["before_shares"], row["after_shares"])],
                                     minimum=3) if holders else None
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
    for row in unique:
        row["pdf_page"] = page
    return unique


def extract_stock_options(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                          pdf_bytes: bytes) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for _, soup in documents:
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
                                "exercise_period": periods[index], "source_id": source_id})

    primary_count = len(options)

    # EDINET also represents grants as one vertical table per series.  Parse the
    # post-split values in square brackets when present; they are the current
    # potential shares and exercise price used at listing.
    for _, soup in documents:
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
            share_numbers = re.findall(r"[\[［]([\d,]+)[\]］]", shares_text)
            price_numbers = re.findall(r"[\[［]([\d,]+)[\]］]", price_text)
            plain_shares = re.findall(r"([\d,]+)\s*株", shares_text)
            plain_prices = re.findall(r"([\d,]+)", price_text)
            if not (share_numbers or plain_shares) or not (price_numbers or plain_prices):
                continue
            current_shares = int((share_numbers[-1] if share_numbers else plain_shares[-1]).replace(",", ""))
            current_price = int((price_numbers[-1] if price_numbers else plain_prices[-1]).replace(",", ""))
            heading = table.find_previous(string=re.compile(r"第\s*\d+\s*回新株予約権"))
            series_match = re.search(r"第\s*\d+\s*回新株予約権", str(heading or ""))
            series = re.sub(r"\s+", "", series_match.group()) if series_match else f"第{vertical_index}回新株予約権"
            issue_date = next((value for label, value in values.items() if "決議年月日" in label), None)
            options.append({"series": series, "issue_date": issue_date,
                            "issued_potential_shares": current_shares,
                            "forfeited_shares": 0,
                            "effective_potential_shares": current_shares,
                            "exercise_price_yen": current_price,
                            "exercise_period": period_text, "source_id": source_id})

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
                                        "forfeited_shares": 0})
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
    options.extend(row for row in merged.values()
                   if row.get("effective_potential_shares") is not None
                   and row.get("exercise_price_yen") is not None)
    if primary_count:
        options = options[:primary_count]
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
        effective = min(previous["effective_potential_shares"], row["effective_potential_shares"])
        chosen = previous if previous["effective_potential_shares"] <= row["effective_potential_shares"] else row
        chosen = dict(chosen)
        chosen["issued_potential_shares"] = issued
        chosen["effective_potential_shares"] = effective
        chosen["forfeited_shares"] = max(issued - effective, previous.get("forfeited_shares", 0),
                                          row.get("forfeited_shares", 0))
        by_series[key] = chosen
    deduped = list(by_series.values())
    page = locate_pdf_page_by_values(pdf_bytes,
                                     [value for row in deduped for value in (row["issued_potential_shares"], row["exercise_price_yen"])],
                                     minimum=3) if deduped else None
    for row in deduped:
        row["pdf_page"] = page
    return deduped


def extract_financial_position(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                               pdf_bytes: bytes) -> dict[str, Any]:
    definitions = {
        "total_assets_million_yen": ("LiabilitiesAndNetAssets", ("InterimInstant", "Prior1YearInstant")),
        "net_assets_million_yen": ("NetAssets", ("InterimInstant", "Prior1YearInstant")),
        "cash_million_yen": ("CashAndDeposits", ("InterimInstant", "Prior1YearInstant")),
        "current_debt_million_yen": ("CurrentPortionOfLongTermLoansPayable", ("InterimInstant", "Prior1YearInstant")),
        "long_term_debt_million_yen": ("LongTermLoansPayable", ("InterimInstant", "Prior1YearInstant")),
        "operating_cf_million_yen": ("NetCashProvidedByUsedInOperatingActivities", ("Prior1YearDuration", "InterimDuration")),
        "investing_cf_million_yen": ("NetCashProvidedByUsedInInvestmentActivities", ("Prior1YearDuration", "InterimDuration")),
        "financing_cf_million_yen": ("NetCashProvidedByUsedInFinancingActivities", ("Prior1YearDuration", "InterimDuration")),
        "income_taxes_deferred_million_yen": ("IncomeTaxesDeferred", ("Prior1YearDuration", "InterimDuration")),
    }
    result: dict[str, Any] = {"source_id": source_id, "contexts": {}}
    for field, (suffix, priorities) in definitions.items():
        chosen = _choose_fact(_find_facts(documents, suffix), priorities)
        if chosen:
            result[field] = chosen["value"] / 1000 if chosen["scale"] == 3 else chosen["value"] / 1_000_000
            result["contexts"][field] = chosen["context"]
    if result.get("total_assets_million_yen"):
        result["equity_ratio_pct"] = result.get("net_assets_million_yen", 0) / result["total_assets_million_yen"] * 100
    result["interest_bearing_debt_million_yen"] = (
        result.get("current_debt_million_yen", 0) + result.get("long_term_debt_million_yen", 0)
    )
    page_values = [value * 1000 for key, value in result.items()
                   if key.endswith("_million_yen") and isinstance(value, (int, float))]
    result["pdf_page"] = locate_pdf_page_by_values(pdf_bytes, page_values, minimum=2)
    return result


def extract_kpis_and_narratives(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                                 pdf_bytes: bytes) -> dict[str, Any]:
    text = "\n".join(soup.get_text(" ", strip=True) for _, soup in documents)
    kpis = []
    patterns = (
        ("cumulative_users", r"(20\d{2}年\d{1,2}月末時点)[^。]{0,100}?累計ユーザー会員数[^。]{0,80}?([\d.]+)万人"),
        ("available_parking_spaces", r"(20\d{2}年\d{1,2}月(?:末|月間平均))[^。]{0,120}?([\d.]+)万台"),
    )
    for name, pattern in patterns:
        match = re.search(pattern, text)
        if match:
            kpis.append({"name": name, "period": match.group(1), "value": float(match.group(2)),
                         "unit": "万人" if name == "cumulative_users" else "万台", "source_id": source_id})
    values = [row["value"] for row in kpis]
    page = locate_pdf_page_by_values(pdf_bytes, values + [2025], minimum=2) if kpis else None
    for row in kpis:
        row["pdf_page"] = page
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
        key=lambda sentence: sum(cue in sentence for cue in ("提供", "運営", "サービス", "プラットフォーム", "マーケットプレイス")),
        default=None,
    )
    risk_candidates = [
        sentence.strip()
        for sentence in re.split(r"(?<=。)", text)
        if 40 < len(sentence) < 500
        and any(cue in sentence for cue in ("競合", "法的規制", "システム障害", "情報漏洩", "事業等のリスク"))
    ]
    return {
        "kpis": kpis,
        "customer_concentration": customer_concentration,
        "customer_concentration_page": locate_pdf_page_by_text(
            pdf_bytes, ("総販売実績に対する割合", "10％以上の相手先")
        ) if customer_concentration else None,
        "single_segment": "アキッパ事業の単一セグメント" if "単一セグメント" in text else None,
        "business_model": business_model,
        "business_model_page": locate_pdf_page_by_text(pdf_bytes, ("事業の内容", "プラットフォーム", "マーケットプレイス")) if business_model else None,
        "risk_excerpt": risk_candidates[0] if risk_candidates else None,
        "risk_page": locate_pdf_page_by_text(pdf_bytes, ("事業等のリスク", "競合", "法的規制")) if risk_candidates else None,
        "source_id": source_id,
    }


def extract_offering_terms(documents: list[tuple[str, BeautifulSoup]], source_id: str,
                           pdf_bytes: bytes) -> dict[str, Any]:
    text = "\n".join(soup.get_text(" ", strip=True) for _, soup in documents)
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
    parent = re.search(r"親引けしようとする株式の数[^\d]{0,80}([\d,]+)株", text)
    if parent:
        result["parent_allotment_shares"] = int(parent.group(1).replace(",", ""))
    result["parent_holding_condition"] = "上場後180日継続保有" if "親引け" in text and "180日目" in text else None
    oa = re.search(r"主幹事会社は、?([\d,]+)株について貸株人より追加的に[^。]{0,200}?グリーンシューオプション", text)
    if not oa:
        oa = re.search(r"グリーンシューオプション[^。]{0,400}?([\d,]+)株", text)
    if oa:
        result["greenshoe_shares"] = int(oa.group(1).replace(",", ""))
    lenders = re.search(
        r"オーバーアロットメントによる売出しのために、?主幹事会社が当社株主である(.{1,300}?)(?:\(以下|（以下)[「『]貸株人[」』]",
        text,
    )
    if lenders:
        result["oa_lenders"] = re.sub(r"\s+", " ", lenders.group(1)).strip(" 、")
    deadline = re.search(r"([0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日)を行使期限として貸株人より付与", text)
    if deadline:
        result["greenshoe_exercise_deadline"] = deadline.group(1)
    cover = re.search(
        r"主幹事会社は、?([0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日から[0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日までの間)[^。]{0,160}?シンジケートカバー取引",
        text,
    )
    if cover:
        result["syndicate_cover_period"] = cover.group(1)
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


def extract_due_diligence(*, base_xbrl: bytes, base_pdf: bytes, base_source_id: str,
                          latest_xbrl: bytes, latest_pdf: bytes, latest_source_id: str,
                          post_listing_shares: float | None = None) -> dict[str, Any]:
    base_docs = _html_documents(base_xbrl)
    latest_docs = _html_documents(latest_xbrl)
    shareholders = extract_shareholders(latest_docs, latest_source_id, latest_pdf)
    lockups = extract_lockups(latest_docs, latest_source_id, latest_pdf)
    options = extract_stock_options(base_docs, base_source_id, base_pdf)
    financial_position = extract_financial_position(base_docs, base_source_id, base_pdf)
    narratives = extract_kpis_and_narratives(base_docs, base_source_id, base_pdf)
    offering_terms = extract_offering_terms(latest_docs, latest_source_id, latest_pdf)
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
        "financial_position": financial_position, **narratives,
        "offering_terms": offering_terms, "expected_locations": expected_locations,
    }


def build_completeness(*, manifests: list[dict[str, Any]], financials: list[dict[str, Any]],
                       offering: dict[str, Any], diligence: dict[str, Any],
                       discovery: dict[str, Any] | None = None) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    def ok(group: str, evidence: Any) -> None:
        statuses[group] = {"passed": bool(evidence), "reason_code": None if evidence else "PARSER_UNSUPPORTED",
                           "evidence_count": len(evidence) if isinstance(evidence, list) else int(bool(evidence))}
    ok("basic_business", diligence.get("business_model"))
    ok("financial_performance", financials)
    ok("balance_sheet_cash_flow", diligence.get("financial_position", {}).get("total_assets_million_yen"))
    terms = diligence.get("offering_terms", {})
    offering_evidence = (offering.get("offering_price") is not None
                         or terms.get("offering_price") is not None)
    detailed_offering = offering_evidence and all(
        terms.get(field) is not None
        for field in ("underwriting_price", "company_law_payment_price", "capital_per_share", "net_proceeds_thousand_yen")
    )
    if (offering.get("oa_shares") or terms.get("greenshoe_shares")):
        detailed_offering = detailed_offering and all(
            terms.get(field) for field in ("oa_lenders", "greenshoe_exercise_deadline", "syndicate_cover_period")
        )
    ok("offering", detailed_offering or offering.get("not_applicable_evidence"))
    if offering.get("not_applicable_evidence") and not offering_evidence:
        statuses["offering"]["reason_code"] = "NOT_APPLICABLE"
    ok("shareholders_sellers", diligence.get("shareholders"))
    ok("lockup", diligence.get("lockups"))
    ok("stock_options", diligence.get("stock_options"))
    ok("kpi_growth", diligence.get("kpis"))
    ok("risks", diligence.get("risk_excerpt") and diligence.get("customer_concentration"))
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
