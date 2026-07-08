import os
import json
import logging
from dataclasses import asdict
from typing import Any

from src.events.pipeline_context import (
    DisclosureIdentity, FilingPeriodEvidence, CanonicalWritePlan, EarningsExtractionEvidence
)
from src.events.summary_financials import EarningsSummaryData
from src.events.earnings_guidance_extractor import GuidanceData

logger = logging.getLogger(__name__)

def run_shadow_write_plan(
    ticker: str,
    doc: Any,
    earnings: EarningsSummaryData,
    guidance: GuidanceData | None,
    xbrl_path: str,
    fiscal_year: str,
    quarter: str
):
    """
    Shadow mode for generating CanonicalWritePlan.
    This does not save to DB, just logs the plan and any diff against existing expectations.
    Requires EARNINGS_WRITE_PLAN_SHADOW=1 to be set.
    """
    if os.getenv("EARNINGS_WRITE_PLAN_SHADOW", "0") != "1":
        return

    logger.info(f"[EARNINGS][SHADOW] {ticker} Starting shadow write plan generation")
    
    try:
        # Re-extract with include_evidence=True to capture evidences
        # Since we are in shadow mode (dry run / batch testing), it's okay to take the performance hit
        from src.events.summary_financials import extract_earnings_data
        from src.events.earnings_guidance_extractor import extract_guidance_from_zip
        
        shadow_earnings = extract_earnings_data(xbrl_path=xbrl_path, include_evidence=True)
        shadow_guidance = extract_guidance_from_zip(xbrl_path=xbrl_path, include_evidence=True)
        
        if not shadow_earnings:
            logger.warning(f"[EARNINGS][SHADOW] {ticker} shadow extraction returned None")
            return
            
        tdnet_pdf_id = getattr(doc, "tdnet_pdf_id", None) or getattr(doc, "doc_id", None)
        ident = DisclosureIdentity.create_and_normalize(
            ticker=ticker, tdnet_pdf_id=tdnet_pdf_id
        )
        
        # Determine expected context end based on simple logic for shadow run
        expected_context_end = None
        if fiscal_year:
            # naive end of month string if possible
            expected_context_end = "unknown"
            
        # We simulate resolving period using the master_period concept if available
        # In real pipeline, master_period will come from `companies` table.
        # Here we just use the naive expected_context_end or what extraction got
        period_ev = FilingPeriodEvidence(
            title_quarter=quarter,
            title_fiscal_year=fiscal_year,
            expected_context_end=expected_context_end
        )
        # Use extracted period as canonical for now if master doesn't override
        period_ev.resolve_period(shadow_earnings.period)

        plans = []
        
        # Helper to compare and append
        def append_diff_plan(metric_name, shadow_val, current_val, quarter_str, source, evidence_list):
            shadow_val_millions = round(shadow_val / 1_000_000) if shadow_val is not None else None
            current_val_millions = round(current_val / 1_000_000) if current_val is not None else None
            
            plan = CanonicalWritePlan(
                ticker=ticker,
                period=period_ev.canonical_period or "",
                quarter=quarter_str,
                metric=metric_name,
                value=shadow_val_millions if shadow_val_millions is not None else 0,
                unit="millions_jpy",
                source=source,
                filing_id=ident.filing_id
            )
            plan.validate_and_prepare()
            matched_ev = next((e for e in evidence_list if e.metric == metric_name), None)
            
            diff = {
                "value_diff": shadow_val_millions != current_val_millions,
                "current_value": current_val_millions,
                "shadow_value": shadow_val_millions
            }
            
            plans.append({
                "write_plan": asdict(plan),
                "evidence": asdict(matched_ev) if matched_ev else None,
                "diff": diff
            })

        # 1. Actuals
        append_diff_plan("sales", shadow_earnings.sales_current, earnings.sales_current, quarter, "jquants", shadow_earnings.evidences)
        append_diff_plan("operating_profit", shadow_earnings.op_current, earnings.op_current, quarter, "jquants", shadow_earnings.evidences)
        append_diff_plan("gross_profit", shadow_earnings.gross_profit_current, getattr(earnings, "gross_profit_current", None), quarter, "jquants", shadow_earnings.evidences)
                
        # 2. Guidance
        if shadow_guidance:
            append_diff_plan("sales", shadow_guidance.sales_forecast, guidance.sales_forecast if guidance else None, "FY", "jquants_forecast_fy", shadow_guidance.evidences)
            append_diff_plan("operating_profit", shadow_guidance.op_forecast, guidance.op_forecast if guidance else None, "FY", "jquants_forecast_fy", shadow_guidance.evidences)
                    
        # Append to a global report file for verification
        report_file = "scratch/phase4a_shadow_write_plan.json"
        existing = []
        if os.path.exists(report_file):
            try:
                with open(report_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except:
                pass
                
        existing.append({
            "ticker": ticker,
            "identity": asdict(ident),
            "period_evidence": asdict(period_ev),
            "plans": plans
        })
        
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
            
        logger.info(f"[EARNINGS][SHADOW] {ticker} generated {len(plans)} shadow write plans.")
        
    except Exception as e:
        logger.exception(f"[EARNINGS][SHADOW] {ticker} Shadow write plan failed: {e}")
