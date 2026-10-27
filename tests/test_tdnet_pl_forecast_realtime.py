"""FY/quarter TDnet PL and guidance stay complete on ingestion and retry."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock
import zipfile

import pytest

from lib.pipeline.canonical_writer import expand_financials_rows
from src.events.earnings_guidance_extractor import extract_guidance_from_zip
from src.events.earnings_production_pipeline import (
    _retry_incomplete_canonical_for_duplicate,
    _sync_canonical_financials,
)
from src.events.summary_financials import extract_earnings_data
from src.pdf_financial_table import extract_actual_statement_table, extract_fy_actual_period_pdf
from src.events.earnings_subprocess_runner import build_worker_input


FILING_ID = "a" * 64


def _context(name: str, start: str, end: str, member: str) -> str:
    return f'''<xbrli:context id="{name}"><xbrli:entity>
      <xbrli:identifier scheme="test">1234</xbrli:identifier><xbrli:segment>
      <xbrldi:explicitMember dimension="tse:ScopeAxis">tse:{member}</xbrldi:explicitMember>
      </xbrli:segment></xbrli:entity><xbrli:period>
      <xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>
      </xbrli:period></xbrli:context>'''


def _fact(name: str, ctx: str, value: str, scale: int = 3) -> str:
    return (f'<ix:nonFraction name="jppfs_cor:{name}" contextRef="{ctx}" '
            f'unitRef="JPY" scale="{scale}">{value}</ix:nonFraction>')


def _html(body: str) -> str:
    return ('''<html xmlns:ix="http://www.xbrl.org/2008/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:jppfs_cor="urn:jppfs" xmlns:tse="urn:tse">'''
            + body + "</html>")


def _filing_zip(tmp_path):
    actual = "CurrentYearDuration_NonConsolidatedMember"
    prior = "Prior1YearDuration_NonConsolidatedMember"
    opposite = "CurrentYearDuration_ConsolidatedMember"
    forecast = "NextYearDuration_NonConsolidatedMember_ForecastMember"
    summary = _html("".join([
        _context(actual, "2025-10-01", "2026-09-30", "NonConsolidatedMember"),
        _context(forecast, "2026-10-01", "2027-09-30", "NonConsolidatedMember"),
        _fact("NetSales", actual, "100", 6),
        _fact("OperatingIncome", actual, "20", 6),
        _fact("NetSales", forecast, "135", 6),
        _fact("OperatingIncome", forecast, "21", 6),
        _fact("OrdinaryIncome", forecast, "14", 6),
        _fact("NetIncome", forecast, "10", 6),
        _fact("EarningsPerShare", forecast, "154.04", 0),
    ]))
    detail = _html("".join([
        _context(actual, "2025-10-01", "2026-09-30", "NonConsolidatedMember"),
        _context(prior, "2024-10-01", "2025-09-30", "NonConsolidatedMember"),
        _context(opposite, "2025-10-01", "2026-09-30", "ConsolidatedMember"),
        _fact("NetSales", actual, "100,500"),
        _fact("GrossProfit", actual, "40,000"),
        _fact("SellingGeneralAndAdministrativeExpenses", actual, "19,500"),
        _fact("OperatingIncome", actual, "20,500"),
        _fact("GrossProfit", prior, "30,000"),
        _fact("GrossProfit", opposite, "900,000"),
    ]))
    path = tmp_path / "081220261027556739.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/summary-ixbrl.htm", summary)
        archive.writestr("XBRLData/Attachment/pl-ixbrl.htm", detail)
    return path


def test_nonconsolidated_fy_uses_current_statement_and_next_year_context(tmp_path):
    path = _filing_zip(tmp_path)
    earnings = extract_earnings_data(
        xbrl_path=str(path), title="2026年9月期 決算短信〔日本基準〕（非連結）",
        ticker="1234",
    )
    assert (earnings.sales_current, earnings.gross_profit_current,
            earnings.selling_general_and_administrative_expenses_current,
            earnings.op_current) == (100_500_000, 40_000_000, 19_500_000, 20_500_000)
    assert earnings.gross_profit_current - earnings.op_current == 19_500_000
    assert earnings.gross_profit_current != 900_000_000
    guidance = extract_guidance_from_zip(str(path))
    assert guidance.forecast_period == "2027-09-30"
    assert (guidance.sales_forecast, guidance.op_forecast,
            guidance.ordinary_forecast, guidance.net_income_forecast,
            guidance.eps_forecast) == (135_000_000, 21_000_000, 14_000_000, 10_000_000, 154.04)


def test_consolidated_scope_rejects_nonconsolidated_statement(tmp_path):
    path = _filing_zip(tmp_path)
    earnings = extract_earnings_data(
        xbrl_path=str(path), title="2026年9月期 決算短信〔日本基準〕（連結）",
        ticker="5678",
    )
    assert earnings.gross_profit_current is None
    assert earnings.sales_current is None


def test_pdf_statement_uses_current_column_and_preserves_disclosed_zero():
    table = [["売上高", "9,000", "10,000"], ["売上総利益", "2,000", "2,500"],
             ["販売費及び一般管理費", "800", "0"], ["営業利益", "1,200", "2,500"]]
    values, unit = extract_actual_statement_table(
        [table], "損益計算書（単位：千円） 前事業年度 当事業年度",
        "2026年9月期 決算短信（非連結）",
    )
    assert unit == "千円"
    assert values == {"sales": 10_000, "gross_profit": 2_500,
                      "selling_general_and_administrative_expenses": 0,
                      "operating_profit": 2_500}
    assert extract_actual_statement_table(
        [table], "連結損益計算書（単位：千円） 前年度 当年度", "決算短信（非連結）"
    ) == ({}, "")


def test_worker_passes_only_same_disclosure_cached_pdf(tmp_path):
    source_id = "140120261027556739"
    _filing_zip(tmp_path)
    (tmp_path / f"{source_id}.pdf").write_bytes(b"%PDF-1.4")
    doc = {"ticker": "1234", "title": "2026年9月期 決算短信",
           "source_doc_id": source_id, "disclosed_at": "2026-10-27T15:30:00+09:00"}
    worker_input = build_worker_input(doc, archive_root=tmp_path)
    assert worker_input["pdf_path"] == str(tmp_path / f"{source_id}.pdf")


def test_pdf_fills_missing_pl_fields_without_replacing_xbrl_or_zero(tmp_path):
    actual = "CurrentYearDuration_NonConsolidatedMember"
    summary = _html("".join([
        _context(actual, "2025-10-01", "2026-09-30", "NonConsolidatedMember"),
        _fact("NetSales", actual, "100", 6),
        _fact("OperatingIncome", actual, "20", 6),
    ]))
    zip_path = tmp_path / "summary.zip"
    pdf_path = tmp_path / "140120261027556739.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("XBRLData/Summary/summary-ixbrl.htm", summary)
    with patch("src.pdf_financial_table.extract_actual_statement_pdf", return_value=(
        {"sales": 100_000, "operating_profit": 20_000,
         "gross_profit": 20_000, "selling_general_and_administrative_expenses": 0},
        "千円",
    )), patch("src.extractor._extract_from_pdf", return_value=(None, "")):
        earnings = extract_earnings_data(
            xbrl_path=str(zip_path), pdf_path=str(pdf_path),
            title="2026年9月期 決算短信（非連結）", ticker="1234",
        )
    assert (earnings.sales_current, earnings.op_current,
            earnings.gross_profit_current,
            earnings.selling_general_and_administrative_expenses_current) == (
                100_000_000, 20_000_000, 20_000_000, 0,
            )


def test_pdf_mismatch_cannot_fill_missing_pl_fields(tmp_path):
    actual = "CurrentYearDuration_NonConsolidatedMember"
    summary = _html("".join([
        _context(actual, "2025-10-01", "2026-09-30", "NonConsolidatedMember"),
        _fact("NetSales", actual, "100", 6),
        _fact("OperatingIncome", actual, "20", 6),
    ]))
    zip_path = tmp_path / "summary.zip"
    pdf_path = tmp_path / "140120261027556739.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("XBRLData/Summary/summary-ixbrl.htm", summary)
    with patch("src.pdf_financial_table.extract_actual_statement_pdf", return_value=(
        {"sales": 90_000, "operating_profit": 20_000,
         "gross_profit": 30_000, "selling_general_and_administrative_expenses": 10_000},
        "千円",
    )), patch("src.extractor._extract_from_pdf", return_value=(None, "")):
        earnings = extract_earnings_data(
            xbrl_path=str(zip_path), pdf_path=str(pdf_path),
            title="2026年9月期 決算短信（非連結）", ticker="1234",
        )
    assert earnings.gross_profit_current is None
    assert earnings.selling_general_and_administrative_expenses_current is None


def test_pdf_without_unit_does_not_guess_yen_scale(tmp_path):
    pdf_path = tmp_path / "140120261027556739.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    summary = SimpleNamespace(sales=100, operating_profit=20, source_unit="")
    with patch("src.pdf_financial_table.extract_actual_statement_pdf", return_value=({}, "")), \
         patch("src.extractor._extract_from_pdf", return_value=(summary, "")):
        earnings = extract_earnings_data(
            pdf_path=str(pdf_path), title="2026年9月期 決算短信（非連結）",
            ticker="1234",
        )
    assert earnings.sales_current is None
    assert earnings.op_current is None


def test_mixed_forecast_context_periods_are_not_canonicalized(tmp_path):
    sales_period = "NextYearDuration_NonConsolidatedMember_ForecastMember"
    op_period = "Next2YearDuration_NonConsolidatedMember_ForecastMember"
    summary = _html("".join([
        _context(sales_period, "2026-10-01", "2027-09-30", "NonConsolidatedMember"),
        _context(op_period, "2027-10-01", "2028-09-30", "NonConsolidatedMember"),
        _fact("NetSales", sales_period, "135", 6),
        _fact("OperatingIncome", op_period, "21", 6),
    ]))
    path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBRLData/Summary/summary-ixbrl.htm", summary)
    assert extract_guidance_from_zip(str(path)).forecast_period is None


def test_canonical_actual_and_forecast_are_distinct_idempotent_rows(monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    guidance = {"forecast_period": "2027-09-30", "sales_forecast": 13_500_000_000,
                "op_forecast": 2_160_000_000, "ordinary_forecast": 1_460_000_000,
                "net_income_forecast": 1_000_000_000, "eps_forecast": 154.04}
    with patch("lib.pipeline.canonical_writer.write_financials_canonical") as write:
        for _ in range(2):
            _sync_canonical_financials(
                "1234", "2026-09-30", "FY", 10_877_756_000, 1_536_547_000,
                2_311_624_000, 775_077_000, guidance, FILING_ID, False, "test",
                disclosure_datetime="2026-10-27T15:30:00+09:00",
            )
    assert write.call_count == 4
    actual, forecast = write.call_args_list[:2]
    assert (actual.kwargs["period"], actual.kwargs["source"], actual.kwargs["quarter"]) == (
        "2026-09-30", "tdnet_xbrl", "FY")
    assert actual.kwargs["metrics_dict"]["gross_profit"] == 2311.624
    assert (forecast.kwargs["period"], forecast.kwargs["source"], forecast.kwargs["quarter"]) == (
        "2027-09-30", "tdnet_forecast", "FY")
    assert forecast.kwargs["metrics_dict"] == {
        "sales": 13500, "operating_profit": 2160, "ordinary_profit": 1460,
        "net_income": 1000, "eps": 154.04,
    }
    rows1, _ = expand_financials_rows(**{k: actual.kwargs[k] for k in (
        "ticker", "period", "quarter", "metrics_dict", "source", "filing_id",
        "disclosure_datetime", "unit")})
    rows2, _ = expand_financials_rows(**{k: write.call_args_list[2].kwargs[k] for k in (
        "ticker", "period", "quarter", "metrics_dict", "source", "filing_id",
        "disclosure_datetime", "unit")})
    assert {r["source_row_key"] for r in rows1} == {r["source_row_key"] for r in rows2}


def test_missing_forecast_period_never_writes_to_actual_year(monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    with patch("lib.pipeline.canonical_writer.write_financials_canonical") as write:
        _sync_canonical_financials(
            "1234", "2026-09-30", "FY", None, None, None, None,
            {"sales_forecast": 0}, FILING_ID, False, "test",
        )
    assert all(call.kwargs["source"] != "tdnet_forecast" for call in write.call_args_list)


def _pdf_forecast_page(heading="2027年9月期の業績予想（2026年10月1日～2027年9月30日）",
                       sales="百万円\n13,500", op="百万円\n2,160"):
    table = [
        ["", "売上高", "", "営業利益", "", "経常利益", "", "当期純利益", "",
         "１株当たり\n当期純利益"],
        ["通 期", sales, "％\n24.1", op, "％\n40.6", "百万円\n1,460",
         "％\n28.1", "百万円\n1,000", "％\n34.9", "円 銭\n154.04"],
    ]
    return SimpleNamespace(extract_text=lambda: heading, extract_tables=lambda: [table])


def test_pdf_only_forecast_period_and_values_are_saved_separately(tmp_path, monkeypatch):
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY", raising=False)
    monkeypatch.delenv("EARNINGS_CANONICAL_WRITE_REPLACE_DRYRUN", raising=False)
    pdf = tmp_path / "filing.pdf"
    pdf.touch()
    opened = MagicMock()
    opened.__enter__.return_value.pages = [_pdf_forecast_page()]
    with patch("pdfplumber.open", return_value=opened):
        guidance = extract_guidance_from_zip(None, pdf_path=str(pdf))
    assert guidance.forecast_period == "2027-09-30"
    with patch("lib.pipeline.canonical_writer.write_financials_canonical") as write:
        _sync_canonical_financials(
            "1234", "2026-09-30", "FY", 10_877_756_000, 1_536_547_000,
            2_311_624_000, 775_077_000, vars(guidance), FILING_ID, False, "pdf_only",
        )
    assert len(write.call_args_list) == 2
    actual, forecast = write.call_args_list
    assert actual.kwargs["metrics_dict"]["gross_profit"] == 2311.624
    assert actual.kwargs["metrics_dict"]["selling_general_and_administrative_expenses"] == 775.077
    assert (forecast.kwargs["period"], forecast.kwargs["quarter"], forecast.kwargs["source"]) == (
        "2027-09-30", "FY", "tdnet_forecast")
    assert forecast.kwargs["metrics_dict"] == {
        "sales": 13500, "operating_profit": 2160, "ordinary_profit": 1460,
        "net_income": 1000, "eps": 154.04,
    }


def test_pdf_only_actual_statement_keeps_precise_current_column(tmp_path):
    pdf = tmp_path / "filing.pdf"
    pdf.touch()
    summary = SimpleNamespace(sales=10877, operating_profit=1536, source_unit="百万円")
    statement = {"sales": 10_877_756, "gross_profit": 2_311_624,
                 "selling_general_and_administrative_expenses": 775_077,
                 "operating_profit": 1_536_547}
    with patch("src.extractor._extract_from_pdf", return_value=(summary, None)), \
         patch("src.pdf_financial_table.extract_actual_statement_pdf",
               return_value=(statement, "千円")):
        actual = extract_earnings_data(
            pdf_path=str(pdf), title="2026年9月期 決算短信〔日本基準〕（非連結）",
            ticker="1234",
        )
    assert (actual.sales_current, actual.gross_profit_current,
            actual.selling_general_and_administrative_expenses_current,
            actual.op_current) == (10_877_756_000, 2_311_624_000,
                                   775_077_000, 1_536_547_000)


@pytest.mark.parametrize("page", [
    _pdf_forecast_page("2027年9月期の業績予想"),
    _pdf_forecast_page("2028年9月期の業績予想（2026年10月1日～2027年9月30日）"),
    _pdf_forecast_page(sales="13,500"),
    _pdf_forecast_page(op="百万円\n—"),
])
def test_pdf_forecast_requires_matching_explicit_period_and_units(tmp_path, page):
    pdf = tmp_path / "filing.pdf"
    pdf.touch()
    opened = MagicMock()
    opened.__enter__.return_value.pages = [page]
    with patch("pdfplumber.open", return_value=opened):
        assert extract_guidance_from_zip(None, pdf_path=str(pdf)).forecast_period is None


def test_pdf_forecast_disclosed_zero_is_retained(tmp_path):
    pdf = tmp_path / "filing.pdf"
    pdf.touch()
    opened = MagicMock()
    opened.__enter__.return_value.pages = [_pdf_forecast_page(op="百万円\n0")]
    with patch("pdfplumber.open", return_value=opened):
        guidance = extract_guidance_from_zip(None, pdf_path=str(pdf))
    assert guidance.forecast_period == "2027-09-30"
    assert guidance.op_forecast == 0


def test_pdf_consolidated_quarter_release_uses_annual_forecast_heading(tmp_path):
    pdf = tmp_path / "quarter.pdf"
    pdf.touch()
    page = _pdf_forecast_page(
        "2027年3月期の連結業績予想（2026年4月1日～2027年3月31日）",
        sales="百万円\n2,500", op="百万円\n0",
    )
    table = page.extract_tables()[0]
    table[0][5] = "税引前利益"
    table[0][7] = "親会社の所有者に帰属する当期利益"
    opened = MagicMock()
    opened.__enter__.return_value.pages = [page]
    with patch("pdfplumber.open", return_value=opened):
        guidance = extract_guidance_from_zip(None, pdf_path=str(pdf))
    assert guidance.forecast_period == "2027-03-31"
    assert guidance.sales_forecast == 2_500_000_000
    assert guidance.op_forecast == 0


def test_pdf_actual_period_requires_explicit_full_year_dates(tmp_path):
    pdf = tmp_path / "filing.pdf"
    pdf.touch()
    opened = MagicMock()
    opened.__enter__.return_value.pages = [SimpleNamespace(
        extract_text=lambda: "2026年9月期の業績（2025年10月1日～2026年9月30日）",
    )]
    with patch("pdfplumber.open", return_value=opened):
        assert extract_fy_actual_period_pdf(str(pdf)) == "2026-09-30"
    opened.__enter__.return_value.pages = [SimpleNamespace(
        extract_text=lambda: "2026年9月期の業績",
    )]
    with patch("pdfplumber.open", return_value=opened):
        assert extract_fy_actual_period_pdf(str(pdf)) is None


def test_forecast_revision_same_year_has_distinct_rows_and_newer_recency():
    common = {"ticker": "1234", "period": "2027-09-30", "quarter": "FY",
              "source": "tdnet_forecast", "unit": "millions_jpy"}
    old, _ = expand_financials_rows(
        **common, metrics_dict={"sales": 13500}, filing_id="a" * 64,
        disclosure_datetime="2026-10-27T15:30:00+09:00",
    )
    revised, _ = expand_financials_rows(
        **common, metrics_dict={"sales": 14000}, filing_id="b" * 64,
        disclosure_datetime="2026-11-15T15:30:00+09:00",
    )
    assert old[0]["source_row_key"] != revised[0]["source_row_key"]
    assert revised[0]["recency_key"] > old[0]["recency_key"]
    assert revised[0]["period"] == old[0]["period"] == "2027-09-30"


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        data = [row for row in self.rows if all(row.get(k) == v for k, v in self.filters.items())]
        return SimpleNamespace(data=data)


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _Query(self.rows)


@pytest.mark.parametrize("missing", ["gross_profit", "forecast"])
def test_duplicate_retry_requires_gross_and_forecast(missing):
    actual = [{"ticker": "1234", "period": "2026-09-30", "quarter": "FY",
               "filing_id": FILING_ID, "metric": metric, "source": "tdnet_xbrl"}
              for metric in ("sales", "operating_profit", "gross_profit")]
    forecast = [{"ticker": "1234", "period": "2027-09-30", "quarter": "FY",
                 "filing_id": FILING_ID, "metric": metric, "source": "tdnet_forecast"}
                for metric in ("sales", "operating_profit")]
    rows = ([row for row in actual if row["metric"] != "gross_profit"] + forecast
            if missing == "gross_profit" else actual)
    values = {"sales": 100_000_000, "op": 20_000_000, "gross": 40_000_000,
              "sga": None, "guidance": {"forecast_period": "2027-09-30",
                                         "sales_forecast": 135_000_000,
                                         "op_forecast": 21_000_000}}
    with patch("src.events.tdnet_event_store._get_supabase", return_value=_Client(rows)), \
         patch("src.events.earnings_production_pipeline._sync_canonical_financials") as sync:
        _retry_incomplete_canonical_for_duplicate(
            "1234", "2026-09-30", "FY", FILING_ID, "", None, values, False,
        )
    assert sync.call_count == 1


def test_duplicate_retry_reparses_old_incomplete_payload(tmp_path):
    path = _filing_zip(tmp_path)
    rows = [{"ticker": "1234", "period": "2026-09-30", "quarter": "FY",
             "filing_id": FILING_ID, "metric": metric, "source": "jquants_earnings_summary"}
            for metric in ("sales", "operating_profit")]
    with patch("src.events.tdnet_event_store._get_supabase", return_value=_Client(rows)), \
         patch("src.events.earnings_production_pipeline._sync_canonical_financials") as sync:
        _retry_incomplete_canonical_for_duplicate(
            "1234", "2026-09-30", "FY", FILING_ID, "", str(path),
            {"sales": 100_000_000, "op": 20_000_000,
             "gross": None, "sga": None, "guidance": {}}, False,
            title="2026年9月期 決算短信〔日本基準〕（非連結）",
        )
    assert sync.call_count == 1
    assert sync.call_args.kwargs["gross_value"] == 40_000_000
    assert sync.call_args.kwargs["sga_value"] == 19_500_000
    assert sync.call_args.kwargs["guidance"]["forecast_period"] == "2027-09-30"


def test_quarter_guidance_and_zero_do_not_move_to_next_year():
    from src.events.earnings_production_pipeline import _valid_forecast_period, _canonical_forecast_metrics
    assert _valid_forecast_period("2026-09-30", "3Q", "2026-09-30")
    assert not _valid_forecast_period("2026-09-30", "FY", "2026-09-30")
    assert _canonical_forecast_metrics({"sales_forecast": 0, "eps_forecast": 0}) == {
        "sales": 0, "eps": 0,
    }
