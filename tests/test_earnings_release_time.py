from datetime import datetime, time

import pytest

from lib.backfill.listing_sources.tdnet_html import _strip_trailing_zero
from tools.enrich_earnings_release_time import (
    JST,
    classify_release_session,
    select_primary_candidate,
)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("14:59", "intraday"),
        ("15:00", "intraday"),
        ("15:29", "intraday"),
        ("15:30", "after_close"),
        ("15:31", "after_close"),
    ],
)
def test_tse_1530_boundaries(clock: str, expected: str) -> None:
    hour, minute = map(int, clock.split(":"))
    published = datetime(2026, 7, 15, hour, minute, tzinfo=JST)
    assert classify_release_session(published, time(15, 30)) == expected


def test_missing_time_is_unknown() -> None:
    assert classify_release_session(None, time(15, 30)) == "unknown"


def test_correction_and_formal_statement_selects_formal() -> None:
    candidates = [
        {
            "title": "（訂正）2026年5月期 決算短信〔日本基準〕",
            "published_at": "2026-07-15 15:20",
            "doc_url": "https://example.com/correction.pdf",
        },
        {
            "title": "2026年5月期 決算短信〔日本基準〕",
            "published_at": "2026-07-15 15:30",
            "doc_url": "https://example.com/formal.pdf",
        },
    ]
    selected = select_primary_candidate(candidates)
    assert selected.status == "selected"
    assert selected.candidate == candidates[1]


def test_multiple_formal_candidates_at_same_earliest_time_are_ambiguous() -> None:
    candidates = [
        {
            "title": "2026年5月期 決算短信〔日本基準〕（連結）",
            "published_at": "2026-07-15 15:30",
            "doc_url": "https://example.com/a.pdf",
        },
        {
            "title": "2026年5月期 決算短信〔日本基準〕（非連結）",
            "published_at": "2026-07-15 15:30",
            "doc_url": "https://example.com/b.pdf",
        },
    ]
    selected = select_primary_candidate(candidates)
    assert selected.status == "ambiguous"
    assert selected.candidate is None
    assert len(selected.candidates) == 2


def test_multiple_formal_candidates_selects_unique_earliest() -> None:
    candidates = [
        {
            "title": "2026年5月期 決算短信〔日本基準〕（連結）",
            "published_at": "2026-07-15 15:30",
            "doc_url": "https://example.com/a.pdf",
        },
        {
            "title": "2026年5月期 決算短信〔日本基準〕（非連結）",
            "published_at": "2026-07-15 15:31",
            "doc_url": "https://example.com/b.pdf",
        },
    ]
    selected = select_primary_candidate(candidates)
    assert selected.status == "selected"
    assert selected.candidate == candidates[0]


def test_tdnet_numeric_and_alpha_codes_do_not_collide() -> None:
    assert _strip_trailing_zero("14300") == "1430"
    assert _strip_trailing_zero("143A0") == "143A"
