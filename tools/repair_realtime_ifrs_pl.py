#!/usr/bin/env python3
"""Bounded repair for the 2026-07-31 IFRS realtime PL alias incident.

Only the two manifest rows below may be changed.  The script uses the normal
XBRL extractor, canonical writer, and guarded event partial-update API; it
never sends a Discord notification.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.pipeline.db import load_env
from src.events.earnings_production_pipeline import _sync_canonical_financials
from src.events.summary_financials import extract_earnings_data
from src.events.tdnet_event_store import _get_supabase, update_tdnet_event_fields_by_identity


TARGETS = (
    {
        "ticker": "6268", "period": "2026-12-31", "quarter": "2Q",
        "filing_id": "4286b976286b6fc5f45d9c986f05a10af10ce9617927a46677e2fd953129d42b",
        "zip_path": "data/tdnet_cache/20260731505000/xbrl.zip",
        "title": "2026年12月期第2四半期（中間期）決算短信〔IFRS〕（連結）",
        "event_id": "35c928ca-5ea6-42fb-ac5b-f7f581726e46",
        "disclosed_at": "2026-07-31T07:00:00+00:00",
        "dedupe_key": "204aa661129d457330eca7e3aa9b77ed4c86a7db",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260731505000.pdf",
    },
    {
        "ticker": "6503", "period": "2027-03-31", "quarter": "1Q",
        "filing_id": "f997b092da3fc256817a5707765fcf4044b051adc8dfc57a984e164edb251c4d",
        "zip_path": "data/tdnet_cache/20260721596543/xbrl.zip",
        "title": "2027年3月期第1四半期決算短信〔IFRS〕（連結）",
        "event_id": "a028e2a3-c561-4ce6-a730-271565ab73ef",
        "disclosed_at": "2026-07-31T06:30:00+00:00",
        "dedupe_key": "f95c50dbbbb5ede2a02e767f4777574e98702557",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260721596543.pdf",
    },
    {
        "ticker": "6758", "period": "2027-03-31", "quarter": "1Q",
        "filing_id": "c45448eb968911c909e1b34316aa7266379d38334f745c998e8cf22b086a0ec3",
        "zip_path": "data/tdnet_cache/20260730504252/xbrl.zip",
        "title": "2027年３月期 第１四半期決算短信〔IFRS〕（連結）",
        "event_id": "02bbb9a6-0b84-41c2-a842-3a842cae22c6",
        "disclosed_at": "2026-07-31T03:00:00+00:00",
        "dedupe_key": "51abd1036bac0bf680bf29ca2f4a3a29208ecc62",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260730504252.pdf",
    },
    {
        "ticker": "6762", "period": "2027-03-31", "quarter": "1Q",
        "filing_id": "52e5b86f47918a0d691b404da1e422ab6d853a15a83469c5a4bff7cb8426d912",
        "zip_path": "data/tdnet_cache/20260730503470/xbrl.zip",
        "title": "2027年3月期 第1四半期決算短信〔IFRS〕(連結)",
        "event_id": "0ac8bad0-e558-4998-a435-166311c9a2bc",
        "disclosed_at": "2026-07-31T06:30:00+00:00",
        "dedupe_key": "5a68898e769864ad553f98a39386ae1e741c9e76",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260730503470.pdf",
    },
)


def _updated_message(existing: str, pl_line: str) -> str:
    first, sep, remainder = existing.partition("\n")
    return f"{first}\n{pl_line}{sep}{remainder}" if sep else f"{existing}\n{pl_line}"


def repair(*, apply: bool, tickers: set[str] | None = None) -> list[dict]:
    load_env(".")
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase client is unavailable")
    results = []
    for target in TARGETS:
        if tickers is not None and target["ticker"] not in tickers:
            continue
        earnings = extract_earnings_data(
            xbrl_path=target["zip_path"], title=target["title"], ticker=target["ticker"],
        )
        if not earnings or earnings.sales_current is None or earnings.op_current is None:
            raise RuntimeError(f"{target['ticker']}: XBRL PL extraction remained empty")

        existing = client.table("tdnet_events").select("raw_payload,formatted_message") \
            .eq("id", target["event_id"]).eq("ticker", target["ticker"]) \
            .eq("disclosed_at", target["disclosed_at"]).eq("dedupe_key", target["dedupe_key"]) \
            .eq("pdf_url", target["pdf_url"]).limit(2).execute().data
        if len(existing) != 1:
            raise RuntimeError(f"{target['ticker']}: event identity matched {len(existing)} rows")

        payload = existing[0]["raw_payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        extracted = payload.setdefault("extracted", {})
        extracted.update({
            "sales_current": earnings.sales_current,
            "sales_label": "売上高",
            "sales_yoy": earnings.sales_yoy,
            "op_current": earnings.op_current,
            "op_label": "営業利益",
            "op_source": "xbrl",
            "op_yoy": earnings.op_yoy,
            "gross_profit_value": earnings.gross_profit_current,
            "selling_general_and_administrative_expenses_value": earnings.selling_general_and_administrative_expenses_current,
            "has_yoy": earnings.has_yoy,
        })
        payload["text_extract_status"] = "ok"
        payload["text_empty"] = False
        payload.setdefault("notification_compare_json", {}).setdefault("current", {}).update({
            "label": target["quarter"], "sales_yoy": earnings.sales_yoy, "op_yoy": earnings.op_yoy,
        })
        pl_line = earnings.format_summary_line()
        message = _updated_message(existing[0].get("formatted_message") or "", pl_line)

        if apply:
            _sync_canonical_financials(
                ticker=target["ticker"], period=target["period"], quarter=target["quarter"],
                sales_value=earnings.sales_current, op_value=earnings.op_current,
                gross_value=earnings.gross_profit_current,
                sga_value=earnings.selling_general_and_administrative_expenses_current,
                guidance=extracted.get("guidance") or {}, filing_id=target["filing_id"],
                dry_run=False, route="ifrs_alias_incident_repair",
            )
        update = update_tdnet_event_fields_by_identity(
            client, id=target["event_id"], ticker=target["ticker"],
            disclosed_at=target["disclosed_at"], dedupe_key=target["dedupe_key"], pdf_url=target["pdf_url"],
            updates={
                "raw_payload": json.dumps(payload, ensure_ascii=False),
                "primary_metric_value": f"{earnings.op_current / 1_000_000:,.0f}百万円",
                "primary_metric_yoy": f"{earnings.op_yoy:+.1%}" if earnings.op_yoy is not None else None,
                "display_summary": message,
                "formatted_message": message,
            }, dry_run=not apply,
        )
        results.append({"ticker": target["ticker"], "sales": earnings.sales_current, "op": earnings.op_current, "update": update})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Validate the two manifest rows without writing.")
    mode.add_argument("--apply", action="store_true", help="Apply the validated, bounded repair.")
    parser.add_argument("--tickers", nargs="+", choices=[target["ticker"] for target in TARGETS])
    args = parser.parse_args()
    print(json.dumps(repair(apply=args.apply, tickers=set(args.tickers) if args.tickers else None), ensure_ascii=False, default=str, indent=2))
