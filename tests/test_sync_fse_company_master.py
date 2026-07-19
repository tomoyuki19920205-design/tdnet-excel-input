from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sync_fse_company_master as subject


def table(code: str, market: str, name: str) -> str:
    return f'<table><tr><th>コード</th><td>{code}</td><th>市場区分</th><td>{market}</td></tr><tr><th>会社名</th><td><img alt="{name}"></td></tr></table>'


def all_required(extra: str = "") -> str:
    return "".join(table(code, "本則" if code != "3824" else "Q-Board", name) for code, name in subject.REQUIRED.items()) + "".join(table(str(1000+i), "本則", f"会社{i}") for i in range(4)) + extra


def test_primary_parses_official_name_and_alpha_code():
    companies = subject.primary_companies(all_required(table("231A", "Q-Board", "Cross Eホールディングス")))
    assert {x.ticker_code for x in companies} >= {"1771", "231A"}
    assert next(x for x in companies if x.ticker_code == "1771").name_ja == "日本乾溜工業"


def test_clean_text_decodes_and_does_not_shorten_name():
    assert subject.clean_text("  サイタ　ホールディングス &amp; Co. \n") == "サイタ ホールディングス & Co."
    assert subject.clean_text("日本乾溜工業") == "日本乾溜工業"


def test_primary_rejects_empty_name_and_duplicate():
    with pytest.raises(subject.ValidationError):
        subject.primary_companies(all_required('<table><tr><th>コード</th><td>7777</td><th>市場区分</th><td>本則</td></tr><tr><th>会社名</th><td><img alt=""></td></tr></table>'))
    with pytest.raises(subject.ValidationError):
        subject.primary_companies(all_required(table("1771", "本則", "重複")))


def test_plan_preserves_name_en_and_skips_unchanged(monkeypatch):
    rows = [subject.Company("1771", "日本乾溜工業", "本則", subject.PRIMARY_URL), subject.Company("231A", "Cross Eホールディングス", "Q-Board", subject.PRIMARY_URL)]
    existing = {"1771": {"ticker_code": "1771", "name_ja": "日本乾溜", "name_en": "KEEP", "is_active": True}, "231A": {"ticker_code": "231A", "name_ja": "Cross Eホールディングス", "name_en": "KEEP", "is_active": True}}
    changes, stats = subject.plan(rows, existing, {"rest_url": "", "headers": {}})
    assert changes == [{"ticker_code": "1771", "name_ja": "日本乾溜工業"}]
    assert stats["name_ja_update"] == 1 and stats["unchanged"] == 1


def test_plan_inserts_and_pro_requires_viewer_data(monkeypatch):
    rows = [subject.Company("1999", "サイタホールディングス", "本則", subject.PRIMARY_URL), subject.Company("342A", "プロ会社", "Fukuoka PRO Market", subject.PRIMARY_URL)]
    monkeypatch.setattr(subject, "viewer_data_exists", lambda _c, ticker: ticker == "342A")
    changes, stats = subject.plan(rows, {}, {"rest_url": "", "headers": {}})
    assert {x["ticker_code"] for x in changes} == {"1999", "342A"}
    assert stats["insert"] == 2
