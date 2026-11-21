"""Regressions from 525 archived official disclosures; no external APIs."""
import zipfile
from xml.etree import ElementTree as ET

import pytest

from src.extractor import _extract_from_xbrl, _parse_xbrl_content
from src.xbrl_package_context import package_resources

NS = '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:x="http://www.xbrl.org/2003/instance" xmlns:d="http://xbrl.org/2006/xbrldi">{}</html>'


def resource(member='NonConsolidatedMember', consolidated='false', end='2026-02-20', issuer='9999'):
    return (f'<ix:nonNumeric name="dei:WhetherConsolidatedFinancialStatementsArePreparedDEI">{consolidated}</ix:nonNumeric>'
            '<ix:nonNumeric name="dei:CurrentFiscalYearStartDateDEI">2025-02-21</ix:nonNumeric>'
            f'<ix:nonNumeric name="dei:CurrentFiscalYearEndDateDEI">{end}</ix:nonNumeric>'
            f'<x:context id="CurrentYearDuration_{member}_ResultMember"><x:entity><x:identifier scheme="tse">{issuer}</x:identifier>'
            f'<x:segment><d:explicitMember dimension="ScopeAxis">{member}</d:explicitMember><d:explicitMember dimension="ResultAxis">ResultMember</d:explicitMember></x:segment>'
            f'</x:entity><x:period><x:startDate>2025-02-21</x:startDate><x:endDate>{end}</x:endDate></x:period></x:context>')


def facts(member='NonConsolidatedMember', sales_tag='NetSales', gross=True):
    ctx=f'CurrentYearDuration_{member}_ResultMember'
    values=[(sales_tag, '1234'),('OperatingIncome','-25')]
    if gross:values.append(('GrossProfit','456'))
    return ''.join(f'<ix:nonFraction name="t:{tag}" contextRef="{ctx}" scale="6" unitRef="JPY">{value}</ix:nonFraction>'for tag,value in values)


@pytest.mark.parametrize('tag',['NetSales','Revenue2','OperatingRevenuesSpecific'])
def test_declared_nonconsolidated_primary_actual_is_not_discarded(tag):
    result=_parse_xbrl_content(NS.format(resource()+facts(sales_tag=tag)).encode())
    assert result.sales==1234000000
    assert result.operating_profit==-25000000
    assert result.gross_profit==456000000
    assert (result.fiscal_year,result.quarter)==('2026-02-20','FY')


@pytest.mark.parametrize('declared',['true',''])
def test_nonconsolidated_not_admitted_without_primary_scope_declaration(declared):
    result=_parse_xbrl_content(NS.format(resource(consolidated=declared)+facts()).encode())
    assert result.operating_profit is None
    assert result.gross_profit is None


@pytest.mark.parametrize('member',['SegmentMember','NonConsolidatedMember_ForecastMember','ConsolidatedMember'])
def test_declared_nonconsolidated_rejects_other_scope(member):
    result=_parse_xbrl_content(NS.format(resource(member=member)+facts(member=member)).encode())
    assert result is None or result.gross_profit is None


def test_package_resolves_shared_bs_context_without_deriving_gross_profit(tmp_path):
    path=tmp_path/'statement.zip'
    with zipfile.ZipFile(path,'w')as z:
        z.writestr('XBRLData/Summary/s-ixbrl.htm',NS.format(resource()+facts(gross=False)))
        z.writestr('XBRLData/Attachment/bs-ixbrl.htm',NS.format(resource()))
        z.writestr('XBRLData/Attachment/pl-ixbrl.htm',NS.format(facts()))
    result=_extract_from_xbrl(str(path))
    assert result.gross_profit==456000000
    assert result.field_sources['gross_profit']=='attachment_xbrl'
    assert (result.fiscal_year,result.quarter)==('2026-02-20','FY')


def test_conflicting_context_identity_is_rejected(tmp_path):
    path=tmp_path/'conflict.zip'
    with zipfile.ZipFile(path,'w')as z:
        z.writestr('XBRLData/Attachment/bs-ixbrl.htm',NS.format(resource(issuer='1111')))
        z.writestr('XBRLData/Attachment/pl-ixbrl.htm',NS.format(resource(issuer='2222')+facts()))
    with zipfile.ZipFile(path)as z,pytest.raises(ValueError,match='Conflicting shared XBRL context'):
        package_resources(z,z.namelist())
    assert _extract_from_xbrl(str(path)) is None


def test_invalid_unrelated_note_preserves_valid_summary(tmp_path):
    path=tmp_path/'valid-summary.zip'
    with zipfile.ZipFile(path,'w')as z:
        z.writestr('XBRLData/Summary/s-ixbrl.htm',NS.format(resource()+facts()))
        z.writestr('XBRLData/Attachment/note-ixbrl.htm','<malformed')
    result=_extract_from_xbrl(str(path))
    assert result.sales==1234000000
    assert result.gross_profit==456000000
