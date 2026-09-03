import json

import pytest

from tools.company_news_batch_benchmark import (
    _build_event_union_v2,
    _canonical_identity_keys,
    _event_identity_evidence_v2,
    _overmerge_audit_v2,
    _pairwise_metrics_v2,
    _strict_dedupe_run_v2,
    _union_version_comparison,
)


def event(
    ticker: str,
    headline: str,
    url: str,
    *,
    date: str = "2026-08-25",
    category: str = "demand",
    summary: str | None = None,
    importance: str = "medium",
    earnings_relevance: str = "likely",
) -> dict:
    return {
        "ticker": ticker,
        "headline": headline,
        "source_url": url,
        "published_at": f"{date}T10:00:00+09:00",
        "source_type": "ir",
        "category": category,
        "summary": summary or headline,
        "why_it_matters": "Material benchmark fixture.",
        "importance": importance,
        "earnings_relevance": earnings_relevance,
    }


@pytest.mark.parametrize(
    ("left_url", "right_url"),
    [
        ("https://www.celsys.com/topic/20260825", "https://www.celsys.com/en/topic/20260825"),
        ("https://www.celsys.com/topic/20260831", "https://www.celsys.com/en/topic/20260831"),
        ("https://www.shiftinc.jp/news/20260825_segment", "https://en.shiftinc.jp/news_en/20260825_segment"),
        ("https://www.shiftinc.jp/news/20260825_dico", "https://en.shiftinc.jp/news_en/20260825_dico"),
        ("https://www.shiftinc.jp/news/20260825_shift-hap", "https://en.shiftinc.jp/news_en/20260825_shift-hap"),
    ],
)
def test_bilingual_official_page_paths_have_the_same_canonical_key(left_url, right_url):
    left = event("3663", "Japanese disclosure", left_url)
    right = event("3663", "English disclosure", right_url)

    evidence = _event_identity_evidence_v2(left, right)

    assert _canonical_identity_keys(left)["canonical_page_key"] == _canonical_identity_keys(right)["canonical_page_key"]
    assert evidence["tier"] == 1
    assert evidence["confidence"] == "HIGH"
    assert evidence["auto_merge"] is True


def test_sanbio_disclosure_mirrors_merge_despite_host_category_and_annotation_differences():
    left = event(
        "4592",
        "ミナリス社米国工場閉鎖に伴うアクーゴ®生産体制の国内統合に関するお知らせ",
        "https://assets.minkabu.jp/news/article_media_content/urn:newsml:tdnet.info:20260831528477/140120260831528477.pdf",
        date="2026-08-31",
        category="supplier",
        importance="high",
        earnings_relevance="direct",
    )
    right = event(
        "4592",
        "ミナリス社米国工場閉鎖に伴うアクーゴ(R)生産体制の国内統合に関するお知らせ",
        "https://finance.example.jp/disclosure/20260831/20260831528477.pdf",
        date="2026-08-31",
        category="capacity",
        importance="low",
        earnings_relevance="context",
    )

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["auto_merge"] is True
    assert evidence["method"] == "canonical_disclosure_document_id"
    assert evidence["category_equal"] is False
    assert evidence["importance_equal"] is False
    assert evidence["earnings_relevance_equal"] is False


def test_kyogoku_stable_itemid_merges_while_layout_parameter_is_ignored():
    left = event(
        "9073", "三菱ふそう新型トラクタ試乗会に参加しました",
        "https://www.kyogoku.co.jp/news/detail.html?dispmid=600&itemid=116",
        date="2026-08-26",
    )
    right = event(
        "9073", "三菱ふそう新型トラクタ試乗会に参加しました",
        "https://www.kyogoku.co.jp/news?dispmid=582&itemid=116",
        date="2026-08-26", category="capacity",
    )

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["auto_merge"] is True
    assert evidence["method"] == "stable_query_item_id"
    assert evidence["left_canonical_keys"]["stable_item_keys"] == evidence["right_canonical_keys"]["stable_item_keys"]


def test_central_international_pdf_and_html_earnings_are_one_document_purpose():
    left = event(
        "7170", "令和8年12月期 中間決算短信〔日本基準〕(連結)",
        "https://www.cig-ins.co.jp/ir/ir/20261231_tanshin.pdf",
        date="2026-08-26", category="customer",
    )
    right = event(
        "7170", "令和8年12月期 中間決算短信",
        "https://www.cig-ins.co.jp/ir/ir/20261231a_tanshin",
        date="2026-08-26", category="management_comment",
    )

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["auto_merge"] is True
    assert evidence["method"] == "same_issuer_document_purpose"


def test_tokyo_theatre_alternate_landing_url_correct_merge_is_preserved():
    headline = "メリハリのあるリノベーション 今ある要素を活かした、新たな住まいの誕生～『リノまま』よりご提案～"
    left = event("9633", headline, "https://www.theatres.co.jp/", date="2026-08-27")
    right = event("9633", headline, "https://www.theatres.co.jp/pickup", date="2026-08-27")

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["auto_merge"] is True
    assert evidence["method"] == "exact_headline_strong_identity"


