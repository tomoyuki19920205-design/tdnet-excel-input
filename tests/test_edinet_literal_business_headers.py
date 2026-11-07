from pathlib import Path
from src.segment.edinet_segment_extractor import extract_edinet_segments
FIX=Path(__file__).parent/'fixtures/segment_partials'

def extract(doc,ticker,period,quarter,role='current'):
    result=extract_edinet_segments(str(FIX/(doc+'.zip')),ticker=ticker,period=period,quarter=quarter,period_role=role)
    assert result.status=='ok',result.review_hint
    return {r.segment_name_norm:r for r in result.segments}

def test_interim_statement_total_is_not_a_business():
    rows=extract('S100X4HO','2334','2026-03-31','2Q')
    assert set(rows)=={'インターネットメディア','AIデータセンター'}
    assert rows['AIデータセンター'].profit==57.5
    assert rows['AIデータセンター'].sales is None

def test_actual_other_with_note_preserves_its_amount_and_loss():
    rows=extract('S100WJDZ','7048','2025-12-31','2Q')
    assert set(rows)=={'OTA','観光IT','その他'}
    assert (rows['その他'].sales,rows['その他'].profit)==(44.917,15.2)
    assert rows['観光IT'].profit==-116.391

def test_line_wrapping_uses_same_business_identity_as_individual_facts():
    rows=extract('S100WCUK','3266','2025-11-30','2Q')
    assert set(rows)=={'アセットマネジメント事業','インベストメントバンク事業'}
    assert (rows['アセットマネジメント事業'].sales,rows['アセットマネジメント事業'].profit)==(328,61)

def test_prior_quarter_statement_total_is_excluded():
    rows=extract('S100TEI4','3082','2023-06-30','3Q','previous')
    assert set(rows)=={'飲食事業','プラットフォームシェアリング事業','その他'}

def test_wrapped_web_business_loss_is_retained():
    rows=extract('S100W9JR','4813','2023-01-31','2Q')
    assert rows['Webプラットフォーム事業'].profit==-154.815

def test_prior_year_literal_other_note_is_not_part_of_identity():
    rows=extract('S100OAG1','2332','2021-03-31','FY','previous')
    assert set(rows)=={'システム開発','インフラサービス','その他'}
