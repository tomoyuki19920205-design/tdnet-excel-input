from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from lib.ny_market_research import attach_market_data_packet_metadata
from lib.ny_market_display import apply_display_contract


TOP20 = (
    ("SSM", "Sono Group N.V.", 77.425, 6_775_086),
    ("FLYE", "Fly-E Group", 61.765, 3_591_249),
    ("BIAF", "bioAffinity Technologies", 44.518, 3_903_738),
    ("RDAC", "Rising Dragon Acquisition", 41.447, None),
    ("GPRO", "GoPro", 40.379, 226_940_423),
    ("NWGL", "CL Workshop Group", 38.807, 34_046_735),
    ("SWVL", "Swvl Holdings", 31.615, 110_964_196),
    ("PETZ", "TDH Holdings", 28.696, 15_278_437),
    ("FRVO", "Fervo Energy", 28.414, 5_821_573_180),
    ("LIDR", "AEye", 27.826, 68_337_819),
    ("SST", "System1", 26.786, 29_787_763),
    ("FBLG", "FibroBiologics", 22.674, 14_048_787),
    ("GWAV", "Greenwave Technology Solutions", 19.388, 4_853_341),
    ("RDIB", "Reading International Class B", 17.219, 71_000_000),
    ("MOVE", "Corvex", 16.986, 354_161_977),
    ("PXS", "Pyxis Tankers", 16.231, 64_014_116),
    ("NVNI", "Nvni Group", 16.214, 11_405_676),
    ("GYGY", "Game Your Game", 15.966, 19_860_960),
    ("PTN", "Palatin Technologies", 15.516, 20_532_834),
    ("PSIG", "PS International Group", 15.337, 28_915_945),
)

SECTORS = (
    ("XLE", "Energy", 1.266), ("XLU", "Utilities", 0.781),
    ("XLV", "Health Care", 0.663), ("XLP", "Consumer Staples", 0.318),
    ("XLRE", "Real Estate", -0.159), ("XLC", "Communication Services", -0.520),
    ("XLF", "Financials", -0.884), ("XLB", "Materials", -1.177),
    ("XLI", "Industrials", -1.370), ("XLK", "Information Technology", -1.534),
    ("XLY", "Consumer Discretionary", -1.715),
)

CATALYSTS = {
    "SSM": "Sports OneとのLOIを発表",
    "FLYE": "四半期決算を発表",
    "BIAF": "CyPath Lungの臨床利用拡大を発表",
    "GPRO": "Starman Opticalとの合併契約を発表",
    "NWGL": "新規顧客契約を発表",
    "SWVL": "UAEの運転資金融資枠を確保",
    "PETZ": "CEOが自己資金で約297万株を取得",
    "FRVO": "Googleと396MWの地熱電力購入契約を発表",
    "LIDR": "Apollo LiDARの月面車採用契約を発表",
    "SST": "MapQuestがApp Store無料アプリ首位",
    "PXS": "タンカー運賃上昇と原油供給懸念が追い風",
}


def _point(symbol: str, close: float, change_pct: float, **extra):
    previous = close / (1.0 + change_pct / 100.0)
    return {"symbol": symbol, "close": close, "previous_close": previous, "change_pct": change_pct, **extra}


def _research(ticker: str, company: str, change: float, cap: float | None, rank: int):
    close = float(rank + 1)
    catalyst = CATALYSTS.get(ticker)
    verified = catalyst is not None
    if cap is None:
        method = "unavailable"
        components = []
    elif ticker == "RDIB":
        method = "issuer_total_dual_class"
        components = [
            {"class": "Class A", "price": 2.0, "shares_outstanding": 30_000_000},
            {"class": "Class B", "price": 11.0, "shares_outstanding": 1_000_000},
        ]
    else:
        method = "issuer_total_single_class"
        components = [{"class": "common", "price": close, "shares_outstanding": cap / close}]
    return {
        "ticker": ticker, "company_name": company,
        "company_description": f"{company}の事業・製品・顧客基盤を持つ企業。",
        "close": close, "change_pct": change,
        "market_cap": cap, "market_cap_method": method,
        "catalyst": catalyst or "今回の検索範囲では株価変動を説明できる大型当日材料を検索したが確認できず",
        "catalyst_type": "company_event" if verified else "not_found",
        "source_url": f"https://example.com/catalyst/{ticker.lower()}" if verified else None,
        "source_type": "company_press_release" if verified else None,
        "search_status": "verified_catalyst" if verified else "searched_not_found",
        "searched_at": "2026-09-02T07:20:00+09:00", "share_class_components": components,
        "search_attempt_count": 2,
        "search_queries": [
            f'"{ticker}" "{company}" 2026-09-01 news catalyst',
            f'"{company}" investor relations 2026-09-01 press release',
        ],
        "searched_sources": ["https://www.google.com/search", "https://www.sec.gov/edgar/search/"],
    }


