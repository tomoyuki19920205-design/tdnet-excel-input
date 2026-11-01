"""Reconcile official J-Quants 2Q cumulative guidance with original PDFs.

The job is bounded to recent disclosures and can be rerun after a failed batch.
It never writes actuals or legacy ``financials`` rows. A filing-specific key and
disclosure ordering prevent an older delivery from replacing newer guidance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.pipeline.forecast_sync import ForecastDTO, expand_forecast_rows, select_latest_forecasts
from lib.pipeline.db import get_supabase_write_config, load_env, supabase_upsert

JST = timezone(timedelta(hours=9))
STEMS = {"sales": "Sales", "operating_profit": "OP", "ordinary_profit": "OdP", "net_income": "NP", "eps": "EPS"}
REGIONAL_CACHE_TTL_SECONDS = 24 * 60 * 60
REGIONAL_CACHE_VERSION = 3
NSE_OFFICIAL_DELISTED_CODES = {
    # The detail API still returns a stock object with watchInfo1=False after
    # this 2025-06-27 delisting. NSE's own notice takes precedence.
    # https://www.nse.or.jp/news/2025/0602_10783.html
    "58360",
}


def state_root() -> Path:
    return Path(os.environ.get("TDNET_RUNTIME_STATE_ROOT") or ROOT)


def amount(raw, metric):
    try:
        return (float(raw) if metric == "eps" else float(raw) / 1_000_000) if raw not in (None, "", "-", "—") else None
    except (ValueError, TypeError):
        return None


def pdf_has_amounts(data: bytes, values: dict[str, float]) -> bool:
    if not data.startswith(b"%PDF"):
        return False
    pdf = fitz.open(stream=data, filetype="pdf")
    raw = "\n".join(page.get_text() for page in list(pdf)[:3])
    compact = re.sub(r"[\s,，]", "", unicodedata.normalize("NFKC", raw))
    if len(raw) < 100:
        return False  # image PDF needs separate OCR/visual review
    for value in values.values():
        if value == 0:
            return False
        token = str(int(abs(value))) if value.is_integer() else str(abs(value))
        hits = [m.start() for m in re.finditer(re.escape(token), compact)]
        if not hits:
            return False
        if value < 0 and not any(
            any(mark in compact[max(0, pos - 4):pos] for mark in ("△", "▲", "-", "−", "▼"))
            for pos in hits
        ):
            return False
    return True


def supplement_regional_current_revision(text: str, values: dict[str, float]) -> dict[str, float]:
    """Recover a missing operating-profit field only from a matching B row."""
    if values.get("operating_profit") is not None or values.get("sales") is None:
        return values
    normalized = unicodedata.normalize("NFKC", text)
    pattern = (r"今回修正予想\s*\(?B\)?\s*"
               r"([△▲−-]?[\d,]+)\s+([△▲−-]?[\d,]+)\s+"
               r"([△▲−-]?[\d,]+)\s+([△▲−-]?[\d,]+)\s+([△▲−-]?[\d,.]+)")
    def number(token: str) -> float:
        return float(token.replace(",", "").replace("△", "-").replace("▲", "-").replace("−", "-"))
    for match in re.finditer(pattern, normalized):
        sales, operating, ordinary, net, eps = map(number, match.groups())
        expected = {"sales": sales, "ordinary_profit": ordinary,
                    "net_income": net, "eps": eps}
        if all(values.get(metric) is None or values[metric] == value
               for metric, value in expected.items()):
            return {**values, "operating_profit": operating}
    return values


def regional_scope_from_text(compact: str) -> str:
    consolidated = re.search(r"(?<!非)連結業績予想", compact)
    standalone = re.search(r"個別業績予想|非連結業績予想", compact)
    if consolidated and (not standalone or consolidated.start() < standalone.start()):
        return "consolidated"
    if standalone:
        return "nonconsolidated"
    return "unknown"


def regional_source_url(exchange: str, local_code: str, filing: str, disclosed_date: str) -> str:
    if exchange == "nse":
        return f"https://www.nse.or.jp/listing/search/files/1401{filing}.pdf"
    if exchange == "fse":
        return f"https://www.fse.or.jp/files/lis_tkj/{disclosed_date.replace('-', '')[2:]}{local_code}.pdf"
    return f"https://www2.jpx.co.jp/disc/{local_code}/1401{filing}.pdf"


def nse_current_stock(stock: dict | None) -> bool:
    """Reject known delisted records retained by the NSE detail API."""
    return (bool(stock) and stock.get("watchInfo1") is not True
            and stock.get("stockCode") not in NSE_OFFICIAL_DELISTED_CODES)


def discover_regional_codes(db: Path, since: str, state: Path, session: requests.Session):
    """Resolve only recent, typed 2Q candidates missing from the TSE roster."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    roster_date = conn.execute("SELECT MAX(date) FROM market_data_universe").fetchone()[0]
    tse_codes = {str(row[0]) for row in conn.execute(
        "SELECT code FROM market_data_universe WHERE date=? AND is_ordinary_stock=1", (roster_date,))}
    unknown = set()
    for local_code, payload in conn.execute("""SELECT local_code,raw_json
        FROM jquants_financials_normalized WHERE disclosed_date>=? AND raw_json IS NOT NULL""", (since,)):
        if local_code in tse_codes:
            continue
        raw = json.loads(payload or "{}")
        if any(raw.get(prefix + stem + "2Q") not in (None, "", "-", "—")
               for prefix in ("F", "FNC", "NxF", "NxFNC")
               for stem in ("Sales", "OP", "OdP", "NP", "Np", "EPS")):
            unknown.add(str(local_code))
    conn.close()
    cache_path = state / "2q-regional-market-cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    if cache.get("_version") != REGIONAL_CACHE_VERSION:
        cache = {"_version": REGIONAL_CACHE_VERSION}
    now = time.time()
    fse_codes = {}
    try:
        response = session.get("https://www.fse.or.jp/market-info/listed-companies/", timeout=20)
        response.raise_for_status()
        html = response.content.decode("utf-8")
        fse_codes = dict(re.findall(r'detail\.php\?copid=(\d+)[^>]*>.*?コード[：:]\s*([0-9A-Z]{4})', html, re.S))
        fse_codes = {code + "0": copid for copid, code in fse_codes.items()}
    except Exception as exc:
        print(json.dumps({"regional_fse_registry_error": type(exc).__name__}), flush=True)
    resolved = {}
    for local_code in sorted(unknown):
        cached = cache.get(local_code, {})
        if local_code in fse_codes:
            exchange = "fse"
            bank = bool(cached.get("bank"))
            if not bank:
                try:
                    detail = session.get(
                        "https://www.fse.or.jp/market-info/listed-companies/detail.php?copid=" + fse_codes[local_code],
                        timeout=15)
                    detail.raise_for_status()
                    bank = "銀行業" in detail.content.decode("utf-8")
                except Exception:
                    pass
        elif now - float(cached.get("checked_at", 0)) < REGIONAL_CACHE_TTL_SECONDS:
            exchange, bank = cached.get("exchange", ""), bool(cached.get("bank"))
        else:
            try:
                response = session.get(
                    f"https://www.nse.or.jp/api/stock/view.json?stockCode={local_code}", timeout=15)
                response.raise_for_status()
                stock = (response.json().get("stock") or [None])[0]
                exchange = "nse" if nse_current_stock(stock) else ""
                bank = bool(exchange and stock.get("industryName_j") == "銀行業")
                time.sleep(0.15)
            except Exception as exc:
                print(json.dumps({"regional_nse_lookup_error": local_code,
                    "reason": type(exc).__name__}), flush=True)
                continue
        cache[local_code] = {"checked_at": now, "exchange": exchange, "bank": bank}
        if exchange:
            resolved[local_code] = (local_code[:-1], exchange, bank)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps({"regional_candidates": len(unknown), "regional_verified": len(resolved),
        "nse": sum(item[1] == "nse" for item in resolved.values()),
        "fse": sum(item[1] == "fse" for item in resolved.values())}), flush=True)
    return resolved


