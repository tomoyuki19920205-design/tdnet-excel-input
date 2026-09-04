"""Financial identity and unit validation; ambiguous evidence stays missing."""
from datetime import date
import calendar
from decimal import Decimal, InvalidOperation

FORECAST_SOURCES = frozenset({'tdnet_forecast', 'jquants_nxf', 'jquants_forecast_fy',
                             'jquants_forecast_next_fy', 'jquants_forecast'})
UNIT_FACTORS = {'JPY': Decimal('0.000001'), '円': Decimal('0.000001'),
                '千円': Decimal('0.001'), 'thousands_jpy': Decimal('0.001'),
                '百万円': Decimal(1), 'million_yen': Decimal(1), 'millions_jpy': Decimal(1)}

def normalize_amount(value, unit, metric='sales'):
    if value is None:
        return None
    factor = Decimal(1) if metric == 'eps' and unit in ('JPY', '円', 'yen_per_share') else UNIT_FACTORS.get(unit)
    if factor is None:
        return None
    try:
        result = Decimal(str(value)) * factor
        return float(result) if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None

def duration_quarter(start, end, fiscal_start=None, fiscal_end=None):
    """Classify cumulative duration, not a fiscal-year label or context name."""
    try:
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        if fiscal_start and start != date.fromisoformat(fiscal_start):
            return None
        days = (end-start).days+1
        if days <= 0:
            return None
        if fiscal_start and fiscal_end and end == date.fromisoformat(fiscal_end):
            return 'FY'  # Explicit complete fiscal duration also permits short fiscal years.
        for lo, hi, quarter in ((75,105,'1Q'),(165,195,'2Q'),(255,285,'3Q'),(350,380,'FY')):
            if lo <= days <= hi:
                return quarter
    except (TypeError, ValueError):
        pass
    return None

def actual_period_is_valid(period, quarter, disclosure_datetime=None):
    if quarter not in ('1Q','2Q','3Q','FY','4Q'):
        return False
    try:
        if len(period) == 7:
            year, month = map(int, period.split("-"))
            period = f"{period}-{calendar.monthrange(year, month)[1]:02d}"
        end = date.fromisoformat(period)
        asof = date.fromisoformat(str(disclosure_datetime)[:10]) if disclosure_datetime else date.today()
        return quarter not in ('FY','4Q') or end <= asof
    except (TypeError, ValueError):
        return False
