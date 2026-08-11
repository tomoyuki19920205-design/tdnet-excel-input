"""Resolve J-Quants interim rows across fiscal-year-end transitions.

J-Quants summary data can retain the old fiscal-year end on interim rows when a
company changes its year end.  The raw payload is intentionally preserved; the
canonical period is resolved from the latest financial-statement FY actual that
has the same ``CurFYSt`` and a self-consistent ``CurPerEn``/``CurFYEn`` pair.
"""

from __future__ import annotations


def resolved_fiscal_year_end_sql(row_alias: str = "source") -> str:
    """Return a SQLite expression for the canonical fiscal-year end.

    The expression does not assume a twelve-month fiscal year and only changes
    interim periods.  Without an authoritative FY actual, the source value is
    retained.
    """

    return f"""\
CASE
  WHEN {row_alias}.type_of_current_period IN ('1Q', '2Q', '3Q')
       AND json_valid({row_alias}.raw_json)
       AND COALESCE(json_extract({row_alias}.raw_json, '$.CurFYSt'), '') <> ''
  THEN COALESCE(
    (
      SELECT fy.current_fiscal_year_end_date
      FROM jquants_financials_normalized AS fy
      WHERE fy.local_code = {row_alias}.local_code
        AND fy.type_of_current_period = 'FY'
        AND fy.type_of_document LIKE '%FinancialStatements%'
        AND json_valid(fy.raw_json)
        AND json_extract(fy.raw_json, '$.CurFYSt')
            = json_extract({row_alias}.raw_json, '$.CurFYSt')
        AND json_extract(fy.raw_json, '$.CurPerEn')
            = fy.current_fiscal_year_end_date
      ORDER BY fy.disclosed_date DESC
      LIMIT 1
    ),
    {row_alias}.current_fiscal_year_end_date
  )
  ELSE {row_alias}.current_fiscal_year_end_date
END"""


def transition_sibling_sql(recent_predicate: str, row_alias: str = "source") -> str:
    """Return a predicate that reselects interim siblings of a recent FY row."""

    return f"""\
(
  {row_alias}.type_of_current_period IN ('1Q', '2Q', '3Q')
  AND json_valid({row_alias}.raw_json)
  AND COALESCE(json_extract({row_alias}.raw_json, '$.CurFYSt'), '') <> ''
  AND EXISTS (
    SELECT 1
    FROM jquants_financials_normalized AS recent_fy
    WHERE recent_fy.local_code = {row_alias}.local_code
      AND recent_fy.type_of_current_period = 'FY'
      AND recent_fy.type_of_document LIKE '%FinancialStatements%'
      AND json_valid(recent_fy.raw_json)
      AND json_extract(recent_fy.raw_json, '$.CurFYSt')
          = json_extract({row_alias}.raw_json, '$.CurFYSt')
      AND ({recent_predicate})
  )
)"""
