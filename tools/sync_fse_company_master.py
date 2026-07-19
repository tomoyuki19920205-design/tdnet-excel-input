#!/usr/bin/env python3
"""福証公式の単独上場会社情報を companies に安全に同期する。"""
from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from lib.pipeline.db import get_supabase_read_config, get_supabase_write_config, load_env
from src.common_ticker import normalize_ticker

PRIMARY_URL = "https://www.fse.or.jp/listed/single.php"
SECONDARY_URL = "https://www.fse.or.jp/listed/list.php"
REQUIRED = {"1771": "日本乾溜工業", "1999": "サイタホールディングス", "2058": "ヒガシマル", "4995": "サンケイ化学", "7894": "丸東産業", "3824": "メディアファイブ"}
CODE_RE = re.compile(r"^[0-9]{3}[0-9A-Z]$")


class ValidationError(RuntimeError):
    pass


class UpsertError(RuntimeError):
    """Supabaseの秘密情報を含めずにPostgRESTエラーを伝える。"""


@dataclass(frozen=True)
class Company:
    ticker_code: str
    name_ja: str
    market: str
    source_url: str


def clean_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value or ""))
    return " ".join(value.replace("\u3000", " ").split())


def fetch(url: str, session: requests.Session) -> tuple[str, dict[str, Any]]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=30)
            content_type = response.headers.get("content-type", "")
            if response.status_code != 200 or "html" not in content_type.lower() or not response.content:
                raise ValidationError(f"invalid HTTP response for {url}: {response.status_code} {content_type}")
            return response.text, {"url": url, "status": response.status_code, "content_type": content_type, "bytes": len(response.content), "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified"), "sha256": hashlib.sha256(response.content).hexdigest()}
        except (requests.RequestException, ValidationError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ValidationError(str(last))


def primary_companies(html: str) -> list[Company]:
    soup = BeautifulSoup(html, "html.parser")
    companies: list[Company] = []
    for table in soup.find_all("table"):
        values: dict[str, str] = {}
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            for index in range(0, len(cells) - 1, 2):
                values[clean_text(cells[index].get_text(" ", strip=True))] = clean_text(cells[index + 1].get_text(" ", strip=True))
        code = clean_text(values.get("コード"))
        market = clean_text(values.get("市場区分"))
        name_cell = table.find("th", string=lambda x: clean_text(x) == "会社名")
        image = name_cell.find_next("img") if name_cell else None
        name = clean_text(image.get("alt") if image else "")
        if not any((code, market, name)):
            continue
        code = normalize_ticker(code)
        if not CODE_RE.fullmatch(code) or not name or not market:
            raise ValidationError("primary table has invalid code, company name, or market")
        companies.append(Company(code, name, market, PRIMARY_URL))
    validate_companies(companies)
    return companies


def secondary_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, str] = {}
    for a in soup.select("ul.list_listed_company a[href]"):
        name = clean_text(a.get_text(" ", strip=True))
        if name:
            if name in links:
                raise ValidationError(f"duplicate secondary name: {name}")
            links[name] = urljoin(SECONDARY_URL, a["href"])
    if not links:
        raise ValidationError("secondary company list was not found")
    return links