def select_recent(db: Path, since: str, regional_map=None):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    universe_date = conn.execute("SELECT MAX(date) FROM market_data_universe").fetchone()[0]
    official = {str(r["code"]): str(r["ticker"]) for r in conn.execute(
        "SELECT code,ticker FROM market_data_universe WHERE date=? AND is_ordinary_stock=1", (universe_date,)
    )}
    regional_map = regional_map or {}
    official.update({code: item[0] for code, item in regional_map.items()})
    metadata = {str(r["disclosure_id"]): str(r["title"] or "") for r in conn.execute(
        "SELECT disclosure_id,title FROM jquants_tdnet_metadata WHERE disclosed_date>=?", (since,)
    )}
    actual_fy = {}
    actual_2q = set()
    latest_scope = {}
    for row in conn.execute("""SELECT local_code,current_fiscal_year_end_date,type_of_current_period,
        type_of_document,disclosed_date
        FROM jquants_financials_normalized WHERE type_of_document LIKE '%FinancialStatements%'"""):
        ticker = official.get(str(row["local_code"]))
        if not ticker:
            continue
        key = (ticker, row["current_fiscal_year_end_date"])
        doc_type = str(row["type_of_document"])
        scope = ("nonconsolidated" if "NonConsolidated" in doc_type else
                 "consolidated" if "Consolidated" in doc_type else "unknown")
        if scope != "unknown" and row["disclosed_date"] >= latest_scope.get(key, ("", ""))[0]:
            latest_scope[key] = (row["disclosed_date"], scope)
        if row["type_of_current_period"] == "FY":
            actual_fy[ticker] = max(actual_fy.get(ticker, ""), key[1])
        elif row["type_of_current_period"] == "2Q":
            actual_2q.add(key)
    disclosures = []
    blank_revisions = defaultdict(list)
    status_updates = []
    for row in conn.execute("""SELECT local_code,current_fiscal_year_end_date,
        type_of_document,disclosed_date,raw_json FROM jquants_financials_normalized
        WHERE disclosed_date>=? AND raw_json IS NOT NULL ORDER BY disclosed_date""", (since,)):
        ticker = official.get(str(row["local_code"]))
        if not ticker:
            continue
        raw = json.loads(row["raw_json"] or "{}")
        doc_type = str(row["type_of_document"] or "")
        filing = str(raw.get("DiscNo") or "")
        if not filing:
            continue
        for prefix, period in (("F", raw.get("CurFYEn") or row["current_fiscal_year_end_date"]),
                               ("NxF", raw.get("NxtFYEn"))):
            if not period or period <= actual_fy.get(ticker, "") or (ticker, period) in actual_2q:
                continue
            values = {metric: amount(raw.get(prefix + ("Np" if prefix == "NxF" and metric == "net_income" else stem) + "2Q"), metric)
                      for metric, stem in STEMS.items()}
            exchange, bank = regional_map.get(str(row["local_code"]), (None, "", False))[1:]
            if exchange and not any(v is not None for v in values.values()):
                nc_prefix = "FNC" if prefix == "F" else "NxFNC"
                values = {metric: amount(raw.get(nc_prefix + stem + "2Q"), metric)
                          for metric, stem in STEMS.items()}
            if bank:
                values["sales"] = None  # 銀行の経常収益は売上高として表示しない。
            if not any(v is not None for v in values.values()):
                if "ForecastRevision" in doc_type:
                    blank_revisions[(ticker, period)].append(row["disclosed_date"])
                    title = metadata.get(filing, "")
                    if re.search(r"第?[２2]四半期|中間|上期", title):
                        status_updates.append({
                            "ticker": ticker, "period": period,
                            "status": "withdrawn" if re.search(r"撤回|取り下げ", title) else "unresolved",
                            "disclosure_datetime": row["disclosed_date"] + "T" + str(raw.get("DiscTime") or "00:00:00") + "+09:00",
                            "source_url": regional_source_url(exchange, row["local_code"], filing, row["disclosed_date"]),
                            "reason": "later upper-half revision has no structured 2Q values; original review pending",
                        })
                continue
            scope = ("nonconsolidated" if "NonConsolidated" in doc_type else
                     "consolidated" if "Consolidated" in doc_type else "unknown")
            if scope != "unknown" and row["disclosed_date"] >= latest_scope.get((ticker, period), ("", ""))[0]:
                latest_scope[(ticker, period)] = (row["disclosed_date"], scope)
            disclosed = row["disclosed_date"] + "T" + str(raw.get("DiscTime") or "00:00:00")
            url = regional_source_url(exchange, row["local_code"], filing, row["disclosed_date"])
            disclosures.append((ticker, period, scope, disclosed, filing, url, doc_type, exchange,
                                {m: v for m, v in values.items() if v is not None}))
    conn.close()
    return disclosures, {key: value[1] for key, value in latest_scope.items()}, blank_revisions, status_updates, universe_date


