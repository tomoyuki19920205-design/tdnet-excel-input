"""Production preflight must not mistake an HTTP error for no report."""

import pytest
import requests

from tools.preflight_ny_market_runtime import _resilient_select, _strict_select


def test_strict_select_raises_on_connection_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.exceptions.ProxyError("proxy unavailable")

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(requests.exceptions.ProxyError):
        _strict_select("canonical_ny_market_reports", params={}, config={
            "rest_url": "https://example.test/rest/v1", "headers": {},
        })


def test_strict_select_accepts_real_empty_response(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: Response())
    assert _strict_select("canonical_ny_market_reports", params={}, config={
        "rest_url": "https://example.test/rest/v1", "headers": {},
    }) == []


def test_transient_dns_failure_retries_then_preserves_real_empty_result(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def request(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("temporary DNS failure")
        return Response()

    monkeypatch.setattr(requests, "get", request)
    monkeypatch.setattr("tools.preflight_ny_market_runtime.time.sleep", lambda _s: None)
    assert _resilient_select("canonical_ny_market_reports", params={}, config={
        "rest_url": "https://example.test/rest/v1", "headers": {},
    }) == []
    assert len(calls) == 3
