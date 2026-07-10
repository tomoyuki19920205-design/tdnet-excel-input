import os
import pytest
import zipfile
import datetime
from src.segment.xbrl_segment_extractor import (
    extract_segments_from_xbrl_zip,
    extract_segments_from_xbrl_zip_detailed,
    SegmentExtractionResult
)

def create_dummy_ixbrl_zip(zip_path, title, contexts: list[dict], facts: list[dict]):
    # context xml を構築
    ctx_xmls = []
    for c in contexts:
        cid = c["id"]
        start = c.get("start")
        end = c.get("end")
        if start and end:
            ctx_xmls.append(f"""
            <xbrli:context id="{cid}">
              <xbrli:period>
                <xbrli:startDate>{start}</xbrli:startDate>
                <xbrli:endDate>{end}</xbrli:endDate>
              </xbrli:period>
            </xbrli:context>
            """)
        else:
            ctx_xmls.append(f"""
            <xbrli:context id="{cid}">
              <xbrli:period>
                <xbrli:instant>{c.get('instant', '2026-08-31')}</xbrli:instant>
              </xbrli:period>
            </xbrli:context>
            """)
            
    # fact xml を構築
    fact_xmls = []
    for f in facts:
        name = f["name"]
        ctx = f["context"]
        val = f["val"]
        fact_xmls.append(f"""
        <ix:nonfraction name="{name}" contextref="{ctx}" unitref="JPY" decimals="0" scale="6">{val}</ix:nonfraction>
        """)
        
    main_html = f"""
    <html>
    <head>
      <meta charset="utf-8" />
    </head>
    <body>
      <ix:nonNumeric name="jpcrp_cor:DocumentTitle">{title}</ix:nonNumeric>
      {"".join(ctx_xmls)}
    </body>
    </html>
    """
    
    sg_html = f"""
    <html>
    <body>
      {"".join(fact_xmls)}
    </body>
    </html>
    """
    
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ixbrl-main-7601.htm", main_html)
        zf.writestr("ixbrl-qcsg-7601.htm", sg_html)


