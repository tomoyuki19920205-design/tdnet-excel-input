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


def stored_quarter_is_completed_actual(period, quarter, evidence_datetime):
    """A stored actual must have evidence dated on/after its own quarter end.

    SQLite imports may contain a forecast in actual columns. A later sync time
    does not promote that forecast into an actual; use the row's evidence time.
    """
    try:
        period_text = str(period)
        if len(period_text) == 7:
            year, month = map(int, period_text.split("-"))
            fiscal_end = date(year, month, calendar.monthrange(year, month)[1])
        else:
            fiscal_end = date.fromisoformat(period_text[:10])
        q = 4 if quarter in ("FY", "4Q") else int(str(quarter)[0])
        if q not in (1, 2, 3, 4):
            return False
        month_index = fiscal_end.year * 12 + fiscal_end.month - 1 - (4 - q) * 3
        year, month_zero = divmod(month_index, 12)
        quarter_end = date(year, month_zero + 1, calendar.monthrange(year, month_zero + 1)[1])
        evidence_date = date.fromisoformat(str(evidence_datetime)[:10])
        return quarter_end <= evidence_date <= date.today()
    except (TypeError, ValueError, IndexError):
        return False
