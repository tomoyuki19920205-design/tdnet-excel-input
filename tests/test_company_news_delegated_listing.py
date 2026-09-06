"""Offline tests for delegated JSONP listing reconstruction and parsing."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qsl, urlsplit

import pytest

from tools.company_news_delegated_listing import (
    DelegatedListingError,
    build_xj_document_inventory,
    derive_official_window_coverage,
    find_inventory_by_date,
    parse_jsonp,
    reconstruct_xj_jsonp_request,
    summarize_inventory,
)


CALLBACK = "XJStorageClass.jsonpReq.jsonpCallback_phase19s_6444"
LIB = '''
class XJStorageClass {
  url = "https://www.xj-storage.jp/public-list/GetList2.aspx";
  getData(e = {}) {
    const t = { company: this.params.company, doctype: "", output: "json", len: "0",
      callback: `XJStorageClass.jsonpReq.${r}`, ...e };
    const a = this._buildQuery(t); const s = `${this.url}?${a}`;
    const script = document.createElement("script");
  }
  _buildQuery(e) { return Object.keys(e).map((r) => `${encodeURIComponent(r)}=${encodeURIComponent(e[r])}`).join("&"); }
}
'''
PAGE = '''
const xjParams = { company: 'AS02369' };
const docInfoList = [
 {label: 'Timely', doctype: '0,5'},
 {label: 'Results', doctype: '1,2'},
];
const doctype = docInfoList.map(docInfo => docInfo.doctype).join(',');
XJStorage.getData({doctype});
'''


def reconstructed():
    return reconstruct_xj_jsonp_request(LIB, PAGE, callback=CALLBACK)


def test_request_reconstruction_uses_one_all_category_request_and_js_order():
    result = reconstructed()
    assert result.endpoint == "https://www.xj-storage.jp/public-list/GetList2.aspx"
    assert result.method == "GET"
    assert parse_qsl(urlsplit(result.final_url).query) == [
        ("company", "AS02369"), ("doctype", "0,5,1,2"),
        ("output", "json"), ("len", "0"), ("callback", CALLBACK),
    ]
    assert all(item.source == "DERIVED_FROM_JS" for item in result.parameters)


@pytest.mark.parametrize("old,new,field", [
    ('output: "json"', 'output: "xml"', "output"),
    ('len: "0"', 'len: someUnknown', "len"),
    ('callback: `XJStorageClass.jsonpReq.${r}`', 'callback: unknown', "callback"),
])
def test_unknown_mandatory_parameter_fails_before_request(old, new, field):
    with pytest.raises(DelegatedListingError, match=field):
        reconstruct_xj_jsonp_request(LIB.replace(old, new), PAGE, callback=CALLBACK)


def test_missing_doctype_aggregation_fails_closed():
    with pytest.raises(DelegatedListingError, match="doctype"):
        reconstruct_xj_jsonp_request(LIB, PAGE.replace(".join(',')", ".join(';')"), callback=CALLBACK)


@pytest.mark.parametrize("callback", ["alert(1)", "other.callback", "XJStorageClass.jsonpReq.bad-name"])
def test_callback_is_bounded_to_xj_contract(callback):
    with pytest.raises(DelegatedListingError, match="callback"):
        reconstruct_xj_jsonp_request(LIB, PAGE, callback=callback)


def test_jsonp_parser_accepts_exact_callback_without_eval():
    result = parse_jsonp((CALLBACK + '({"items":[]});').encode(), callback=CALLBACK)
    assert result == {"items": []}


@pytest.mark.parametrize("payload", [
    'wrong({"items":[]})',
    CALLBACK + '({"items":[]});alert(1)',
    CALLBACK + '({items:[]})',
    CALLBACK + '([])',
    CALLBACK + '({"other":[]})',
])
def test_jsonp_parser_fails_closed(payload):
    with pytest.raises(DelegatedListingError):
        parse_jsonp(payload, callback=CALLBACK)


def sample_payload():
    return {"items": [
        {"publishDate": "2026/08/27 15:00:00", "fiscalYearEnd": "202612",
         "title": "Second-quarter briefing", "disclosureCode": "1",
         "files": [{"type": "PDF-GENERAL", "url": "https://example.test/briefing.pdf", "size": 100, "page": 5}]},
        {"publishDate": "2026/08/20 12:00:00", "title": "Old", "disclosureCode": "0", "files": []},
        {"publishDate": "unknown", "title": "Unknown", "disclosureCode": "999", "files": []},
    ]}


def test_inventory_preserves_fields_categories_and_fixed_window():
    inventory = build_xj_document_inventory(
        sample_payload(), category_map=(("Timely", "0,5"), ("Results", "1,2")),
        window_from=date(2026, 8, 25), window_to=date(2026, 8, 31))
    summary = summarize_inventory(inventory)
    assert summary == {
        "total": 3, "category_counts": {"Results": 1, "Timely": 1, "UNKNOWN": 1},
        "in_window": 1, "out_of_window": 1, "unknown_date": 1,
    }
    assert inventory[0]["publication_precision"] == "DATE_ONLY"
    assert inventory[0]["canonical_document_candidates"][0]["type"] == "PDF-GENERAL"
    assert find_inventory_by_date(inventory, date(2026, 8, 27))[0]["title"] == "Second-quarter briefing"


def test_invalid_dates_do_not_create_fake_timestamp():
    inventory = build_xj_document_inventory(
        {"items": [{"publishDate": "2026/02/30 00:00:00"}]}, category_map=(),
        window_from=date(2026, 8, 25), window_to=date(2026, 8, 31))
    assert inventory[0]["publication_date"] is None
    assert inventory[0]["publication_precision"] == "UNKNOWN"


def test_coverage_complete_even_when_confirmed_listing_is_empty():
    assert derive_official_window_coverage(jsonp_parsed=True, inventory_built=True) == "COMPLETE"
    assert derive_official_window_coverage(jsonp_parsed=False, inventory_built=False) == "BLOCKED"
    assert derive_official_window_coverage(
        jsonp_parsed=True, inventory_built=True, required_listing_gaps=("another listing",)) == "PARTIAL"


def test_request_artifact_contains_hashes_and_no_search_or_model_data():
    value = reconstructed().to_dict()
    encoded = json.dumps(value)
    assert len(value["endpoint_source_sha256"]) == 64
    assert len(value["parameter_source_sha256"]) == 64
    assert "SearchRecord" not in encoded
    assert "model" not in encoded.lower()
