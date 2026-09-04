"""PDF money/profit contracts; all databases are temporary fixtures."""
import pytest
from src.pdf_financial_table import extract_actual_financial_table
from src.migration.migration_db import MigrationDB

@pytest.mark.parametrize("header", ["税引前当期純利益", "税金等調整前四半期純利益", "税引前中間純利益"])
def test_pbt_is_not_net_or_ordinary_income(header):
    result = extract_actual_financial_table([[["売上高", header], ["100", "20"]]])
    assert result == {"sales": 100, "profit_before_tax": 20}

def test_distinct_profit_accounts_and_missing_prior():
    table = [[["売上高", "営業利益", "税引前利益", "当期純利益"], ["100\n90", "10\n9", "12\n11", "7"]]]
    assert extract_actual_financial_table(table) == {"sales": 100, "operating_profit": 10, "profit_before_tax": 12, "net_income": 7}
    assert extract_actual_financial_table(table, period_index=1) == {"sales": 90, "operating_profit": 9, "profit_before_tax": 11}

def test_local_profit_storage_preserves_values_on_sparse_update(tmp_path):
    db = MigrationDB(str(tmp_path / "financial.db"))
    try:
        db.upsert_quarterly_result("1234", "2024-10-31", "3Q", sales=100, operating_profit=10, profit_before_tax=12, net_income=7)
        row = db.get_quarterly_result("1234", "2024-10-31", "3Q")
        assert (row["operating_profit"], row["profit_before_tax"], row["net_income"]) == (10, 12, 7)
        db.upsert_quarterly_result("1234", "2024-10-31", "3Q", sales=101)
        row = db.get_quarterly_result("1234", "2024-10-31", "3Q")
        assert (row["sales"], row["profit_before_tax"], row["net_income"]) == (101, 12, 7)
    finally:
        db.close()

@pytest.mark.parametrize("unit,multiplier", [("円",1),("千円",1000),("百万円",1000000)])
def test_summary_pdf_money_normalized_once(monkeypatch, tmp_path, unit, multiplier):
    from src.models import ExtractedFinancials
    from src.events.summary_financials import extract_earnings_data
    import src.extractor
    import pdfplumber
    class PDF:
        pages = []
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setattr(pdfplumber, "open", lambda *a,**k: PDF())
    monkeypatch.setattr(src.extractor,"_extract_from_pdf", lambda *a: (ExtractedFinancials(sales=100,operating_profit=10,profit_before_tax=12,source_unit=unit),""))
    path=tmp_path/"summary.pdf"
    path.write_bytes(b"fixture")
    result=extract_earnings_data(pdf_path=str(path), title="2024年10月期 第3四半期決算短信", ticker="1234")
    assert (result.sales_current,result.op_current,result.profit_before_tax_current)==(100*multiplier,10*multiplier,12*multiplier)
    assert result.source_unit=="円"
