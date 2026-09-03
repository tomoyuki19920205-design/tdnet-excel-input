from __future__ import annotations

from copy import deepcopy
import re

import pytest

from lib.ny_market import NYMarketValidationError, validate_payload
from lib.ny_market_after_hours import build_after_hours_discovery_plan
from lib.ny_market_display import apply_display_contract
from lib.ny_market_research import attach_market_data_packet_metadata
from tests.ny_market_quality_fixture import payload


def _section(markdown: str, heading: str) -> str:
    marker = f"## {heading}\n\n"
    body = markdown.split(marker, 1)[1]
    return body.split("\n## ", 1)[0].strip()


SEPTEMBER_3_CANDIDATES = [
    ("AVGO", "Broadcom", "半導体とインフラソフトを展開する企業です。", -3.5,
     "Q3売上は$29.59B、調整後EPSは$3.32、Q4売上見通しは$34.8Bでした。",
     ["$29.59B", "$3.32", "$34.8B"], "Q4見通しが市場予想を下回り、AI成長より非AI半導体の弱さが意識されました。",
     "https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-third-quarter-fiscal-year-2026-financial", "company_ir"),
    ("SNOW", "Snowflake", "企業向けAIデータクラウドを提供する企業です。", 21.0,
     "Q2売上は$1.55B、製品売上は$1.49Bで前年比+37%、調整後EPSは$0.62でした。",
     ["$1.55B", "$1.49B", "+37%", "$0.62"], "製品売上の加速と通期見通しの引き上げが強く評価されました。",
     "https://www.sec.gov/Archives/edgar/data/1640147/000164014726000033/fy2027q2earnings.htm", "sec"),
    ("HPE", "Hewlett Packard Enterprise", "企業向けサーバー、ネットワーク、クラウド基盤を提供する企業です。", -1.0,
     "Q3売上は$12.2Bで前年比+34%、調整後EPSは$1.11、通期EPS見通しは$3.75–$3.85でした。",
     ["$12.2B", "+34%", "$1.11", "$3.75–$3.85"], "上振れと見通し引き上げ後も、高い期待を背景に利益確定が優勢でした。",
     "https://www.sec.gov/Archives/edgar/data/1645590/000164559026000078/ex-991x922026x8k.htm", "sec"),
    ("NTAP", "NetApp", "企業向けデータストレージとハイブリッドクラウド基盤を提供する企業です。", -7.0,
     "Q1売上は$2.03Bで前年比+30%、調整後EPSは$2.58、通期EPS見通しは$9.73–$10.03でした。",
     ["$2.03B", "+30%", "$2.58", "$9.73–$10.03"], "好決算と見通し引き上げにもかかわらず、事前期待の高さから材料出尽くしとなりました。",
     "https://investors.netapp.com/news/news-details/2026/NetApp-Reports-First-Quarter-of-Fiscal-Year-2027-Results/default.aspx", "company_ir"),
    ("NTSK", "Netskope", "クラウド、AI、ネットワーク向けのSASEセキュリティ基盤を提供する企業です。", 16.0,
     "Q2売上は$220.5Mで前年比+29%、ARRは$899Mで+27%、調整後1株損失は-$0.03でした。",
     ["$220.5M", "+29%", "$899M", "+27%", "-$0.03"], "売上とARRの成長、赤字縮小、通期見通し改善が評価されました。",
     "https://investors.netskope.com/news-releases/news-release-details/netskope-announces-strong-second-quarter-fiscal-2027-financial", "company_ir"),
    ("CHPT", "ChargePoint", "法人・家庭向けEV充電ネットワークと機器を提供する企業です。", 16.0,
     "Q2売上は$116Mで前年比+18%、サブスクリプション売上は$44M、調整後EBITDA損失は-$4.8Mでした。",
     ["$116M", "+18%", "$44M", "-$4.8M"], "売上上振れと損失縮小が、収益性改善への期待につながりました。",
     "https://www.sec.gov/Archives/edgar/data/1777393/000177739326000061/chpt8-kerfy2027q2exx991.htm", "sec"),
    ("FIVE", "Five Below", "若年層向け低価格雑貨を展開するディスカウント小売企業です。", 3.0,
     "Q2売上は$1.26Bで前年比+22.9%、既存店売上は+14.1%、調整後EPSは$1.68でした。",
     ["$1.26B", "+22.9%", "+14.1%", "$1.68"], "既存店成長と通期見通し引き上げ、自社株買いが評価されました。",
     "https://www.sec.gov/Archives/edgar/data/1177609/000117760926000023/q22026fivebelowexhibit991.htm", "sec"),
    ("TLYS", "Tilly’s", "若者向け衣料・靴・アクセサリーを店舗とECで販売する企業です。", 32.0,
     "Q2売上は$163.5Mで前年比+8.1%、既存店売上は+12.1%、Q3 EPS見通しは$0.07–$0.12でした。",
     ["$163.5M", "+8.1%", "+12.1%", "$0.07–$0.12"], "黒字化の進展と市場予想を上回るQ3見通しが急騰材料となりました。",
     "https://www.sec.gov/Archives/edgar/data/1524025/000162828026060088/q2fy2026earningsrelease.htm", "sec"),
    ("PVH", "PVH", "Calvin KleinとTommy Hilfigerを展開するアパレル企業です。", -3.0,
     "Q2売上は$2.097B、調整後営業利益率は11.1%、調整後EPSは$3.70でした。",
     ["$2.097B", "11.1%", "$3.70"], "利益は上振れた一方、欧州の弱さと慎重な見通しが嫌気されました。",
     "https://www.sec.gov/Archives/edgar/data/78239/000007823926000055/ex99120262q8k.htm", "sec"),
    ("RARE", "Ultragenyx", "希少疾患向け治療薬を開発・販売するバイオ医薬品企業です。", -40.0,
     "Angelman症候群向けapazunersenのPhase 3 Aspire試験は、主要評価項目と重要な副次評価項目を達成できませんでした。",
     ["Phase 3"], "主要開発品の臨床失敗がパイプライン価値を大きく損なう材料となりました。",
     "https://ir.ultragenyx.com/news-releases/news-release-details/ultragenyx-announces-phase-3-aspire-results-angelman-syndrome", "company_ir"),
]


