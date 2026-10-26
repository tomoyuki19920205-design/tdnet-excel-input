"""Dividend comparisons must use disclosed totals for the same forecast period."""
from pathlib import Path

import pdfplumber

from src.events.dividend_extractor import extract_dividend_revision
from src.events.tdnet_event_store import _compute_strength_score, _extract_primary_metric
from src.events.event_pipeline import _dividend_to_event_record
from src.events.common_models import DocumentMeta


PDF = Path(__file__).parent / "fixtures" / "dividend" / "nagawa_9663_20261026.pdf"
TITLE = "2027年3月期配当予想の修正に関するお知らせ"


def test_nagawa_original_pdf_uses_revised_total_not_ordinary_component():
    with pdfplumber.open(PDF) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    event = extract_dividend_revision(text, TITLE, str(PDF))
    assert event.fiscal_period.startswith("2027年")
    assert event.dividend_basis == "年間"
    assert (event.annual_total_previous, event.annual_total_revised) == (60, 80)
    assert (event.previous_dividend_per_share, event.revised_dividend_per_share) == (60, 80)
    assert event.delta_dividend_per_share == 20
    assert event.commemorative_dividend_per_share is None  # 40円は前期実績
    assert event.subtype == "increase"

    doc = DocumentMeta(doc_id="disclosure-9663", ticker="9663", company_name="ナガワ",
                       title=TITLE, disclosure_datetime="2026-10-26 13:00")
    record = _dividend_to_event_record(doc, event)
    assert _extract_primary_metric(record) == ("配当", "80.0円", "+33.3%")
    assert round(_compute_strength_score(record), 1) == 33.3


def test_split_label_rows_and_special_dividend_use_disclosed_total():
    pdf_path = PDF.parent / "special_1418_20261015.pdf"
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    event = extract_dividend_revision(text, pdf_path=str(pdf_path))
    assert (event.annual_total_previous, event.annual_total_revised) == (30, 36)
    assert (event.previous_dividend_per_share, event.revised_dividend_per_share) == (30, 36)
    assert event.special_dividend_per_share == 6
    period_end = extract_dividend_revision(
        text, "2027年2月期 期末配当予想の修正", str(pdf_path))
    assert period_end.dividend_basis == "期末"
    assert (period_end.previous_dividend_per_share, period_end.revised_dividend_per_share) == (15, 21)
    assert (period_end.annual_total_previous, period_end.annual_total_revised) == (30, 36)


def test_explicit_zero_total_is_not_discarded():
    pdf_path = PDF.parent / "nodividend_3297_20261013.pdf"
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    event = extract_dividend_revision(text, pdf_path=str(pdf_path))
    assert (event.annual_total_previous, event.annual_total_revised) == (43, 0)
    assert event.delta_dividend_per_share == -43
    assert event.subtype == "decrease"


def test_explicit_annual_total_is_not_added_to_ordinary_and_special_components():
    text = """2027年3月期 配当予想の修正
年間配当金 中間 期末 合計
前回予想 20円 30円 50円
今回修正予想 20円 50円 70円（普通配当50円、特別配当20円）
前期実績（2026年3月期）20円 80円 100円（普通配当60円、記念配当40円）"""
    event = extract_dividend_revision(text, "2027年3月期配当予想の修正")
    assert (event.previous_dividend_per_share, event.revised_dividend_per_share) == (50, 70)
    assert event.special_dividend_per_share == 20
    assert event.commemorative_dividend_per_share is None
    assert event.annual_total_revised == 70


def test_period_end_revision_uses_period_end_not_interim_plus_annual():
    text = """2027年3月期 期末配当予想の修正
中間 期末 合計
前回予想 20円 30円 50円
今回修正予想 20円 40円 60円"""
    event = extract_dividend_revision(text, "2027年3月期 期末配当予想の修正")
    assert event.dividend_basis == "期末"
    assert (event.previous_dividend_per_share, event.revised_dividend_per_share) == (30, 40)
    assert (event.annual_total_previous, event.annual_total_revised) == (50, 60)


def test_undecided_and_zero_are_not_coerced_to_unchanged():
    undecided = extract_dividend_revision(
        "前回予想 未定\n今回修正予想 80円", TITLE)
    assert undecided.previous_dividend_per_share is None
    assert undecided.revised_dividend_per_share == 80
    assert undecided.delta_dividend_per_share is None
    zero = extract_dividend_revision("前回予想 0円\n今回修正予想 20円", TITLE)
    assert (zero.previous_dividend_per_share, zero.revised_dividend_per_share) == (0, 20)
    assert zero.delta_dividend_per_share == 20
