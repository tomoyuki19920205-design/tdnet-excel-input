from copy import deepcopy
from datetime import date
import hashlib
import pytest
from lib.ny_market_deadline import counts,notice,CONTRACT,REVIEW_CONTRACT
from lib.ny_market import validate_payload,NYMarketValidationError,report_markdown_sha256
from tools.build_ny_market_deadline_payload import build_deadline
from tests.test_ny_market_provisional import _synthetic_packet
from tests.ny_market_quality_fixture import payload as full_payload
from tools.prepare_ny_market_review import prepare

def candidates(n=396):
    return [dict(ticker=f'T{i}',importance_rank=i+1,priority='high' if i<10 else 'normal',
                 status='pending',reason_code='awaiting_verification',reason='未確認') for i in range(n)]

def progress(rows):
    return dict(contract_version=CONTRACT,candidates=rows,counts=counts(rows),as_of_utc='2026-09-01T22:45:00+00:00')

def update(tmp_path):
    raw=b'Revenue increased to 100 million on September 1, 2026.'
    path=tmp_path/'release.txt';path.write_bytes(raw)
    sha=hashlib.sha256(raw).hexdigest()
    return dict(id='company-results',title='決算',section='確認済みニュース',summary='売上高は1億ドルとなりました。',
                evidence=[dict(url='https://example.org/release',published_at='2026-09-01T21:00:00+00:00',
                    path=str(path),sha256=sha,text_path=str(path),text_sha256=sha,quote=raw.decode())])

def test_396_pending_candidates_publish_verified_news_without_false_completion(tmp_path):
    rows=candidates();p=progress(rows)
    result=build_deadline(_synthetic_packet(),date(2026,9,2),'open',p,[update(tmp_path)],
                          [{'field':'重要候補','reason':'確認が残っています'}])
    assert result['quality_status']=='provisional'
    assert '未確認396件（重要候補10件）' in result['report_markdown']
    assert '売上高は1億ドル' in result['report_markdown']
    assert rows==candidates()  # no silent exclusions
    assert len(result['sources'])==2
    with pytest.raises(NYMarketValidationError):validate_payload({**result,'quality_status':'complete'})

def test_progress_counts_and_priority_cannot_be_forged():
    rows=candidates();rows[0]['priority']='normal'
    with pytest.raises(ValueError,match='high priority'):counts(rows)
    rows=candidates();rows[0]['status']='excluded'
    with pytest.raises(ValueError,match='decision evidence'):counts(rows)

def test_resume_preserves_completed_decisions_and_unknown_candidates():
    raw=[{'ticker':f'T{i}'} for i in range(396)]
    raw[0]['discovery_routes']=['https://example.org/calendar']
    initial=prepare(raw,[],'2026-09-01T22:45:00Z')
    decision={'ticker':'T0','status':'included','reason_code':'verified_earnings','reason':'一次資料で確認',
              'decision_evidence':[{'url':'https://example.org/release'}]}
    second=prepare(raw,[decision],'2026-09-01T22:45:00Z',initial)
    resumed=prepare(raw,[],'2026-09-01T22:45:00Z',second)
    assert resumed['candidates'][0]['discovery_source_url']=='https://example.org/calendar'
    assert resumed['counts']==dict(discovered=396,confirmed=1,adopted=1,excluded=0,pending=395,important_pending=9)
    with pytest.raises(ValueError,match='discard'):prepare(raw[1:],[],'2026-09-01T22:45:00Z',resumed)


def test_deadline_update_preserves_unresolved_market_evidence(tmp_path):
    packet=_synthetic_packet()
    packet['unresolved_items']=[{'field':'市場データ','reason':'価格根拠の照合未完了'}]
    result=build_deadline(packet,date(2026,9,2),'open',progress(candidates()),[update(tmp_path)],
                          [{'field':'企業調査','reason':'一次資料の確認未完了'}])
    assert result['unresolved_items']==packet['unresolved_items']+[{'field':'企業調査','reason':'一次資料の確認未完了'}]


def test_direct_publish_cannot_shift_historical_cutoff(tmp_path):
    from lib.ny_market import render_provisional_markdown
    result=build_deadline(_synthetic_packet(),date(2026,9,2),'open',progress(candidates()),[update(tmp_path)],
                          [{'field':'企業調査','reason':'未完了'}])
    result['review_progress']['as_of_utc']='2026-09-03T00:00:00Z'
    result['verified_updates'][0]['evidence'][0]['published_at']='2026-09-02T23:00:00Z'
    result['report_markdown']=render_provisional_markdown(result)
    result['report_delivery']['sha256']=report_markdown_sha256(result['report_markdown'])
    with pytest.raises(NYMarketValidationError,match='morning deadline'):validate_payload(result)

def test_future_source_and_tampered_raw_are_rejected(tmp_path):
    u=update(tmp_path);u['evidence'][0]['published_at']='2026-09-03T00:00:00Z'
    args=(_synthetic_packet(),date(2026,9,2),'open',progress(candidates()),[u],[{'field':'x','reason':'未確認'}])
    with pytest.raises(ValueError,match='future source'):build_deadline(*args)
    u['evidence'][0]['published_at']='2026-09-01T21:00:00Z'
    (tmp_path/'release.txt').write_text('changed')
    with pytest.raises(ValueError,match='integrity'):build_deadline(*args)

def test_complete_contract_rejects_any_pending_candidate():
    p=full_payload();review=p['after_hours_candidate_review']
    review['contract_version']=REVIEW_CONTRACT
    rows=review['candidates']
    for c in rows:
        c['priority']='high';c['decision_evidence']=[{'url':c['discovery_source_url']}]
    # Fill up to ten resolved important candidates, then retain 386 unreviewed.
    template=deepcopy(rows[-1])
    for i in range(len(rows),396):
        c=deepcopy(template);c.update(ticker=f'P{i}',importance_rank=i+1,priority='high' if i<10 else 'normal',
            status='excluded' if i<10 else 'pending',reason_code='outside_session' if i<10 else 'awaiting_verification',reason='dated evidence' if i<10 else '未確認')
        rows.append(c)
    review['counts']=counts(rows);review['discovered_candidate_count']=len(rows);review['coverage_status']='incomplete'
    p['report_markdown']+='\n\n'+notice(review['counts'])
    p['report_delivery']['sha256']=report_markdown_sha256(p['report_markdown'])
    with pytest.raises(NYMarketValidationError,match='coverage_status|pending after-hours candidates'):
        validate_payload(p)
    review['coverage_status']='quiet_day'
    with pytest.raises(NYMarketValidationError,match='pending after-hours candidates'):
        validate_payload(p)
