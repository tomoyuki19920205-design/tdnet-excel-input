from src.fetcher import _fetch_via_api


class _Response:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    text = "[]"

    def json(self):
        return [{
            "Ttitle": "2026年12月期 第2四半期決算短信〔日本基準〕（連結）",
            "Tcode": "50720",
            "Tname": "Example",
            "TdocURL": "https://www.release.tdnet.info/inbs/140120260807123456.pdf",
            "Ttime": "2026-08-07 15:00",
        }]

    def raise_for_status(self):
        return None


class _Session:
    def get(self, *args, **kwargs):
        return _Response()


def test_api_items_infer_xbrl_url_when_feed_omits_it():
    items = _fetch_via_api(session=_Session())
    assert len(items) == 1
    assert items[0].xbrl_url == (
        "https://www.release.tdnet.info/inbs/081220260807123456.zip"
    )