def payload() -> dict:
    market_url = "https://example.com/market/2026-09-01"
    research = [_research(*row, rank) for rank, row in enumerate(TOP20, start=1)]
    for rank, (ticker, company, change, cap) in enumerate((
        ("MDT", "Medtronic", 3.0, 120_000_000_000),
        ("DELL", "Dell Technologies", -6.8, 80_000_000_000),
    ), start=21):
        item = _research(ticker, company, change, cap, rank)
        item.update({
            "catalyst": "四半期決算とガイダンスを発表", "catalyst_type": "earnings",
            "source_url": f"https://example.com/ir/{ticker.lower()}", "source_type": "company_ir",
            "search_status": "verified_catalyst",
        })
        if ticker == "MDT":
            item["company_description"] = "医療機器を世界で展開する企業。"
        research.append(item)
    research_by_ticker = {item["ticker"]: item for item in research}
    top = [{"rank": rank, **deepcopy(research_by_ticker[row[0]])} for rank, row in enumerate(TOP20, start=1)]
    indexes = [
        _point("SOX", 11288.6123, -2.136), _point("S&P500", 7631.47, -0.711),
        _point("Dow", 52766.88, -0.788), _point("Nasdaq", 26099.77, -1.028),
        _point("Russell 2000", 2920.13, -1.229),
    ]
    sectors = [
        _point(symbol, 100.0 * (1 + change / 100), change, rank=rank, sector=name)
        for rank, (symbol, name, change) in enumerate(SECTORS, start=1)
    ]
    report = ("# NY市場モーニングレポート 2026-09-02\n\n"
        "## 要点\n\n日本株への含意を含む検証済み要点。\n\n"
        "## 5指数\n\n下書き。\n\n"
        "## 11 Sector SPDR（騰落率降順）\n\n下書き。\n\n"
        "## 話題の値上がり10社\n\n下書き。\n\n"
        "## 話題の値下がり10社\n\n下落銘柄の説明。\n\n"
        "## 純粋上昇率Top20\n\n下書き。\n\n"
        "## アフター決算\n\n下書き。\n\n"
        "## 主要決算\n\n" + (
        "5指数と11セクターの通常取引終値を同一基準で検証した。個別企業は一次情報を優先し、"
        "確認済み事実と推論を分離した。原油、金利、Fed、AI設備投資、半導体、ネットワーク、"
        "メモリ、データセンター電力・冷却、消費、信用、コモディティを接続し、日本株への含意を考察する。\n\n"
    ) * 8).strip()
    news = [{
        "title": f"独立ニュース {i}", "summary": "市場に影響する新規情報を確認",
        "market_impact": "複数資産への波及を検討", "event_cluster": f"cluster-{i}",
        "source_url": f"https://example.com/news/{i}", "source_type": "reuters",
        "covered_elsewhere": False, "market_wide_exception": False,
    } for i in range(10)]
    earning = {
        **deepcopy(research_by_ticker["MDT"]),
        "revenue": "$x", "eps": "$x", "guidance": "維持", "key_kpis": ["organic growth"],
        "one_offs": ["none"], "price_reaction": "+x%", "why_stock_moved": "成長率が期待を上回ったため。",
        "forward_implication": "医療機器需要の底堅さを示す。",
    }
    def rich_after_hours(base, display_name, change, revenue, eps, price_url, theme):
        primary_url = base["source_url"]
        return {
            **base,
            "event_type": "earnings",
            "after_hours_change_pct": change,
            "as_of_utc": "2026-09-01T22:47:00Z",
            "as_of_jst": "2026-09-02T07:47:00+09:00",
            "session": "post_market",
            "display_company_name": display_name,
            "reported_results": [{
                "summary": f"四半期売上は{revenue}となり、主力事業の需要が前年同期から拡大しました。",
                "evidence_tokens": [revenue, "主力事業"], "source_url": primary_url,
            }],
            "consensus_comparison": [{
                "summary": f"調整後EPSは{eps}と市場予想$1.40を上回りました。",
                "actual": eps, "estimate": "$1.40", "outcome": "beat", "source_url": price_url,
            }],
            "guidance": [{
                "summary": "会社は次四半期も増収と利益率改善を見込む通期見通しを示しました。",
                "evidence_tokens": ["増収", "利益率改善"], "source_url": primary_url,
            }],
            "guidance_comparison": [],
            "key_kpis": [{
                "summary": f"{theme}の受注残と継続収益が伸び、成長の再現性を支えました。",
                "evidence_tokens": [theme, "受注残"], "source_url": primary_url,
            }],
            "background_context": [],
            "same_day_developments": [],
            "after_hours_reaction": {"change_pct": change, "source_url": price_url},
            "why_moved": f"{theme}の受注残拡大とEPSの市場予想超過が、単なる売上増より強い利益成長を示したためです。",
            "investment_readthrough": f"{theme}関連の需要が継続収益へ転換しており、同業の設備投資と部品需要にも追い風となります。",
            "watch_items": [f"{theme}の受注残から売上への転換速度", "非GAAP利益率の持続性"],
            "qualitative_evidence": {
                "why_moved": [theme, "EPS"],
                "investment_readthrough": [theme, "継続収益"],
            },
            "fact_sources": [{"label": "決算資料", "url": primary_url, "source_kind": "company_ir"}],
            "market_context_sources": [{"label": "市場予想・時間外株価", "url": price_url, "source_kind": "trusted_market_data"}],
            "after_hours_price_source_url": price_url,
            "after_hours_price_provider": "fixture_market_data",
        }

    after = rich_after_hours(
        {**earning, **deepcopy(research_by_ticker["DELL"])}, "Dell", 6.35,
        "$30.0B", "$2.00", "https://example.com/after-hours/dell", "AIサーバー",
    )
    after_second = rich_after_hours(
        deepcopy(earning), "Medtronic", -2.25, "$8.0B", "$1.50",
        "https://example.com/after-hours/mdt", "医療機器",
    )
    after_hours = [after, after_second]
    sources = [{"title": "Canonical market snapshot", "publisher": "Market Data", "url": market_url, "published_at": "2026-09-02T00:00:00Z"}]
    for item in research:
        if item["source_url"]:
            sources.append({"title": f"{item['ticker']} primary release", "publisher": item["company_name"], "url": item["source_url"], "published_at": "2026-09-01"})
    for item in news:
        sources.append({"title": item["title"], "publisher": "Reuters", "url": item["source_url"], "published_at": "2026-09-01T23:00:00Z"})
    for item in (earning, *after_hours):
        sources.append({"title": f"{item['ticker']} earnings", "publisher": item["company_name"], "url": item["source_url"], "published_at": "2026-09-01"})
    for item in after_hours:
        sources.append({"title": f"{item['ticker']} after hours", "publisher": "Market Data", "url": item["after_hours_price_source_url"], "published_at": "2026-09-01T22:47:00Z"})
    discovery_runs = [
        {"scope": "earnings_calendar", "query": "US earnings calendar 2026-09-01", "source_url": "https://example.com/discovery/earnings-calendar", "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "after_hours_movers", "query": "US after-hours movers 2026-09-01", "source_url": "https://example.com/discovery/after-hours-movers", "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "regulatory_filings", "query": "SEC 8-K results 2026-09-01", "source_url": "https://www.sec.gov/edgar/search/", "source_kind": "official_registry", "status": "completed"},
        {"scope": "material_events", "query": "US clinical trial FDA announcements 2026-09-01", "source_url": "https://example.com/discovery/material-events", "source_kind": "secondary_discovery", "status": "completed"},
    ]
    for run in discovery_runs:
        sources.append({"title": run["scope"], "publisher": "Discovery", "url": run["source_url"], "published_at": "2026-09-01"})
    commodity_url = "https://example.com/commodities/wti"
    sources.append({"title": "WTI close", "publisher": "Market Data", "url": commodity_url, "published_at": "2026-09-01"})
    result = {
        "schema_version": "ny_market_daily_v1", "quality_contract_version": "ny_market_quality_v2",
        "stable_key": "ny_market_daily:2026-09-02", "report_type": "ny_market_daily",
        "report_date_jst": "2026-09-02", "generated_at": "2026-09-02T07:25:00+09:00",
        "market_session_date": "2026-09-01", "market_status": "open", "headline": "NY市場はハイテク主導で下落",
        "summary_bullets": [f"日本語の検証済み要点 {i}" for i in range(5)],
        "canonical_market_data": {
            "market_data_contract_version": "ny_market_data_v1",
            "market_data_generated_at": "2026-09-02T07:10:00+09:00",
            "providers": ["fixture_market_data", "fixture_screener"],
            "raw_response_hashes": ["a" * 64, "b" * 64],
            "discrepancy_count": 0,
            "price_basis": "regular_close", "adjusted": False, "market_session_date": "2026-09-01",
            "source": {"name": "Market Data", "url": market_url, "retrieved_at": "2026-09-02T07:10:00+09:00"},
            "indexes": indexes, "sectors": sectors, "top_gainers_20": top,
        },
        "ticker_research": research,
        "index_moves": {item["symbol"]: {**item, "source_url": market_url} for item in indexes},
        "sector_moves": [{**item, "source_url": market_url} for item in sectors],
        "notable_gainers": [deepcopy(item) for item in top[:10]],
        "notable_losers": [deepcopy(item) for item in top[10:]],
        "top_gainers_20": deepcopy(top), "earnings": [earning],
        "after_hours_earnings": deepcopy(after_hours),
        "after_hours_research": deepcopy(after_hours),
        "after_hours_candidate_review": {
            "contract_version": "ny_market_after_hours_v2",
            "discovery_method": "broad_discovery_then_primary_verification",
            "market_session_date": "2026-09-01",
            "discovery_started_at": "2026-09-01T20:00:00Z",
            "discovery_completed_at": "2026-09-01T22:47:00Z",
            "discovery_runs": discovery_runs,
            "coverage_status": "quiet_day",
            "discovered_candidate_count": len(after_hours),
            "candidates": [
                {
                    "importance_rank": index,
                    "ticker": item["ticker"],
                    "status": "included",
                    "reason_code": "verified_earnings",
                    "reason": "決算発表と時間外反応を検証できたため採用。",
                    "discovered_by": ["earnings_calendar", "after_hours_movers"],
                    "discovery_source_url": "https://example.com/discovery/after-hours-movers",
                    "discovery_source_kind": "secondary_discovery",
                    "primary_source_url": item["source_url"],
                    "price_source_url": item["after_hours_price_source_url"],
                }
                for index, item in enumerate(after_hours, start=1)
            ],
        },
        "final_analysis_references": [deepcopy(research_by_ticker[ticker]) for ticker in ("SSM", "SWVL", "PETZ", "PXS", "MDT", "DELL")],
        "major_news": news,
        "commodities": [{"name": "WTI", "price": 70.0, "change_pct": 2.0, "reason": "供給懸念",
                         "source_url": commodity_url, "source_type": "market_data"}],
        "report_markdown": report, "sources": sources,
    }
    result["report_delivery"] = {"source_field": "report_markdown", "sha256": sha256(report.encode("utf-8")).hexdigest()}
    return apply_display_contract(attach_market_data_packet_metadata(result))


BENCHMARK_QUALITATIVE_SCORES = {"earnings_quality": 92, "news10": 90, "final_analysis": 93}
