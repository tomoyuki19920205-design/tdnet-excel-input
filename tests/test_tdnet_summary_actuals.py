import io
import zipfile

from src.tdnet_summary_actuals import extract_summary_actuals_from_zip_bytes


def _zip(summary: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("XBRLData/Summary/sample-ixbrl.htm", summary)
    return output.getvalue()


def _fixture() -> bytes:
    context = "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"
    return _zip(f"""<html xmlns='http://www.w3.org/1999/xhtml'
      xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'
      xmlns:xbrli='http://www.xbrl.org/2003/instance'
      xmlns:xbrldi='http://xbrl.org/2006/xbrldi'
      xmlns:tse-ed-t='http://www.xbrl.tdnet.info/taxonomy/jp/tse/tdnet/ed/t/2014-01-12'
      xmlns:jppfs_cor='http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2023-12-01/jppfs_cor'>
      <body><ix:header><ix:resources>
        <xbrli:context id='{context}'><xbrli:entity><xbrli:identifier scheme='x'>92490</xbrli:identifier>
          <xbrli:segment>
            <xbrldi:explicitMember dimension='tse-ed-t:ConsolidatedNonconsolidatedAxis'>tse-ed-t:ConsolidatedMember</xbrldi:explicitMember>
            <xbrldi:explicitMember dimension='tse-ed-t:ResultForecastAxis'>tse-ed-t:ResultMember</xbrldi:explicitMember>
          </xbrli:segment></xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <xbrli:context id='CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember'><xbrli:entity><xbrli:identifier scheme='x'>92490</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension='tse-ed-t:ConsolidatedNonconsolidatedAxis'>tse-ed-t:NonConsolidatedMember</xbrldi:explicitMember><xbrldi:explicitMember dimension='tse-ed-t:ResultForecastAxis'>tse-ed-t:ResultMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
        <xbrli:context id='CurrentAccumulatedQ2Duration_ConsolidatedMember_ForecastMember'><xbrli:entity><xbrli:identifier scheme='x'>92490</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension='tse-ed-t:ConsolidatedNonconsolidatedAxis'>tse-ed-t:ConsolidatedMember</xbrldi:explicitMember><xbrldi:explicitMember dimension='tse-ed-t:ResultForecastAxis'>tse-ed-t:ForecastMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
      </ix:resources></ix:header>
      <ix:nonFraction name='tse-ed-t:NetSales' contextRef='{context}' unitRef='JPY' scale='6'>7,882</ix:nonFraction>
      <ix:nonFraction name='tse-ed-t:OperatingIncome' contextRef='{context}' unitRef='JPY' scale='6'>1,019</ix:nonFraction>
      <ix:nonFraction name='tse-ed-t:OrdinaryIncome' contextRef='{context}' unitRef='JPY' scale='6'>1,019</ix:nonFraction>
      <ix:nonFraction name='tse-ed-t:ProfitAttributableToOwnersOfParent' contextRef='{context}' unitRef='JPY' scale='6'>687</ix:nonFraction>
      <ix:nonFraction name='tse-ed-t:OperatingIncome' contextRef='CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember' unitRef='JPY' scale='6'>9,999</ix:nonFraction>
      <ix:nonFraction name='tse-ed-t:OperatingIncome' contextRef='CurrentAccumulatedQ2Duration_ConsolidatedMember_ForecastMember' unitRef='JPY' scale='6'>8,888</ix:nonFraction>
    </body></html>""")


def test_extracts_9249_equivalent_consolidated_actual_summary_pl():
    facts = extract_summary_actuals_from_zip_bytes(_fixture(), expected_quarter="2Q")

    assert {metric: fact.value_jpy for metric, fact in facts.items()} == {
        "sales": 7_882_000_000,
        "operating_profit": 1_019_000_000,
        "ordinary_profit": 1_019_000_000,
        "net_income": 687_000_000,
    }
    assert facts["sales"].period_start == "2025-10-01"
    assert facts["sales"].period_end == "2026-03-31"
    assert facts["operating_profit"].members == (
        "ConsolidatedMember", "ResultMember",
    )


def test_nonconsolidated_and_forecast_contexts_are_rejected():
    facts = extract_summary_actuals_from_zip_bytes(_fixture(), expected_quarter="2Q")

    assert facts["operating_profit"].value_jpy == 1_019_000_000


def test_wrong_quarter_does_not_consume_2q_facts():
    facts = extract_summary_actuals_from_zip_bytes(_fixture(), expected_quarter="3Q")

    assert facts == {}


def test_expected_period_dates_reject_prior_comparative_context():
    facts = extract_summary_actuals_from_zip_bytes(
        _fixture(),
        expected_quarter="2Q",
        expected_period_start="2024-10-01",
        expected_period_end="2025-03-31",
    )

    assert facts == {}