def detail_code(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [clean_text(x.get_text(" ", strip=True)) for x in rows[0].find_all("th")]
        values = [clean_text(x.get_text(" ", strip=True)) for x in rows[1].find_all("td")]
        if headers and headers[0] == "コード" and values:
            code = normalize_ticker(values[0])
            if CODE_RE.fullmatch(code):
                return code
    raise ValidationError("secondary detail code was not found")


def validate_companies(companies: list[Company]) -> None:
    if len(companies) < 10:
        raise ValidationError("fewer than 10 primary companies")
    codes = [item.ticker_code for item in companies]
    if len(codes) != len(set(codes)):
        raise ValidationError("duplicate primary ticker")
    current = {item.ticker_code: item.name_ja for item in companies}
    if any(current.get(code) != name for code, name in REQUIRED.items()):
        raise ValidationError("required FSE companies are absent or unexpected")


def fetch_and_validate() -> tuple[list[Company], dict[str, Any]]:
    session = requests.Session()
    session.headers["User-Agent"] = "tdnet-company-master-sync/1.0"
    primary_html, primary_meta = fetch(PRIMARY_URL, session)
    secondary_html, secondary_meta = fetch(SECONDARY_URL, session)
    companies = primary_companies(primary_html)
    links = secondary_links(secondary_html)
    detail_meta: list[dict[str, Any]] = []
    for company in companies:
        detail_url = links.get(company.name_ja)
        if not detail_url:
            raise ValidationError(f"secondary list lacks {company.name_ja}")
        detail_html, meta = fetch(detail_url, session)
        if detail_code(detail_html) != company.ticker_code:
            raise ValidationError(f"secondary detail code mismatch for {company.ticker_code}")
        detail_meta.append(meta)
    return companies, {"fetched_at": datetime.now(timezone.utc).isoformat(), "primary": primary_meta, "secondary": secondary_meta, "secondary_detail_count": len(detail_meta)}


def get_companies(config: dict[str, str]) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = requests.get(config["rest_url"] + "/companies", headers={**config["headers"], "Range": f"{offset}-{offset + 999}"}, params={"select": "ticker_code,name_ja,name_en,is_active", "order": "ticker_code"}, timeout=30)
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return {str(row["ticker_code"]): row for row in rows}


def viewer_data_exists(config: dict[str, str], ticker: str) -> bool:
    response = requests.get(config["rest_url"] + "/api_latest_financials_canonical", headers=config["headers"], params={"select": "ticker", "ticker": f"eq.{ticker}", "limit": "1"}, timeout=30)
    response.raise_for_status()
    return bool(response.json())


def plan(companies: list[Company], existing: dict[str, dict[str, Any]], read_config: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    changes: list[dict[str, Any]] = []
    stats = {"insert": 0, "name_ja_update": 0, "is_active_update": 0, "unchanged": 0, "skip": 0}
    for item in companies:
        if item.market == "Fukuoka PRO Market" and not viewer_data_exists(read_config, item.ticker_code):
            stats["skip"] += 1
            continue
        old = existing.get(item.ticker_code)
        # PostgREST の一括 upsert は全行を同一JSONスキーマで送る。
        payload: dict[str, Any] = {"ticker_code": item.ticker_code, "name_ja": item.name_ja, "is_active": True}
        if old is None:
            payload.update({"name_ja": item.name_ja, "is_active": True})
            stats["insert"] += 1
        else:
            if clean_text(old.get("name_ja")) != item.name_ja:
                stats["name_ja_update"] += 1
            if old.get("is_active") is False:
                stats["is_active_update"] += 1
            if clean_text(old.get("name_ja")) == item.name_ja and old.get("is_active") is not False:
                stats["unchanged"] += 1
                continue
        changes.append(payload)
    return changes, stats


def validate_payload(changes: list[dict[str, Any]]) -> None:
    expected = {"ticker_code", "name_ja", "is_active"}
    tickers: set[str] = set()
    for row in changes:
        if set(row) != expected or not isinstance(row["ticker_code"], str) or not isinstance(row["name_ja"], str) or not row["name_ja"] or not isinstance(row["is_active"], bool):
            raise ValidationError("invalid homogeneous companies upsert payload")
        if row["ticker_code"] in tickers:
            raise ValidationError("duplicate ticker in companies upsert payload")
        tickers.add(row["ticker_code"])


def apply_changes(changes: list[dict[str, Any]], config: dict[str, str]) -> None:
    if not changes:
        return
    validate_payload(changes)
    response = requests.post(config["rest_url"] + "/companies", headers={**config["headers"], "Prefer": "resolution=merge-duplicates,return=representation"}, params={"on_conflict": "ticker_code"}, json=changes, timeout=60)
    if not response.ok:
        try:
            body = response.json()
            safe = {key: str(body.get(key, ""))[:500] for key in ("code", "message", "details", "hint")}
        except ValueError:
            safe = {"body_type": "non_json", "body_length": len(response.text)}
        raise UpsertError(f"companies upsert failed: status={response.status_code} rows={len(changes)} keys=ticker_code,name_ja,is_active error={json.dumps(safe, ensure_ascii=False)}")
    if len(response.json()) != len(changes):
        raise RuntimeError("companies upsert returned an unexpected row count")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="福証単独上場会社を companies に同期")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--output-json", action="store_true")
    args = parser.parse_args(argv)
    load_env()
    read_config = get_supabase_read_config()
    if not read_config["url"] or not read_config["key"]:
        raise RuntimeError("Supabase read configuration is missing")
    master, source = fetch_and_validate()
    existing = get_companies(read_config)
    changes, stats = plan(master, existing, read_config)
    validate_payload(changes)
    report = {"source": source, "markets": {market: sum(x.market == market for x in master) for market in sorted({x.market for x in master})}, "official_count": len(master), "changes": changes, "stats": stats}
    if args.apply:
        write_config = get_supabase_write_config()
        if not write_config:
            raise RuntimeError("Supabase service-role write configuration is missing")
        apply_changes(changes, write_config)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.output_json else json.dumps({"official_count": len(master), "stats": stats, "change_count": len(changes)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
