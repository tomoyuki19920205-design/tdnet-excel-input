from src.segment.business_disclosure_evidence import business_segment_omission_html,attachment_business_omissions
from pathlib import Path
from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip_detailed

def test_real_plain_note_binds_current_dei_and_preserves_omission_statement():
    path=Path(__file__).parent/'fixtures/segment_partials/business_omission_20230112588032.zip'
    result=extract_segments_from_xbrl_zip_detailed(str(path),period='2023-02-28',quarter='3Q')
    assert result.status=='success_empty' and not result.segments
    assert len(result.omission_evidence)==1
    proof=result.omission_evidence[0]
    assert (proof['ticker'],proof['period'],proof['quarter'])==('8289','2023-02-28','3Q')
    assert (proof['period_start'],proof['period_end'])==('2022-03-01','2022-11-30')
    assert proof['kind']=='business_segment_omission' and proof['omission_basis']=='immateriality'
    from types import SimpleNamespace
    from lib.backfill.worker_v2 import _try_xbrl_source
    candidate=_try_xbrl_source(str(path),None,SimpleNamespace(ticker='8289',expected_period='2023-02-28',expected_quarter='3Q',title='2023年2月期 第3四半期決算短信'),None,'fixture',None,{},retry_xbrl=0,timeout_xbrl=10,sleep_fn=lambda _:None)
    assert candidate.skip_reason=='valid_no_segments:success_empty'
    assert candidate.omission_evidence==result.omission_evidence

def test_explicit_immaterial_business_note_is_not_labelled_single_segment():
    note='当社グループは、「小売事業」の割合が高く、開示情報としての重要性が乏しいことから、セグメント情報の記載を省略しております。'
    proof=business_segment_omission_html(note)
    assert proof['kind']=='business_segment_omission' and proof['omission_basis']=='immateriality'
    assert proof['statement']==note

def test_explicit_current_quantitative_note_is_supported():
    assert business_segment_omission_html('当第1四半期の定量セグメント情報は省略する。')['kind']=='business_segment_omission'

def test_prior_omission_does_not_hide_current_table():
    html='<p>前連結会計年度(自2024年1月1日至2024年12月31日)</p><p>セグメント情報の記載を省略しております。</p><p>当連結会計年度(自2025年1月1日至2025年12月31日)</p><table><tr><td>セグメント利益</td><td>3</td></tr></table>'
    assert business_segment_omission_html(html) is None
    assert business_segment_omission_html(html,period_role='previous')['period_heading']

def test_region_exemption_and_prior_unbound_statement_prove_nothing():
    assert business_segment_omission_html('地域別セグメント情報は省略しております。') is None
    assert business_segment_omission_html('前連結会計年度のセグメント情報は省略しております。') is None
    assert business_segment_omission_html('【関連情報】単一セグメントのため省略しております。') is None

def test_plain_attachment_isolates_business_note_from_pl_and_next_section():
    html='<h3>損益計算書</h3><table><tr><td>利益</td><td>100</td></tr></table><h3>（セグメント情報等）</h3><p>【セグメント情報】</p><p>重要性が乏しいことから、セグメント情報の記載を省略しております。</p><h3>一株当たり情報</h3><table><tr><td>EPS</td><td>2</td></tr></table>'
    proofs=attachment_business_omissions(html)
    assert len(proofs)==1 and proofs[0]['omission_basis']=='immateriality'
    assert not attachment_business_omissions(html.replace('<p>重要性','<table><tr><td>3</td></tr></table><p>重要性'))
