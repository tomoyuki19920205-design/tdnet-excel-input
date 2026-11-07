from pathlib import Path
import json
import pytest
from src.segment.edinet_segment_extractor import extract_edinet_segments
from src.segment.edinet_canonical_bridge import edinet_result_to_canonical_segments

FIX=Path(__file__).parent/'fixtures/segment_partials'

@pytest.mark.parametrize('doc,count,expected',[
    ('S100U1X7',3,{'その他':(305,54)}),
    ('S100TWBK',5,{'建設事業/日本':(74698,2035),'不動産事業/日本':(1198,411),'不動産事業/東南アジア':(2,0)}),
    ('S100TM93',6,{'日本':(238927,72880),'その他事業':(59969,48484)}),
    ('S100TS7P',7,{'ゲーム&ネットワークサービス':(3538533,250006),'金融':(878532,318118)}),
    ('S100YOTI',8,{'アフリカ':(1373241,36371),'その他':(7140,968)}),
    ('S100T6GP',2,{'基板事業':(908.015,-104.041),'不動産賃貸事業':(150.479,-13.306)}),
    ('S100OHK9',7,{'BPO・ICT':(112025,12664),'不動産・その他':(49506,6182)}),
    ('S100TVT1',6,{'食品加工機械製造販売事業/北米・南米':(3663.599,158.887),'食品製造販売事業/北米・南米':(13247.238,612.169)}),
    ('S100TTWW',9,{'半導体関連事業/日本':(6916,2339),'医療・健康機器事業/日本':(5429,2906),'計測・計量機器事業/日本':(16325,1232)}),
])
def test_original_multilevel_wrapped_and_vertical_tables(doc,count,expected):
    source=json.loads((FIX/(doc+'_source.json')).read_text(encoding='utf-8'))
    result=extract_edinet_segments(str(FIX/(doc+'.zip')),**{k:source[k] for k in ('ticker','period','quarter','period_role')})
    assert result.status=='ok',result.review_hint
    rows=edinet_result_to_canonical_segments(result)
    assert len(rows)==count
    byname={r['segment_name']:r for r in rows}
    assert len(byname)==count
    for name,values in expected.items():assert (byname[name]['sales'],byname[name]['profit'])==values
    assert all(r['segment_type'] in ('ordinary','other') for r in rows)
    assert all(r['sales_qtd'] is None and r['profit_qtd'] is None for r in rows)
    assert not any(r['segment_name'] in ('計','合計','報告セグメント計','事業計','調整額') for r in rows)
