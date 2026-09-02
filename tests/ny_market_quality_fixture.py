from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from lib.ny_market_research import attach_market_data_packet_metadata


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
        "ticker": ticker, "company_name": company, "close": close, "change_pct": change,
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
    report = ("# NY市場モーニングレポート 2026-09-02\n\n" + (
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
        **deepcopy(research_by_ticker["MDT"]), "company_description": "医療機器を世界で展開する企業。",
        "revenue": "$x", "eps": "$x", "guidance": "維持", "key_kpis": ["organic growth"],
        "one_offs": ["none"], "price_reaction": "+x%", "why_stock_moved": "成長率が期待を上回ったため。",
        "forward_implication": "医療機器需要の底堅さを示す。",
    }
    after = {
        **earning, **deepcopy(research_by_ticker["DELL"]), "after_hours_change_pct": 6.35,
        "as_of_utc": "2026-09-01T22:47:00Z", "as_of_jst": "2026-09-02T07:47:00+09:00",
        "session": "post_market",
    }
    sources = [{"title": "Canonical market snapshot", "publisher": "Market Data", "url": market_url, "published_at": "2026-09-02T00:00:00Z"}]
    for item in research:
        if item["source_url"]:
            sources.append({"title": f"{item['ticker']} primary release", "publisher": item["company_name"], "url": item["source_url"], "published_at": "2026-09-01"})
    for item in news:
        sources.append({"title": item["title"], "publisher": "Reuters", "url": item["source_url"], "published_at": "2026-09-01T23:00:00Z"})
    for item in (earning, after):
        sources.append({"title": f"{item['ticker']} earnings", "publisher": item["company_name"], "url": item["source_url"], "published_at": "2026-09-01"})
    commodity_url = "https://example.com/commodities/wti"
    sources.append({"title": "WTI close", "publisher": "Market Data", "url": commodity_url, "published_at": "2026-09-01"})
    result = {
        "schema_version": "ny_market_daily_v1", "quality_contract_version": "ny_market_quality_v2",
        "stable_key": "ny_market_daily:2026-09-02", "report_type": "ny_market_daily",
        "report_date_jst": "2026-09-02", "generated_at": "2026-09-02T07:25:00+09:00",
        "market_session_date": "2026-09-01", "market_status": "open", "headline": "NY市場はハイテク主導で下落",
        "summary_bullets": [f"検証済み要点 {i}" for i in range(5)],
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
        "top_gainers_20": deepcopy(top), "earnings": [earning], "after_hours_earnings": [after],
        "final_analysis_references": [deepcopy(research_by_ticker[ticker]) for ticker in ("SSM", "SWVL", "PETZ", "PXS", "MDT", "DELL")],
        "major_news": news,
        "commodities": [{"name": "WTI", "price": 70.0, "change_pct": 2.0, "reason": "供給懸念",
                         "source_url": commodity_url, "source_type": "market_data"}],
        "report_markdown": report, "sources": sources,
    }
    result["report_delivery"] = {"source_field": "report_markdown", "sha256": sha256(report.encode("utf-8")).hexdigest()}
    return attach_market_data_packet_metadata(result)


BENCHMARK_QUALITATIVE_SCORES = {"earnings_quality": 92, "news10": 90, "final_analysis": 93}
