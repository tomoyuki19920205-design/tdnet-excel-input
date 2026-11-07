from src.segment.business_identity import business_identity_flags, normalize_business_header, raw_business_identity_flags

def test_exact_printed_header_survives_taxonomy_rename_and_typography():
    columns = [{'role': 'segment', 'label': '物流\nサービス'}, {'role': 'segment', 'label': '物流 サービス（注）１'}]
    assert business_identity_flags(columns)['business_key'] == '物流サービス'
    assert normalize_business_header('ＡＩ データセンター') == 'aiデータセンター'

def test_distinct_businesses_hierarchy_and_metric_columns_do_not_merge():
    assert normalize_business_header('食品') != normalize_business_header('食品卸売')
    assert normalize_business_header('建設 / 日本') != normalize_business_header('開発 / 日本')
    assert business_identity_flags([{'role': 'segment', 'label': '食品'}, {'role': 'segment', 'label': '物流'}]) == {}
    for column in ({'role': 'total', 'label': '計'}, {'role': 'unresolved', 'label': ''}, {'role': 'segment', 'label': '営業利益'}):
        assert business_identity_flags([column]) == {}

def test_unverified_metric_prevents_identity_but_zero_and_negative_are_valid():
    raw = {'sales_fact': {'million_value': 0, 'column': {'role': 'segment', 'label': 'その他'}}, 'profit_fact': {'million_value': -1}}
    assert raw_business_identity_flags(raw) == {}
    raw['profit_fact']['column'] = {'role': 'segment', 'label': 'その他'}
    assert raw_business_identity_flags(raw)['business_key'] == 'その他'

def test_literal_identity_uses_original_cell_coordinates():
    raw = {'table_facts': {'profit': {'status': 'disclosed_numeric', 'segment_label': '電子', 'textblock_context': 'CurrentYTDDuration', 'table_index': 1, 'column_index': 2}}}
    flags = raw_business_identity_flags(raw)
    assert flags['business_key'] == '電子'
    assert flags['business_key_evidence'][0]['column_index'] == 2

def test_real_1301_originals_retain_exact_identity_across_taxonomy_names():
    from pathlib import Path
    from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed
    from src.segment.profit_basis import segment_flags
    fixture = Path(__file__).parent / 'fixtures' / 'segment_partials'
    expected = [('20250131560451', '3Q', 'Distribution Service', 2144, 225),
                ('20250509539155', 'FY', 'Logistics Business', 2858, 289)]
    definitions = []
    for doc, quarter, name, sales, profit in expected:
        result = extract_segments_from_xbrl_zip_detailed(str(fixture / ('business_identity_' + doc + '.zip')),
            period='2025-03-31', quarter=quarter, include_context_evidence=True)
        assert result.status == 'success_with_rows'
        rows = [r for r in result.segments if r.period == '2024-03-31']
        row = next(r for r in rows if r.raw_segment_name == name)
        assert (row.sales, row.profit) == (sales, profit)
        flags = segment_flags(row.raw_json)
        assert flags['business_key'] == '物流サービス'
        assert flags['profit_basis'] == 'operating_profit'
        assert flags['sales_basis'] == 'including_intersegment'
        definitions.append(sorted(segment_flags(r.raw_json)['business_key'] for r in rows))
    assert definitions[0] == definitions[1] and len(definitions[0]) == 5
