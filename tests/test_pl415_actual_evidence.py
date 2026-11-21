import json
from pathlib import Path
from decimal import Decimal

import pytest

from src.pl_actual_evidence import actual_period, is_actual_statement, money, summary_actuals

CASES=json.loads((Path(__file__).parent/'fixtures/pl415_actuals.json').read_text(encoding='utf8'))


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['ticker'])
def test_retained_official_exact_period(case):
    got=actual_period(case['title'],case['text'])
    assert got and (got.fiscal_end,got.quarter)==(case['period'],case['quarter'])


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['ticker'])
def test_retained_official_current_amounts(case):
    got,unit=summary_actuals(case['text'],case['period'],case['quarter'])
    assert unit=='百万円'
    assert got['sales']==Decimal(str(case['expected_sales']))
    assert got['operating_profit']==Decimal(str(case['expected_op']))


@pytest.mark.parametrize('day',[15,20,28,31])
def test_exact_day_is_not_replaced_with_month_end(day):
    start=f'2025年4月{day+1}日' if day<31 else '2025年5月1日'
    title='2026年4月期 決算短信'
    text=f'2026年4月期の連結業績({start}~2026年4月{day}日)\n連結経営成績'
    if day==31: # An impossible printed date is rejected, not rounded.
        assert actual_period(title,text) is None
    else:
        assert actual_period(title,text).fiscal_end==f'2026-04-{day}'


def test_short_fiscal_year_is_full_year_not_third_quarter():
    p=actual_period('2025年12月期 決算短信','2025年12月期(2025年4月1日~2025年12月31日)\n連結経営成績')
    assert p.quarter=='FY' and p.fiscal_end=='2025-12-31'


def test_no_exact_date_no_invention():
    assert actual_period('2027年3月期 第2四半期決算短信','連結経営成績') is None


def test_standalone_quarter_not_cumulative():
    assert actual_period('2026年3月期 第3四半期決算短信','2026年3月期(2025年10月1日~2025年12月31日)\n連結経営成績') is None
    assert actual_period('2026年3月期 第4四半期決算短信','2026年3月期(2026年1月1日~2026年3月31日)\n連結経営成績') is None


@pytest.mark.parametrize('title,ok',[
    ('(訂正・数値データ訂正)2026年3月期 決算短信',True),
    ('(再訂正)2026年3月期 決算短信',True),
    ('2026年3月期 決算短信に関するQ&A',False),
    ('2026年3月期 決算説明資料',False),
    ('2026年3月期 決算短信 補足資料',False),
])
def test_statement_kind(title,ok):assert is_actual_statement(title)==ok


@pytest.mark.parametrize('cell,expected',[('11,098',11098),('120,057',120057),('△78',-78),('12.375',Decimal('12.375')),('98',98),('0',0)])
def test_complete_money_cells(cell,expected):assert money(cell)==expected


@pytest.mark.parametrize('cell',['11,0','110 98','2026年','123.4%','123万円','1,23,456'])
def test_reject_partial_money_cells(cell):
    with pytest.raises(ValueError):money(cell)


def test_no_year_or_operating_profit_as_sales():
    text='連結経営成績\n売上高 営業利益\n2026年11月18日\n営業利益503百万円'
    assert summary_actuals(text,'2026-08-31','FY')==({},'')


def test_current_row_is_identified_even_when_prior_row_is_first():
    text='連結経営成績\n売上高 営業利益 経常利益 当期純利益\n千円 % 千円 % 千円 % 千円 %\n2025年3月期 100 0.1 10 0.2 9 0.3 8 0.4\n2026年3月期 123 1.1 20 1.2 19 1.3 18 1.4'
    values,unit=summary_actuals(text,'2026-03-20','FY')
    assert values['sales']==123 and values['operating_profit']==20 and unit=='千円'


def test_ambiguous_before_after_correction_is_held():
    text='連結経営成績\n売上高 営業利益\n百万円 % 百万円 %\n2026年3月期 123 1.1 20 1.2\n2026年3月期 130 1.1 22 1.2'
    assert summary_actuals(text,'2026-03-20','FY')==({},'')