def run(days: int, apply: bool, limit: int):
    state = state_root()
    db = state / "data/jquants.db"
    if not db.is_file():
        raise FileNotFoundError(db)
    since = (datetime.now(JST) - timedelta(days=days)).date().isoformat()
    session = requests.Session()
    regional_map = discover_regional_codes(db, since, state, session)
    disclosures, scopes, blanks, status_updates, universe_date = select_recent(db, since, regional_map)
    cache = state / "2q-pdf-cache"
    cache.mkdir(parents=True, exist_ok=True)
    candidates = []
    rejected = []
    for ticker, period, scope, disclosed, filing, url, doc_type, exchange, values in disclosures[:limit or None]:
        if any(date > disclosed[:10] for date in blanks.get((ticker, period), [])):
            rejected.append((ticker, filing, "later_blank_revision"))
            continue
        scope = scope if scope != "unknown" else scopes.get((ticker, period), "unknown")
        path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")
        try:
            if path.is_file() and path.stat().st_size > 1000:
                data = path.read_bytes()
            else:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                data = response.content
                if data.startswith(b"%PDF"):
                    path.write_bytes(data)
            # J-Quants supplies 2Q EPS and exact zeros as typed source fields.
            # A PDF-wide match for "0" is ambiguous; verify at least one other
            # nonzero amount in the same official PDF before carrying them.
            evidence_values = {metric: value for metric, value in values.items()
                               if metric != "eps" and value != 0}
            if not evidence_values or not pdf_has_amounts(data, evidence_values):
                rejected.append((ticker, filing, "pdf_value_or_table_unverified"))
                continue
            if exchange:
                first_page = unicodedata.normalize("NFKC", fitz.open(stream=data, filetype="pdf")[0].get_text())
                compact = re.sub(r"\s+", "", first_page)
                fy = f"{period[:4]}年{int(period[5:7])}月期"
                if fy not in compact or not re.search(r"第?2四半期|中間期|上期", compact):
                    rejected.append((ticker, filing, "regional_period_unverified"))
                    continue
                if scope == "unknown":
                    scope = regional_scope_from_text(compact)
                if not regional_map.get(ticker + "0", (None, None, False))[2]:
                    values = supplement_regional_current_revision(first_page, values)
        except Exception as exc:
            rejected.append((ticker, filing, type(exc).__name__))
            continue
        if scope == "unknown":
            rejected.append((ticker, filing, "scope_unknown"))
            continue
        sha = hashlib.sha256(data).hexdigest()
        for metric, value in values.items():
            candidates.append(ForecastDTO(ticker=ticker, forecast_period_end=period,
                quarter="2Q", metric=metric, value=value,
                disclosure_datetime=disclosed, filing_id=filing,
                source="jquants_forecast", correction_flag=False,
                forecast_horizon="current_fy", accounting_standard="UNKNOWN",
                document_type="forecast_revision" if "ForecastRevision" in doc_type else "earnings_guidance",
                accounting_scope=scope, source_url=url, source_sha256=sha))
    rows = expand_forecast_rows(select_latest_forecasts(candidates))
    latest_status = {}
    for item in status_updates:
        key = item["ticker"], item["period"]
        if key not in latest_status or item["disclosure_datetime"] > latest_status[key]["disclosure_datetime"]:
            latest_status[key] = item
    status_updates = list(latest_status.values())
    print(json.dumps({"since": since, "universe_date": universe_date,
        "disclosures": len(disclosures), "verified_metrics": len(rows),
        "rejected": len(rejected), "rejection_examples": rejected[:10],
        "status_updates": len(status_updates), "apply": apply},
        ensure_ascii=False), flush=True)
    if apply and (rows or status_updates):
        load_env()
        config = get_supabase_write_config()
        if not config:
            raise RuntimeError("Supabase write config unavailable")
        tickers = sorted({row["ticker"] for row in rows})
        published = set()
        for offset in range(0, len(tickers), 80):
            response = session.get(
                config["rest_url"] + "/api_latest_financials_canonical",
                params={"select": "ticker,period", "quarter": "eq.2Q",
                        "ticker": "in.(" + ",".join(tickers[offset:offset+80]) + ")"},
                headers=config["headers"], timeout=30,
            )
            response.raise_for_status()
            published.update((str(item["ticker"]), str(item["period"])) for item in response.json())
        rows = [row for row in rows if (row["ticker"], row["period"]) not in published]
        status_updates = [item for item in status_updates
                          if (item["ticker"], item["period"]) not in published]
        print(json.dumps({"actual_published_keys": len(published),
            "eligible_after_actual_guard": len(rows)}), flush=True)
        for offset in range(0, len(rows), 100):
            result = supabase_upsert("canonical_financials", rows[offset:offset+100],
                on_conflict="source_row_key", config=config)
            if result.get("errors"):
                raise RuntimeError(f"2Q forecast batch {offset//100+1} failed: {result}")
            print(json.dumps({"batch": offset//100+1, "written": len(rows[offset:offset+100])}), flush=True)
        if status_updates:
            current_status = {}
            status_tickers = sorted({item["ticker"] for item in status_updates})
            for offset in range(0, len(status_tickers), 80):
                response = session.get(
                    config["rest_url"] + "/company_2q_forecast_status",
                    params={"select": "ticker,period,disclosure_datetime",
                            "ticker": "in.(" + ",".join(status_tickers[offset:offset+80]) + ")"},
                    headers=config["headers"], timeout=30,
                )
                response.raise_for_status()
                current_status.update({(item["ticker"], item["period"]): item["disclosure_datetime"]
                    for item in response.json()})
            status_updates = [item for item in status_updates if
                not current_status.get((item["ticker"], item["period"])) or
                datetime.fromisoformat(item["disclosure_datetime"]) >=
                datetime.fromisoformat(current_status[(item["ticker"], item["period"])].replace("Z", "+00:00"))]
        if status_updates:
            response = session.post(
                config["rest_url"] + "/company_2q_forecast_status",
                params={"on_conflict": "ticker,period"},
                headers={**config["headers"], "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=status_updates, timeout=30,
            )
            response.raise_for_status()
            print(json.dumps({"status_rows": len(status_updates)}), flush=True)
    session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.days, args.apply, args.limit)
