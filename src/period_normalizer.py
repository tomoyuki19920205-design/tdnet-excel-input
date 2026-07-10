import re
from typing import Optional, Tuple
from datetime import datetime

class PeriodValidationError(Exception):
    pass

def _is_invalid(val: str) -> bool:
    v = val.strip().upper()
    return not v or v == "UNKNOWN" or v == "NAN"

def _normalize_quarter(quarter_str: str) -> str:
    """4Q is FY. Otherwise, match [1-4]Q or FY."""
    q = quarter_str.strip().upper()
    if "4Q" in q:
        return "FY"
    if "FY" in q:
        return "FY"
    m = re.search(r"([1-4])Q", q)
    if m:
        if m.group(1) == "4":
            return "FY"
        return f"{m.group(1)}Q"
    return q

def normalize_period_and_quarter(
    current_period_end_date: Optional[str],
    current_fiscal_year_end_date: Optional[str],
    type_of_current_period: Optional[str],
    title: Optional[str] = "",
) -> Tuple[str, str, str]:
    """
    Returns: (period, fiscal_year_end, quarter)
    """
    period = str(current_period_end_date or "").strip()
    fy_end = str(current_fiscal_year_end_date or "").strip()
    q_type = str(type_of_current_period or "").strip()

    if _is_invalid(period):
        raise PeriodValidationError(f"Invalid CurrentPeriodEndDate: {period}")

    if _is_invalid(fy_end):
        raise PeriodValidationError(f"Invalid CurrentFiscalYearEndDate: {fy_end}")

    quarter = _normalize_quarter(q_type)
    
    if _is_invalid(quarter):
        # Fallback to title
        t = str(title or "")
        if "第1四半期" in t or "第１四半期" in t:
            quarter = "1Q"
        elif "第2四半期" in t or "第２四半期" in t:
            quarter = "2Q"
        elif "第3四半期" in t or "第３四半期" in t:
            quarter = "3Q"
        elif "第4四半期" in t or "第４四半期" in t or "本決算" in t or re.search(r"Full[- ]?Year|Annual", t, re.IGNORECASE):
            quarter = "FY"
        else:
            raise PeriodValidationError(f"Invalid TypeOfCurrentPeriod and cannot infer from title: {q_type}")

    # Check if period matches fy_end when not FY
    if quarter != "FY" and period == fy_end:
        # User specified: CurrentPeriodEndDate and CurrentFiscalYearEndDate should be strictly separated.
        # But wait, what if API returns period == fy_end for a 1Q? It shouldn't, but if it does, it's a validation error or mismatch.
        # Actually, if quarter is 1Q/2Q/3Q, period should NOT equal fy_end (unless it's an irregular accounting period, but normally no).
        # We don't need to strictly raise an error here unless instructed. The user just said "CurrentFiscalYearEndDate は period に絶対に入れない".
        pass

    return period, fy_end, quarter

def format_fiscal_period(fiscal_year_end: str, quarter: str) -> str:
    """
    Returns display string like '2025年3月期1Q' or '2025年3月期FY'
    """
    if not fiscal_year_end:
        return ""
    try:
        d = datetime.fromisoformat(fiscal_year_end)
        return f"{d.year}年{d.month}月期{quarter}"
    except Exception:
        return f"{fiscal_year_end}期{quarter}"
