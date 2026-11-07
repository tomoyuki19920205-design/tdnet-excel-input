from unittest.mock import Mock
from src.jquants import adapter

def test_official_file_kind_query_supports_pdf_and_xbrl(monkeypatch):
    monkeypatch.setattr(adapter,'_build_headers',lambda:{})
    response=Mock();response.json.return_value={'files':{'pdf':'https://example.invalid/pdf','xbrl':'https://example.invalid/xbrl'}}
    session=Mock();session.get.return_value=response
    result=adapter.get_file_url('20261104564263','x,g',_session=session)
    assert session.get.call_args.kwargs['params']=={'discNo':'20261104564263','docs':'x,g'}
    assert set(result)=={'pdf','xbrl'}