def _september_3_payload() -> dict:
    data = payload()
    data["stable_key"] = "ny_market_daily:2026-09-03"
    data["report_date_jst"] = "2026-09-03"
    data["generated_at"] = "2026-09-03T05:40:00+09:00"
    data["market_session_date"] = "2026-09-02"
    data["canonical_market_data"]["market_session_date"] = "2026-09-02"
    data["report_markdown"] = data["report_markdown"].replace("2026-09-02", "2026-09-03", 1)
    template = deepcopy(data["ticker_research"][-1])
    selected = []
    candidates = []
    for ticker, name, description, change, results, numbers, takeaway, source_url, source_type in SEPTEMBER_3_CANDIDATES:
        research = deepcopy(template)
        research.update({
            "ticker": ticker,
            "company_name": name,
            "company_description": description,
            "catalyst": "引け後の正式発表を一次情報で確認",
            "catalyst_type": "earnings_or_material_event",
            "source_url": source_url,
            "source_type": source_type,
            "search_status": "verified_catalyst",
            "search_queries": [f'"{ticker}" 2026-09-02 results', f'"{name}" investor relations 2026-09-02'],
        })
        data["ticker_research"].append(research)
        item = {
            **deepcopy(research),
            "revenue": numbers[0],
            "eps": numbers[-1],
            "guidance": "正式発表に記載された見通し、または対象外",
            "key_kpis": numbers,
            "one_offs": [],
            "price_reaction": f"時間外 約{change:+.2f}%",
            "why_stock_moved": takeaway,
            "forward_implication": takeaway,
            "after_hours_change_pct": change,
            "as_of_utc": "2026-09-02T20:28:00Z",
            "as_of_jst": "2026-09-03T05:28:00+09:00",
            "session": "post_market",
            "display_company_name": name,
            "results_summary": results,
            "investment_takeaway": takeaway,
            "after_hours_price_source_url": "https://www.investing.com/news/stock-market-news/afterhours-movers-avgo-snow-hpe-ntap-ntsk-chpt-five-tlys-pvh-rare-432SI-4886740",
            "after_hours_price_provider": "Investing.com",
            "display_numbers": numbers,
        }
        selected.append(item)
        candidates.append({
            "importance_rank": len(candidates) + 1,
            "ticker": ticker,
            "status": "included",
            "reason_code": "verified_material_event" if ticker == "RARE" else "verified_earnings",
            "reason": "正式発表と信頼できる時間外反応を検証できたため採用。",
            "discovered_by": ["after_hours_movers", "material_events"] if ticker == "RARE" else ["earnings_calendar", "after_hours_movers"],
            "discovery_source_url": item["after_hours_price_source_url"],
            "discovery_source_kind": "secondary_discovery",
            "primary_source_url": source_url,
            "price_source_url": item["after_hours_price_source_url"],
        })
        data["sources"].extend([
            {"title": f"{ticker} primary", "publisher": name, "url": source_url, "published_at": "2026-09-02"},
        ])
    data["sources"].append({
        "title": "After-hours movers September 2",
        "publisher": "Investing.com",
        "url": selected[0]["after_hours_price_source_url"],
        "published_at": "2026-09-02T20:28:00Z",
    })
    data["after_hours_earnings"] = deepcopy(selected)
    data["after_hours_research"] = deepcopy(selected)
    discovery_runs = [
        {"scope": "earnings_calendar", "query": "US earnings calendar 2026-09-02", "source_url": "https://www.nasdaq.com/market-activity/earnings", "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "after_hours_movers", "query": "US after-hours movers 2026-09-02", "source_url": selected[0]["after_hours_price_source_url"], "source_kind": "secondary_discovery", "status": "completed"},
        {"scope": "regulatory_filings", "query": "SEC 8-K results 2026-09-02", "source_url": "https://www.sec.gov/edgar/search/", "source_kind": "official_registry", "status": "completed"},
        {"scope": "material_events", "query": "US clinical trial FDA announcements 2026-09-02", "source_url": selected[0]["after_hours_price_source_url"], "source_kind": "secondary_discovery", "status": "completed"},
    ]
    for run in discovery_runs:
        if not any(source["url"] == run["source_url"] for source in data["sources"]):
            data["sources"].append({"title": run["scope"], "publisher": "Discovery", "url": run["source_url"], "published_at": "2026-09-02"})
    data["after_hours_candidate_review"] = {
        "contract_version": "ny_market_after_hours_v1",
        "discovery_method": "broad_discovery_then_primary_verification",
        "market_session_date": "2026-09-02",
        "discovery_started_at": "2026-09-02T20:00:00Z",
        "discovery_completed_at": "2026-09-02T20:28:00Z",
        "discovery_runs": discovery_runs,
        "coverage_status": "normal",
        "discovered_candidate_count": len(candidates),
        "candidates": candidates,
    }
    return apply_display_contract(attach_market_data_packet_metadata(data))


def test_discovery_plan_is_broad_then_verifies_every_candidate_without_truncation():
    tickers = ["AVGO", "SNOW", "HPE", "NTAP", "NTSK", "FIVE", "RARE", "CHPT", "TLYS", "PVH"]
    plan = build_after_hours_discovery_plan(
        "2026-09-02",
        [{"ticker": ticker, "company_name": f"Company {ticker}"} for ticker in tickers],
    )
    assert plan["discovery_method"] == "broad_discovery_then_primary_verification"
    assert len(plan["broad_discovery_queries"]) == 4
    assert plan["market_session_date"] == "2026-09-02"
    assert [item["ticker"] for item in plan["candidate_verification"]] == tickers
    assert all(len(item["verification_queries"]) == 3 for item in plan["candidate_verification"])


def test_two_company_quiet_day_is_valid_and_renders_concise_paragraphs():
    data = payload()
    validate_payload(data)
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    assert len(re.findall(r"(?m)^#### ", body)) == 2
    assert "時間外株価は決算直後の初動" in body
    assert "通常取引終値" not in body
    assert "session=post_market" not in body
    assert "as_of_utc" not in body
    assert "provider" not in body.lower()
    assert "\\*\\*" not in body


def test_quiet_day_requires_completed_broad_discovery_and_cannot_bypass_count():
    missing_discovery = payload()
    del missing_discovery["after_hours_candidate_review"]["discovery_runs"]
    with pytest.raises(NYMarketValidationError, match="discovery_runs"):
        validate_payload(missing_discovery)

    self_declared_normal = payload()
    self_declared_normal["after_hours_candidate_review"]["coverage_status"] = "normal"
    with pytest.raises(NYMarketValidationError, match="derived from selected count"):
        validate_payload(self_declared_normal)

    self_declared_quiet = _september_3_payload()
    self_declared_quiet["after_hours_candidate_review"]["coverage_status"] = "quiet_day"
    with pytest.raises(NYMarketValidationError, match="derived from selected count"):
        validate_payload(self_declared_quiet)


def test_excluded_candidate_requires_a_structured_reason():
    data = payload()
    review = data["after_hours_candidate_review"]
    review["candidates"].append({
        "importance_rank": 3,
        "ticker": "TEST",
        "status": "excluded",
        "reason": "一次情報を確認できませんでした。",
        "discovered_by": ["earnings_calendar"],
        "discovery_source_url": "https://example.com/discovery/earnings-calendar",
        "discovery_source_kind": "secondary_discovery",
    })
    review["discovered_candidate_count"] = 3
    with pytest.raises(NYMarketValidationError, match="reason_code"):
        validate_payload(data)


def test_september_3_fixture_renders_all_ten_verified_candidates_compactly():
    data = _september_3_payload()
    validate_payload(data)
    body = _section(data["report_markdown"], "引け後・アフター決算の注目株")
    assert len(re.findall(r"(?m)^#### ", body)) == 10
    for ticker, name, *_ in SEPTEMBER_3_CANDIDATES:
        assert f"#### {name}（{ticker}）— 時間外 約" in body
    for hidden in ("as_of_utc", "as_of_jst", "search_status", "searched_at", "provider=", "通常取引終値"):
        assert hidden not in body
    company_blocks = body.split("\n\n#### ")[1:]
    assert len(company_blocks) == 10
    assert all(len(block.split("\n\n")) == 4 for block in company_blocks)


def test_after_hours_projection_tampering_fails_closed():
    data = payload()
    data["after_hours_earnings"][0]["results_summary"] = (
        data["after_hours_earnings"][0]["results_summary"].replace("$30.0B", "$99.0B")
    )
    data["after_hours_earnings"][0]["display_numbers"][0] = "$99.0B"
    data = apply_display_contract(data)
    with pytest.raises(NYMarketValidationError, match="canonical after_hours_research"):
        validate_payload(data)


def test_after_hours_description_number_and_session_fail_closed():
    missing_description = payload()
    del missing_description["after_hours_earnings"][0]["company_description"]
    with pytest.raises(NYMarketValidationError, match="company_description"):
        validate_payload(missing_description)

    changed_number = payload()
    changed_number["after_hours_research"][0]["results_summary"] = (
        changed_number["after_hours_research"][0]["results_summary"].replace("$30.0B", "$31.0B")
    )
    with pytest.raises(NYMarketValidationError, match="canonical after_hours_research"):
        validate_payload(changed_number)

    wrong_session = payload()
    wrong_session["after_hours_earnings"][0]["session"] = "regular"
    with pytest.raises(NYMarketValidationError, match="post_market"):
        validate_payload(wrong_session)


def test_after_hours_renderer_preserves_every_other_section():
    data = payload()
    original = deepcopy(data)
    data["after_hours_earnings"][0]["investment_takeaway"] += " 確認用。"
    data["after_hours_research"][0]["investment_takeaway"] += " 確認用。"
    rendered = apply_display_contract(data)
    for heading in ("要点", "話題の値下がり10社", "主要決算"):
        assert _section(rendered["report_markdown"], heading) == _section(
            original["report_markdown"], heading
        )