def test_press_release_prefix_and_issuer_prefix_do_not_split_same_dalton_event():
    left = event(
        "7972", "ダルトン、本社を豊洲に移転へ、ティザー動画公開",
        "https://www.itoki.jp/company/news/2026/0827_daltonteaser", date="2026-08-27",
        summary="ダルトンが本社を豊洲へ移転しティザー動画を公開した。",
    )
    right = event(
        "7972", "【プレスリリース】本社を豊洲に移転へ、ティザー動画公開",
        "https://www.dalton.co.jp/news/overall/newhqteaser", date="2026-08-27",
        summary="本社を豊洲へ移転する計画とティザー動画を公開した。",
    )

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["auto_merge"] is True
    assert evidence["method"] == "headline_containment_and_substance"


def test_bluebon_two_products_stay_separate_and_cross_run_matching_is_one_to_one():
    cup = event(
        "2208", "いもジェンヌアイス（ブルボン）2026年8月24日発売",
        "https://news.nissyoku.co.jp/foodsnews/260728-25",
        date="2026-08-27", category="new_product",
        summary="いもジェンヌペーストを使ったカップアイスを発売した。",
    )
    monaka = event(
        "2208", "ルマンド＜いもジェンヌ＞（ブルボン）2026年8月24日発売",
        "https://news.nissyoku.co.jp/foodsnews/260728-24",
        date="2026-08-27", category="new_product",
        summary="いもジェンヌペーストを使ったモナカアイスを発売した。",
    )
    union = _build_event_union_v2(
        {
            "reference": {"2208": [cup, monaka]},
            "reference02": {"2208": [monaka]},
            "batch02": {"2208": [cup, monaka]},
            "batch04": {"2208": [cup]},
        },
        {"2208": "ブルボン"},
    )

    assert len(union["events"]) == 2
    assert all(sum(member["run_label"] == "reference01" for member in cluster["members"]) == 1 for cluster in union["events"])
    assert {cluster["headline"] for cluster in union["events"]} == {cup["headline"], monaka["headline"]}


def test_same_run_semantic_similarity_never_auto_merges():
    first = event("1000", "Alpha logistics service launch", "https://example.com/news/one", summary="Shared logistics platform opens nationwide")
    second = event("1000", "Alpha logistics platform rollout", "https://example.com/news/two", summary="Shared logistics platform opens nationwide")

    nodes, accepted, review = _strict_dedupe_run_v2("reference", {"1000": [first, second]})

    assert len(nodes) == 2
    assert accepted == []
    assert review


def test_same_ticker_date_category_or_substance_theme_is_not_sufficient():
    first = event("9531", "AIVALIX株式会社への出資", "https://www.tokyo-gas.co.jp/news/press/20260828-01.html", date="2026-08-28", category="m_and_a", summary="東京ガスがAI企業へ出資した。")
    second = event("9531", "10月検針分のガス料金", "https://www.tokyo-gas.co.jp/news/press/20260828-02.html", date="2026-08-28", category="m_and_a", summary="東京ガスが10月料金を改定した。")
    themed = event("9531", "別のAI企業との実証", "https://www.tokyo-gas.co.jp/news/press/20260828-03.html", date="2026-08-28", category="m_and_a", summary="東京ガスがAI企業と新しい事業を開始した。")

    assert _event_identity_evidence_v2(first, second)["auto_merge"] is False
    assert _event_identity_evidence_v2(first, themed)["auto_merge"] is False


def test_distinct_query_content_ids_on_the_same_path_stay_separate():
    first = event(
        "583A", "中古マンション価格の調整",
        "https://www.fudousankeizai.co.jp/daily?id=298558",
        summary="都心部で中古マンション在庫が増加した。",
    )
    second = event(
        "583A", "地場業者の売買仲介",
        "https://www.fudousankeizai.co.jp/daily?id=298586",
        summary="都心部で中古マンション在庫が増加した。",
    )

    evidence = _event_identity_evidence_v2(first, second, same_run=True)

    assert evidence["left_canonical_keys"]["canonical_page_key"] != evidence["right_canonical_keys"]["canonical_page_key"]
    assert evidence["auto_merge"] is False


def test_tokyo_gas_same_day_releases_and_shift_three_releases_remain_separate():
    tokyo_gas = [
        event("9531", title, f"https://www.tokyo-gas.co.jp/news/press/20260828-0{index}.html", date="2026-08-28")
        for index, title in enumerate(("AIVALIX出資", "原料費調整", "会社分割", "バイオメタン実証"), start=1)
    ]
    shift_jp = [
        event("3697", title, f"https://www.shiftinc.jp/news/20260825_{slug}")
        for title, slug in (("報告セグメントの変更", "segment"), ("DICO株式会社の吸収合併", "dico"), ("SHIFT HAP設立", "shift-hap"))
    ]
    shift_en = [
        event("3697", f"English {slug}", f"https://en.shiftinc.jp/news_en/20260825_{slug}")
        for slug in ("segment", "dico", "shift-hap")
    ]
    union = _build_event_union_v2(
        {
            "reference": {"9531": tokyo_gas, "3697": shift_jp},
            "reference02": {"3697": shift_en},
            "batch02": {}, "batch04": {},
        },
        {"9531": "東京ガス", "3697": "SHIFT"},
    )

    assert len([cluster for cluster in union["events"] if cluster["ticker"] == "9531"]) == 4
    shift_clusters = [cluster for cluster in union["events"] if cluster["ticker"] == "3697"]
    assert len(shift_clusters) == 3
    assert all(cluster["found_by"]["reference01"] and cluster["found_by"]["reference02"] for cluster in shift_clusters)


