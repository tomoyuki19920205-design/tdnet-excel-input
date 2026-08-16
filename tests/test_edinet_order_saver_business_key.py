from __future__ import annotations

from copy import deepcopy

import pytest

from src.edinet_orders import saver


class Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakePostgREST:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [])
        self.next_id = len(self.rows) + 1

    @staticmethod
    def key(row):
        return (
            row["ticker"], row["period"], row["fiscal_year"],
            row.get("segment_name") or "__ALL__", row.get("source_type", "edinet_yuho"),
        )

    @staticmethod
    def expected_key(params):
        return (
            params["ticker"][3:], params["period"][3:], int(params["fiscal_year"][3:]),
            params["segment_name_key"][3:], params["source_type"][3:],
        )

    def get(self, _endpoint, *, params, **_kwargs):
        key = self.expected_key(params)
        matches = [row for row in self.rows if self.key(row) == key]
        return Response(200, [
            {"id": row["id"], "segment_name": row.get("segment_name"), "segment_name_key": self.key(row)[3]}
            for row in matches
        ])

    def patch(self, _endpoint, *, json, params, **_kwargs):
        key = self.expected_key(params)
        matches = [row for row in self.rows if self.key(row) == key]
        if len(matches) != 1:
            return Response(404, text="not found")
        matches[0].update(deepcopy(json))
        return Response(204)

    def post(self, _endpoint, *, json, **_kwargs):
        row = deepcopy(json)
        if any(self.key(existing) == self.key(row) for existing in self.rows):
            return Response(409, text="duplicate")
        row["id"] = f"id-{self.next_id}"; self.next_id += 1
        self.rows.append(row)
        return Response(201)


def db_row(segment_name, *, ticker="9658"):
    return {
        "id": "existing-id", "ticker": ticker, "company_name": "fixture", "doc_id": "OLD",
        "period": "2024-03-31", "fiscal_year": 2024, "segment_name": segment_name,
        "source_type": "edinet_yuho", "orders_received": 1,
    }


def producer_row(segment_name, *, ticker="9658"):
    return {
        "ticker": ticker, "company_name": "fixture", "doc_id": "NEW", "period": "2024-03-31",
        "fiscal_year": 2024, "segment_name": segment_name, "source_type": "edinet_yuho",
        "source_tag": "semantic_table_v2", "confidence": "high", "source_unit": "million_yen",
        "raw_orders_received": 200, "orders_received": 200,
        "save_candidate": True, "classification": "PASS_SAVE_CANDIDATE",
    }


@pytest.fixture
def install_fake(monkeypatch):
    def factory(rows=None):
        fake = FakePostgREST(rows)
        monkeypatch.setattr(saver, "_get_creds", lambda: ("https://example.test", "key"))
        monkeypatch.setattr(saver.requests, "get", fake.get)
        monkeypatch.setattr(saver.requests, "patch", fake.patch)
        monkeypatch.setattr(saver.requests, "post", fake.post)
        return fake
    return factory


def test_existing_null_all_company_is_updated_not_inserted(install_fake):
    fake = install_fake([db_row(None)])
    stats = saver.save_to_db([producer_row(None)])
    assert (stats["updated"], stats["inserted"], stats["errors"]) == (1, 0, [])
    assert len(fake.rows) == 1


def test_legacy_all_literal_is_same_key_and_canonicalized_to_null(install_fake):
    fake = install_fake([db_row("__ALL__")])
    stats = saver.save_to_db([producer_row(None)])
    assert (stats["updated"], stats["inserted"], stats["errors"]) == (1, 0, [])
    assert fake.rows[0]["id"] == "existing-id"
    assert fake.rows[0]["segment_name"] is None


def test_real_segment_is_updated_by_its_generated_business_key(install_fake):
    fake = install_fake([db_row("建設")])
    stats = saver.save_to_db([producer_row("建設")])
    assert (stats["updated"], stats["inserted"]) == (1, 0)
    assert fake.rows[0]["segment_name"] == "建設"


def test_missing_all_company_key_is_inserted(install_fake):
    fake = install_fake()
    stats = saver.save_to_db([producer_row(None)])
    assert (stats["updated"], stats["inserted"]) == (0, 1)
    assert fake.rows[0]["segment_name"] is None
    assert FakePostgREST.key(fake.rows[0])[3] == "__ALL__"


def test_repeated_save_is_idempotent(install_fake):
    fake = install_fake()
    first = saver.save_to_db([producer_row(None)])
    second = saver.save_to_db([producer_row(None)])
    assert (first["inserted"], first["updated"]) == (1, 0)
    assert (second["inserted"], second["updated"]) == (0, 1)
    assert len(fake.rows) == 1


@pytest.mark.parametrize("sentinel", [None, "__ALL__", "", "None", "none", "NULL", "null"])
def test_all_company_sentinels_share_one_business_key(sentinel):
    assert saver.normalize_segment_name(sentinel) is None
    assert saver.segment_business_key(sentinel) == "__ALL__"


def test_different_real_segments_do_not_collide(install_fake):
    fake = install_fake()
    first = saver.save_to_db([producer_row("建設")])
    second = saver.save_to_db([producer_row("設備")])
    assert (first["inserted"], second["inserted"]) == (1, 1)
    assert {row["segment_name"] for row in fake.rows} == {"建設", "設備"}