class TestXBRLSegmentContextSelection:
    # 8. 本物の抽出器テスト
    # 決算期末日 2027-02-28 に対する検証

    def test_case_1_1q_current_vs_prior(self, tmp_path):
        # 1. 1Q当期3か月と前年3か月 -> 当期だけ採用
        contexts = [
            {"id": "CurrentYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-05-31"},
            {"id": "PriorYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2025-03-01", "end": "2025-05-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "100,000,000"},
            {"name": "jppfs_cor:netsales", "context": "PriorYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "80,000,000"},
        ]
        zip_p = tmp_path / "test_1q.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第1四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="1Q")
        # results には current のみが返る (previous は period が前年になるため別行として処理されるが、
        # 同一当期行として混入しないことを確認)
        current_rows = [r for r in res if r.period == "2027-02-28"]
        prior_rows = [r for r in res if r.period == "2026-02-28"]

        assert len(current_rows) == 1
        assert current_rows[0].sales == 100000000
        assert len(prior_rows) == 1
        assert prior_rows[0].sales == 80000000

    def test_case_2_and_3_2q_cumulative_vs_quarterly_and_prior(self, tmp_path):
        # 2. 2Q当期6か月累計と当期単独3か月 -> 6か月累計を採用
        # 3. 2Q前年6か月累計 -> 当期へ混入しない
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"}, # 6M YTD (183 days)
            {"id": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-06-01", "end": "2026-08-31"}, # 3M Q (91 days)
            {"id": "PriorYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2025-03-01", "end": "2025-08-31"}, # Prior 6M YTD (184 days)
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
            {"name": "jppfs_cor:netsales", "context": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "100,000,000"},
            {"name": "jppfs_cor:netsales", "context": "PriorYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "180,000,000"},
        ]
        zip_p = tmp_path / "test_2q.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q")
        current_rows = [r for r in res if r.period == "2027-02-28"]
        prior_rows = [r for r in res if r.period == "2026-02-28"]

        assert len(current_rows) == 1
        assert current_rows[0].sales == 200000000 # 6M YTD
        assert len(prior_rows) == 1
        assert prior_rows[0].sales == 180000000 # 前期6M YTD (混入していない)

    def test_case_4_and_5_3q_cumulative_vs_quarterly_and_prior(self, tmp_path):
        # 4. 3Q当期9か月累計と当期単独3か月 -> 9か月累計を採用
        # 5. 3Q前年9か月累計 -> 当期へ混入しない
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-11-30"}, # 9M YTD (274 days)
            {"id": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-09-01", "end": "2026-11-30"}, # 3M Q (91 days)
            {"id": "PriorYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2025-03-01", "end": "2025-11-30"}, # Prior 9M YTD (275 days)
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "300,000,000"},
            {"name": "jppfs_cor:netsales", "context": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "110,000,000"},
            {"name": "jppfs_cor:netsales", "context": "PriorYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "270,000,000"},
        ]
        zip_p = tmp_path / "test_3q.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第3四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="3Q")
        current_rows = [r for r in res if r.period == "2027-02-28"]
        prior_rows = [r for r in res if r.period == "2026-02-28"]

        assert len(current_rows) == 1
        assert current_rows[0].sales == 300000000 # 9M YTD
        assert len(prior_rows) == 1
        assert prior_rows[0].sales == 270000000 # 前期9M YTD

    def test_case_6_fy_current_vs_prior(self, tmp_path):
        # 6. FY当期通期と前年通期 -> 当期通期だけ採用
        contexts = [
            {"id": "CurrentYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2027-02-28"},
            {"id": "PriorYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2025-03-01", "end": "2026-02-28"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "400,000,000"},
            {"name": "jppfs_cor:netsales", "context": "PriorYearDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "350,000,000"},
        ]
        zip_p = tmp_path / "test_fy.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="FY")
        current_rows = [r for r in res if r.period == "2027-02-28"]
        prior_rows = [r for r in res if r.period == "2026-02-28"]

        assert len(current_rows) == 1
        assert current_rows[0].sales == 400000000
        assert len(prior_rows) == 1
        assert prior_rows[0].sales == 350000000

    def test_case_7_8_9_multiple_contexts_and_order_independence(self, tmp_path):
        # 7. 同一memberの複数contextで上書き消失しない
        # 8/9. fact/contextの順序反転で結果が同一であることを検証

        contexts_base = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
            {"id": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-06-01", "end": "2026-08-31"},
        ]

        # パターンA
        facts_a = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
            {"name": "jppfs_cor:netsales", "context": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "100,000,000"},
        ]
        zip_a = tmp_path / "test_order_a.zip"
        create_dummy_ixbrl_zip(zip_a, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts_base, facts_a)

        # パターンB: fact 順序反転
        facts_b = [
            {"name": "jppfs_cor:netsales", "context": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "100,000,000"},
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
        ]
        zip_b = tmp_path / "test_order_b.zip"
        create_dummy_ixbrl_zip(zip_b, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts_base, facts_b)

        # パターンC: context 順序反転 (ZIP作成時の定義順を変更)
        contexts_c = [
            {"id": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-06-01", "end": "2026-08-31"},
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        zip_c = tmp_path / "test_order_c.zip"
        create_dummy_ixbrl_zip(zip_c, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts_c, facts_a)

        res_a = extract_segments_from_xbrl_zip(str(zip_a), period="2027-02-28", quarter="2Q")
        res_b = extract_segments_from_xbrl_zip(str(zip_b), period="2027-02-28", quarter="2Q")
        res_c = extract_segments_from_xbrl_zip(str(zip_c), period="2027-02-28", quarter="2Q")

        assert len(res_a) == 1
        assert len(res_b) == 1
        assert len(res_c) == 1
        
        # 順序に依存せず、かつ上書きされずに期待される累計の200Mがすべてで一意に得られること
        assert res_a[0].sales == 200000000
        assert res_b[0].sales == 200000000
        assert res_c[0].sales == 200000000

    def test_case_10_and_11_sales_profit_context_consistency(self, tmp_path):
        # 10. salesとprofitのcontext一致 -> 同じ context_ref であること
        # 11. 累計salesだけ・単独profitだけ -> 別contextを混ぜない
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"}, # 6M YTD
            {"id": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-06-01", "end": "2026-08-31"}, # 3M Q
        ]
        # sales は累計 (200M) のみに存在し、profit は単独 (10M) のみに存在するケース
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
            {"name": "jppfs_cor:operatingincome", "context": "CurrentQuarterDuration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "10,000,000"},
        ]
        zip_p = tmp_path / "test_consistency.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        # 2Q (期待値: 累計 180日 が優先採用される)
        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q")
        assert len(res) == 1
        
        # 採用された context_ref は CurrentYearYTD_Duration のはず
        # したがって、sales は 200,000,000 が取得でき、profit は None になるべき (異なるcontextの単独profitと混ぜない)
        assert res[0].sales == 200000000
        assert res[0].profit is None

    def test_case_12_exceeding_duration_difference(self, tmp_path):
        # 12. 40日を超えるduration差 -> 対象外として除外
        contexts = [
            # 2Q期待値 180日 に対し、230日 (差が50日) の context
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-01-10", "end": "2026-08-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
        ]
        zip_p = tmp_path / "test_exceed_duration.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        # 2Q でパースするが、差が50日 (> 40日) なので無視されて結果は空になるべき
        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q")
        assert len(res) == 0

    def test_raw_json_context_evidence_integrity(self, tmp_path):
        # raw_json 内の証拠領域チェック
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
        ]
        zip_p = tmp_path / "test_evidence.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q", include_context_evidence=True)
        assert len(res) == 1
        
        raw_json = res[0].raw_json
        assert raw_json is not None
        assert "_context_evidence" in raw_json
        
        evidence = raw_json["_context_evidence"]
        assert evidence["context_ref"] == "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember"
        assert evidence["context_start"] == "2026-03-01"
        assert evidence["context_end"] == "2026-08-31"
        assert evidence["duration_days"] == 183
        assert evidence["current_or_previous"] == "current"
        assert evidence["quarter"] == "2Q"
        assert "selection_reason" in evidence
        assert "duration_diff=3" in evidence["selection_reason"]


class TestSegmentExtractionDetailed:

    def test_zip_not_found(self):
        res = extract_segments_from_xbrl_zip_detailed("nonexistent_zip_file.zip")
        assert res.status == "zip_not_found"
        assert res.segments == []
        assert res.reason == "zip_file_not_found"

    def test_quarter_unresolved(self, tmp_path):
        zip_p = tmp_path / "unknown_quarter.zip"
        create_dummy_ixbrl_zip(zip_p, "四半期情報が含まれていないタイトルです", [], [])

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28")
        assert res.status == "quarter_unresolved"
        assert res.segments == []
        assert res.title_quarter == "UNKNOWN"

    def test_segment_source_unavailable(self, tmp_path):
        zip_p = tmp_path / "no_seg_files.zip"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr("ixbrl-main-only.htm", "<html><body><ix:nonNumeric name='jpcrp_cor:DocumentTitle'>2027年2月期 第2四半期決算短信［日本基準］(連結)</ix:nonNumeric></body></html>")

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "segment_source_unavailable"
        assert res.segments == []
        assert res.reason == "no_segment_files_in_zip"

    def test_context_unresolved(self, tmp_path):
        zip_p = tmp_path / "empty_context.zip"
        main_html = "<html><body><ix:nonNumeric name='jpcrp_cor:DocumentTitle'>2027年2月期 第2四半期決算短信［日本基準］(連結)</ix:nonNumeric></body></html>"
        sg_html = "<html><body>セグメント情報</body></html>"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr("ixbrl-main-7601.htm", main_html)
            zf.writestr("ixbrl-qcsg-7601.htm", sg_html)

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "context_unresolved"
        assert res.segments == []
        assert res.reason == "global_context_map_empty"

    def test_date_guard_skip(self, tmp_path):
        contexts = [
            {"id": "CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-01-10", "end": "2026-10-15"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
        ]
        zip_p = tmp_path / "date_guard_skip.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "date_guard_skip"
        assert res.segments == []
        assert res.date_guard_status == "SKIP"

    def test_parse_error(self, tmp_path):
        zip_p = tmp_path / "corrupted.zip"
        with open(zip_p, "w") as f:
            f.write("not a zip file content")

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "parse_error"
        assert res.segments == []
        assert "bad_zip_file" in res.reason

    def test_success_with_rows(self, tmp_path):
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "200,000,000"},
        ]
        zip_p = tmp_path / "success_rows.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "success_with_rows"
        assert len(res.segments) == 1
        assert res.segments[0].sales == 200000000

    def test_success_empty(self, tmp_path):
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        facts = []
        zip_p = tmp_path / "success_empty.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "success_empty"
        assert res.segments == []
        assert res.date_guard_status == "PASS"
        assert res.candidate_file_count == 1
        assert res.parsed_file_count == 1

    def test_legacy_api_parity(self, tmp_path):
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "150,000,000"},
        ]
        zip_p = tmp_path / "parity.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        detailed_res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        legacy_res = extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q")

        assert len(detailed_res.segments) == len(legacy_res)
        assert detailed_res.segments[0].sales == legacy_res[0].sales
        assert detailed_res.segments[0].raw_segment_name == legacy_res[0].raw_segment_name

    def test_legacy_api_calls_detailed_once(self, tmp_path):
        contexts = [
            {"id": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "start": "2026-03-01", "end": "2026-08-31"},
        ]
        facts = [
            {"name": "jppfs_cor:netsales", "context": "CurrentYearYTD_Duration_tse-acedjpfr-25900DomesticBeverageBusinessReportableSegmentsMember", "val": "150,000,000"},
        ]
        zip_p = tmp_path / "call_count.zip"
        create_dummy_ixbrl_zip(zip_p, "2027年2月期 第2四半期決算短信［日本基準］(連結)", contexts, facts)

        from unittest.mock import patch
        with patch("src.segment.xbrl_segment_extractor.extract_segments_from_xbrl_zip_detailed") as mock_detailed:
            mock_detailed.return_value = SegmentExtractionResult(status="success_with_rows", segments=[])
            extract_segments_from_xbrl_zip(str(zip_p), period="2027-02-28", quarter="2Q")
            mock_detailed.assert_called_once()

    def test_mixed_case_k_partially_failed(self, tmp_path):
        main_html = """
        <html>
        <body>
          <ix:nonNumeric name="jpcrp_cor:DocumentTitle">2027年2月期 第2四半期決算短信［日本基準］(連結)</ix:nonNumeric>
          <xbrli:context id="CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember">
            <xbrli:period>
              <xbrli:startDate>2026-03-01</xbrli:startDate>
              <xbrli:endDate>2026-08-31</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
        </body>
        </html>
        """
        sg_html_a = """
        <html>
        <body>
          <ix:nonfraction name="jppfs_cor:netsales" contextref="CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember" unitref="JPY" decimals="0" scale="6">120,000,000</ix:nonfraction>
        </body>
        </html>
        """
        zip_p = tmp_path / "mixed_k.zip"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr("ixbrl-main-7601.htm", main_html)
            zf.writestr("ixbrl-qcsg-filea.htm", sg_html_a)
            zf.writestr("ixbrl-qcsg-fileb.htm", "broken html or content")

        from unittest.mock import patch
        with patch("src.segment.xbrl_segment_extractor._extract_ixbrl_segment_data") as mock_extract:
            mock_extract.side_effect = [
                {("DomesticMember", "current"): {"sales": 120000000, "context_ref": "CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember"}},
                Exception("mock segment data parse error")
            ]
            res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")

        assert res.status == "parse_error"
        assert res.reason == "partial_parsing_failure"
        assert len(res.segments) == 1
        assert res.segments[0].sales == 120000000
        assert res.candidate_file_count == 2
        assert res.parsed_file_count == 1

    def test_mixed_case_l_partially_skipped(self, tmp_path):
        main_html = """
        <html>
        <body>
          <ix:nonNumeric name="jpcrp_cor:DocumentTitle">2027年2月期 第2四半期決算短信［日本基準］(連結)</ix:nonNumeric>
          <xbrli:context id="CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember">
            <xbrli:period>
              <xbrli:startDate>2026-03-01</xbrli:startDate>
              <xbrli:endDate>2026-08-31</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
          <xbrli:context id="MismatchDuration">
            <xbrli:period>
              <xbrli:startDate>2026-01-10</xbrli:startDate>
              <xbrli:endDate>2026-08-31</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
        </body>
        </html>
        """
        zip_p = tmp_path / "mixed_l.zip"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr("ixbrl-main-7601.htm", main_html)
            zf.writestr("ixbrl-qcsg-filea.htm", "<html><body></body></html>")
            zf.writestr("ixbrl-qcsg-fileb.htm", "<html><body></body></html>")

        from unittest.mock import patch
        with patch("src.segment.xbrl_segment_extractor._extract_ixbrl_segment_data") as mock_extract:
            mock_extract.side_effect = [{}, {}]

            res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")

        assert res.status == "success_empty"
        assert res.date_guard_status == "PASS"
        assert res.candidate_file_count == 2
        assert res.parsed_file_count == 2

    def test_mixed_case_m_all_success_empty(self, tmp_path):
        main_html = """
        <html>
        <body>
          <ix:nonNumeric name="jpcrp_cor:DocumentTitle">2027年2月期 第2四半期決算短信［日本基準］(連結)</ix:nonNumeric>
          <xbrli:context id="CurrentYearYTD_DomesticBeverageBusinessReportableSegmentsMember">
            <xbrli:period>
              <xbrli:startDate>2026-03-01</xbrli:startDate>
              <xbrli:endDate>2026-08-31</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
        </body>
        </html>
        """
        zip_p = tmp_path / "mixed_m.zip"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr("ixbrl-main-7601.htm", main_html)
            zf.writestr("ixbrl-qcsg-filea.htm", "<html><body></body></html>")
            zf.writestr("ixbrl-qcsg-fileb.htm", "<html><body></body></html>")

        res = extract_segments_from_xbrl_zip_detailed(str(zip_p), period="2027-02-28", quarter="2Q")
        assert res.status == "success_empty"
        assert res.segments == []
        assert res.candidate_file_count == 2
        assert res.parsed_file_count == 2