def test_one_to_one_conflict_is_emitted_instead_of_many_to_one_merge():
    left_a = event("7170", "令和8年12月期 中間決算短信 A", "https://issuer.example/20261231_a.pdf", date="2026-08-26")
    left_b = event("7170", "令和8年12月期 中間決算短信 B", "https://issuer.example/20261231_b.pdf", date="2026-08-26")
    right = event("7170", "令和8年12月期 中間決算短信", "https://issuer.example/20261231.html", date="2026-08-26")

    union = _build_event_union_v2(
        {"reference": {"7170": [left_a, left_b]}, "reference02": {"7170": [right]}, "batch02": {}, "batch04": {}},
        {},
    )

    assert len(union["events"]) == 2
    assert any("one_to_one_conflict" in candidate["conflicting_signals"] for candidate in union["matcher_merge_candidates"])


def test_probable_semantic_pair_is_ambiguous_review_not_auto_merge():
    left = event("1000", "Alpha logistics service starts nationwide", "https://issuer-a.example/news/one", summary="The same shared logistics platform begins nationwide operations")
    right = event("1000", "Alpha logistics service begins nationwide", "https://issuer-b.example/news/two", summary="The same shared logistics platform begins nationwide operations")

    union = _build_event_union_v2(
        {"reference": {"1000": [left]}, "reference02": {"1000": [right]}, "batch02": {}, "batch04": {}},
        {},
    )

    assert len(union["events"]) == 2
    assert union["matcher_merge_candidates"]
    assert union["matcher_merge_candidates"][0]["confidence"] == "MEDIUM"


def test_ticker_is_a_hard_identity_requirement_even_for_exact_document_url():
    left = event("1000", "Same event", "https://example.com/disclosure/20260831528477.pdf")
    right = event("1001", "Same event", "https://example.com/disclosure/20260831528477.pdf")

    evidence = _event_identity_evidence_v2(left, right)

    assert evidence["same_ticker"] is False
    assert evidence["auto_merge"] is False


def test_matcher_output_and_content_hash_inputs_are_deterministic():
    runs = {
        "reference": {"1000": [event("1000", "Same event", "https://example.com/a")]},
        "reference02": {"1000": [event("1000", "Same event", "https://example.com/a/")]},
        "batch02": {}, "batch04": {},
    }

    first = _build_event_union_v2(runs, {"1000": "A"})
    second = _build_event_union_v2(runs, {"1000": "A"})

    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)


def test_annotation_consistency_is_separate_from_detection_and_pairwise_is_symmetric():
    left = event("1000", "Same event", "https://example.com/a", category="supplier", importance="high", earnings_relevance="direct")
    right = event("1000", "Same event", "https://example.com/a/", category="capacity", importance="low", earnings_relevance="context")

    forward = _pairwise_metrics_v2("reference", "reference02", {"1000": [left]}, {"1000": [right]}, ["1000"])
    reverse = _pairwise_metrics_v2("reference02", "reference", {"1000": [right]}, {"1000": [left]}, ["1000"])

    assert forward["matched_event_count"] == reverse["matched_event_count"] == 1
    assert forward["event_recall"] == reverse["event_recall"] == 1.0
    consistency = forward["annotation_consistency"]
    assert consistency["category_agreement"]["rate"] == 0.0
    assert consistency["importance_agreement"]["rate"] == 0.0
    assert consistency["earnings_relevance_agreement"]["rate"] == 0.0


def test_legacy_bluebon_overmerge_is_detected_and_version_diff_records_split():
    member_a = {"run_label": "reference01", "run_name": "reference", "ticker": "2208", "event_index": 0, "headline": "Cup"}
    member_b = {"run_label": "reference01", "run_name": "reference", "ticker": "2208", "event_index": 1, "headline": "Monaka"}
    v1 = [{"match_cluster_id": "old", "ticker": "2208", "members": [member_a, member_b]}]
    v2 = [
        {"match_cluster_id": "new-a", "ticker": "2208", "members": [{**member_a, "observation_id": "reference01:2208:0000"}], "matcher_evidence": []},
        {"match_cluster_id": "new-b", "ticker": "2208", "members": [{**member_b, "observation_id": "reference01:2208:0001"}], "matcher_evidence": []},
    ]

    audit = _overmerge_audit_v2(v1, v2)
    comparison = _union_version_comparison(v1, v2)

    assert audit["bluebon_2208_legacy_overmerge_detected"] is True
    assert audit["legacy_v1_same_run_multi_member_cluster_count"] == 1
    assert comparison["overmerge_clusters_split_count"] == 1
    assert comparison["overmerge_splits_created_count"] == 1
