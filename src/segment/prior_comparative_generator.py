from typing import Any, Dict, List, Optional
import math
from datetime import datetime
from src.segment.normalize import normalize_segment_key

def generate_prior_comparative_payload(
    xbrl_rows: List[Any],
    official_priors: List[Dict[str, Any]],
    ticker: str,
    doc_id: str,
    disclosure_datetime: str,
    quarter: str
) -> List[Dict[str, Any]]:
    """
    Generates a canonical_segments prior_comparative payload from extracted XBRL rows.
    
    Args:
        xbrl_rows: List of XbrlSegmentRow extracted from XBRL ZIP.
        official_priors: List of canonical_segments dictionaries containing the official_current records for the prior period.
        ticker: The stock ticker (e.g., '6905').
        doc_id: The document ID (e.g., '140120260613537025').
        disclosure_datetime: ISO format date/time of disclosure (e.g., '2026-06-13T15:30:00+09:00').
        quarter: The quarter (e.g., 'FY', '1Q').
        
    Returns:
        List of dictionaries representing the planned canonical_segments prior_comparative rows.
    """
    if not xbrl_rows:
        return []

    # Determine periods
    periods = set(r.period for r in xbrl_rows if r.period)
    if len(periods) < 2:
        return []
        
    current_period = max(periods)
    previous_periods = sorted([p for p in periods if p != current_period])
    prior_period = previous_periods[-1]
    
    prior_rows = [r for r in xbrl_rows if r.period == prior_period]
    current_rows = [r for r in xbrl_rows if r.period == current_period]
    
    planned_rows = []
    
    for r in prior_rows:
        seg_name = r.normalized_segment_name or r.raw_segment_name
        seg_key = normalize_segment_key(seg_name)
        
        cur_row = next((c for c in current_rows if (c.normalized_segment_name or c.raw_segment_name) == seg_name), None)
        matching_official = [o for o in official_priors if o.get("segment_key") == seg_key]
        
        for metric, value in [("sales", r.sales), ("profit", r.profit)]:
            if value is None:
                continue
                
            cur_value = getattr(cur_row, metric) if cur_row else None
            flags = {
                "source_disclosure_date_precision": "exact",
                "source_disclosure_date_estimated_time": False
            }
            if value < 0:
                flags["prior_negative"] = True
                if cur_value is not None and cur_value >= 0:
                    flags["turnaround_black"] = True
            elif value == 0:
                flags["prior_zero"] = True
            elif value > 0:
                if cur_value is not None and cur_value < 0:
                    flags["turnaround_red"] = True
                    
            off_val = next((o for o in matching_official if o.get("metric") == metric), None)
            if off_val:
                unit = off_val.get("unit", "JPY")
                if unit == "millions_jpy" and abs(value) > 100000:
                    value = int(value / 1000000)
            else:
                unit = "JPY"
                if abs(value) > 100000:
                    unit = "millions_jpy"
                    value = int(value / 1000000)
                    
            row_key = f"cs|{ticker}|{prior_period}|{quarter}|{seg_key}|{metric}|xbrl_prior_comparative|{doc_id}"
            disclosure_date = disclosure_datetime.split("T")[0] if disclosure_datetime else None
            
            planned_rows.append({
                "ticker": ticker,
                "period": prior_period,
                "quarter": quarter,
                "segment_name": seg_name,
                "segment_key": seg_key,
                "metric": metric,
                "value": value,
                "unit": unit,
                "source": "xbrl_prior_comparative",
                "source_priority": 1,
                "source_row_key": row_key,
                "data_basis": "prior_comparative",
                "source_disclosure_period": current_period,
                "source_disclosure_date": disclosure_date,
                "source_doc_id": doc_id,
                "flags": flags
            })
            
    return planned_rows
