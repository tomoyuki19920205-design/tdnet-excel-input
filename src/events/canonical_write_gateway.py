from src.events.pipeline_context import CanonicalWritePlan
from typing import Optional

ALLOWED_SOURCES = {
    'jquants',
    'jquants_earnings_summary',
    'jquants_forecast_fy',
    'jquants_nxf',
    'legacy_excel'
}

ALLOWED_METRICS = {
    'sales',
    'operating_profit',
    'ordinary_profit',
    'net_income',
    'gross_profit',
    'eps'
}

def validate_canonical_write_plan(plan: CanonicalWritePlan) -> CanonicalWritePlan:
    """
    Validates a CanonicalWritePlan before allowing it to be saved to DB.
    Checks period alignment, unit scaling, source visibility, and key formats.
    Mutates write_allowed and block_reason on the plan.
    """
    plan.write_allowed = False
    plan.block_reason = None
    
    # 1. Mandatory fields
    if not plan.ticker or not plan.period or not plan.quarter or not plan.metric:
        plan.block_reason = "Missing mandatory fields (ticker, period, quarter, metric)"
        return plan
        
    if plan.value is None:
        plan.block_reason = "Value is None"
        return plan
        
    # 1.5 Quarter validation
    if plan.quarter not in ("1Q", "2Q", "3Q", "4Q", "FY"):
        plan.block_reason = f"Invalid quarter for canonical write: {plan.quarter}"
        return plan
        
    # 2. Metric check
    if plan.metric not in ALLOWED_METRICS:
        plan.block_reason = f"Unsupported metric: {plan.metric}"
        return plan
        
    # 3. Unit scaling check
    if plan.unit != "millions_jpy":
        plan.block_reason = f"Unsupported unit: {plan.unit}. Must be millions_jpy"
        return plan
        
    # Prevent JPY raw values masquerading as millions (e.g. > 1 trillion millions = 1 quintillion JPY)
    # A value of 1,000,000 in millions is 1 trillion JPY.
    # The largest companies (Toyota) might have sales of 45,000,000 millions JPY (45 trillion).
    # If a value exceeds 100,000,000 (100 trillion), it's highly likely unscaled.
    if abs(plan.value) > 100_000_000:
        plan.block_reason = f"Value {plan.value} is abnormally large. Likely unscaled raw JPY."
        return plan
        
    # 4. Source check
    if plan.source not in ALLOWED_SOURCES:
        plan.block_reason = f"Source '{plan.source}' is blocked. Must be one of {ALLOWED_SOURCES}"
        return plan
        
    # 5. Period matching
    # In a full implementation, we'd check if period matches the master. 
    # For now, we block 2026-12-31 for 2408 if it's supposed to be 2026-12-20
    # The `CanonicalWritePlan` already represents the plan to write. If the period ends in -31 but should end in -20,
    # the pipeline context should have resolved it. If the pipeline passes a bad period here, we can flag it.
    if plan.ticker == "2408" and plan.period.endswith("-31"):
        plan.block_reason = "Period mismatch for 2408: should not end in -31 (expected -20)"
        return plan
        
    # 6. source_row_key check
    expected_key = CanonicalWritePlan.generate_source_row_key(
        plan.ticker, plan.period, plan.quarter, plan.metric, plan.source, plan.filing_id
    )
    if plan.source_row_key != expected_key:
        plan.block_reason = f"source_row_key mismatch. Expected {expected_key}, got {plan.source_row_key}"
        return plan

    # All checks passed
    plan.write_allowed = True
    return plan

def build_normalized_canonical_write_plan(
    ticker: str,
    period_raw: str,
    quarter_raw: str,
    metrics_raw: dict,
    guidance_raw: dict,
    filing_id: str
) -> list[CanonicalWritePlan]:
    """
    既存の保存直前データを受け取り、正規化された CanonicalWritePlan のリストを生成する。
    period, source の正規化を行い、FY予想もプラン化する。
    """
    plans = []
    
    # 1. Period Normalization
    normalized_period = period_raw
    if ticker == "2408" and period_raw and period_raw.endswith("-31"):
        normalized_period = period_raw[:8] + "20"
        
    # 2. Actuals (source="jquants")
    for m_name, m_val in metrics_raw.items():
        if m_val is None:
            continue
            
        plan = CanonicalWritePlan(
            ticker=ticker,
            period=normalized_period or "unknown",
            quarter=quarter_raw,
            metric=m_name,
            value=m_val, # Assume already scaled to millions_jpy
            unit="millions_jpy",
            source="jquants",
            filing_id=filing_id
        )
        plan.validate_and_prepare()
        plans.append(plan)
        
    # 3. Guidance (source="jquants_forecast_fy", quarter="FY")
    for g_name, g_val in guidance_raw.items():
        if g_val is None:
            continue
            
        metric_name = None
        if g_name == "sales_forecast":
            metric_name = "sales"
        elif g_name == "op_forecast":
            metric_name = "operating_profit"
            
        if metric_name:
            plan = CanonicalWritePlan(
                ticker=ticker,
                period=normalized_period or "unknown",
                quarter="FY",
                metric=metric_name,
                value=round(g_val / 1_000_000), # Guidance is raw JPY, scale to millions
                unit="millions_jpy",
                source="jquants_forecast_fy",
                filing_id=filing_id
            )
            plan.validate_and_prepare()
            plans.append(plan)
            
    return plans

def should_allow_real_canonical_db_apply(ticker: str) -> dict:
    import os
    
    apply_flag = os.getenv("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY") == "1"
    canary_flag = os.getenv("EARNINGS_CANONICAL_WRITE_APPLY_CANARY") == "1"
    
    allowed_tickers_env = os.getenv("EARNINGS_CANONICAL_WRITE_APPLY_TICKERS", "")
    allowed_tickers = [t.strip() for t in allowed_tickers_env.split(",") if t.strip()]
    ticker_allowed = ticker in allowed_tickers
    
    allow_db_token = os.getenv("EARNINGS_CANONICAL_WRITE_APPLY_ALLOW_DB", "")
    allow_db_ok = allow_db_token == "I_UNDERSTAND_WRITE_CANONICAL_FINANCIALS"
    
    reasons = []
    if not apply_flag:
        reasons.append("EARNINGS_CANONICAL_WRITE_REPLACE_APPLY is not 1")
    if not canary_flag:
        reasons.append("EARNINGS_CANONICAL_WRITE_APPLY_CANARY is not 1")
    if not ticker_allowed:
        reasons.append(f"Ticker {ticker} is not in EARNINGS_CANONICAL_WRITE_APPLY_TICKERS")
    if not allow_db_ok:
        reasons.append("EARNINGS_CANONICAL_WRITE_APPLY_ALLOW_DB is not I_UNDERSTAND_WRITE_CANONICAL_FINANCIALS")
        
    allowed = apply_flag and canary_flag and ticker_allowed and allow_db_ok
    
    return {
        "allowed": allowed,
        "reasons": reasons,
        "apply_flag_on": apply_flag,
        "canary_flag_on": canary_flag,
        "ticker_allowed": ticker_allowed,
        "allow_db_token_ok": allow_db_ok
    }

def apply_normalized_canonical_write_plans(
    plans: list[CanonicalWritePlan],
    writer_func,
    config: dict = None
) -> dict:
    """
    Gateway通過済みの CanonicalWritePlan リストを実際の DB writer へ渡す。
    全件 write_allowed=True でない場合は、安全のため1件も保存しない。
    既存の source_row_key がDBに存在する場合は、内容が一致すれば skip、不一致なら conflict として保存をブロックする。
    """
    blocked_reasons = []
    source_row_keys = []
    
    actuals_count = 0
    forecasts_count = 0
    
    for p in plans:
        if not p.write_allowed:
            blocked_reasons.append(p.block_reason)
        else:
            source_row_keys.append(p.source_row_key)
            if p.quarter == "FY" and "forecast" in p.source:
                forecasts_count += 1
            else:
                actuals_count += 1
                
    if blocked_reasons:
        return {
            "status": "blocked",
            "total_plans": len(plans),
            "write_allowed_count": len(plans) - len(blocked_reasons),
            "skipped_existing_count": 0,
            "conflict_count": 0,
            "would_write_count": 0,
            "blocked_reasons": blocked_reasons,
            "skipped_source_row_keys": [],
            "conflict_source_row_keys": [],
            "actuals_count": 0,
            "forecasts_count": 0,
            "source_row_keys": [],
            "db_write_attempted": False
        }
        
    if not plans:
        return {
            "status": "success",
            "total_plans": 0,
            "write_allowed_count": 0,
            "skipped_existing_count": 0,
            "conflict_count": 0,
            "would_write_count": 0,
            "blocked_reasons": [],
            "skipped_source_row_keys": [],
            "conflict_source_row_keys": [],
            "actuals_count": 0,
            "forecasts_count": 0,
            "source_row_keys": [],
            "db_write_attempted": False
        }

    # 1. Fetch existing rows if config is provided
    existing_rows_map = {}
    if config and source_row_keys:
        from lib.pipeline.db import supabase_select
        keys_str = ",".join(source_row_keys)
        params = {"select": "*", "source_row_key": f"in.({keys_str})"}
        res = supabase_select("canonical_financials", params=params, config=config)
        if isinstance(res, list):
            for row in res:
                existing_rows_map[row["source_row_key"]] = row

    # 2. Check for skipped / conflict
    skipped_existing_keys = []
    conflict_keys = []
    conflict_reasons = []
    plans_to_write = []
    
    for p in plans:
        if p.source_row_key in existing_rows_map:
            ex = existing_rows_map[p.source_row_key]
            # check fields
            is_match = (
                str(float(ex.get("value")) if ex.get("value") is not None else "None") == str(float(p.value) if p.value is not None else "None") and
                str(ex.get("period")) == str(p.period) and
                str(ex.get("quarter")) == str(p.quarter) and
                str(ex.get("metric")) == str(p.metric) and
                str(ex.get("source")) == str(p.source) and
                str(ex.get("unit")) == str(p.unit)
            )
            if is_match:
                skipped_existing_keys.append(p.source_row_key)
            else:
                conflict_keys.append(p.source_row_key)
                conflict_reasons.append(f"Conflict on {p.source_row_key}: db_value={ex.get('value')} vs plan_value={p.value}")
        else:
            plans_to_write.append(p)
            
    if conflict_keys:
        return {
            "status": "conflict",
            "total_plans": len(plans),
            "write_allowed_count": len(plans),
            "skipped_existing_count": len(skipped_existing_keys),
            "conflict_count": len(conflict_keys),
            "would_write_count": 0,
            "blocked_reasons": conflict_reasons,
            "skipped_source_row_keys": skipped_existing_keys,
            "conflict_source_row_keys": conflict_keys,
            "actuals_count": actuals_count,
            "forecasts_count": forecasts_count,
            "source_row_keys": source_row_keys,
            "db_write_attempted": False
        }

    # All allowed and no conflicts
    db_result = writer_func(plans_to_write, config) if writer_func and plans_to_write else {}
    
    return {
        "status": "success",
        "total_plans": len(plans),
        "write_allowed_count": len(plans),
        "skipped_existing_count": len(skipped_existing_keys),
        "conflict_count": 0,
        "would_write_count": len(plans_to_write),
        "blocked_reasons": [],
        "skipped_source_row_keys": skipped_existing_keys,
        "conflict_source_row_keys": [],
        "actuals_count": actuals_count,
        "forecasts_count": forecasts_count,
        "source_row_keys": source_row_keys,
        "db_write_attempted": bool(writer_func) and len(plans_to_write) > 0,
        "db_result": db_result
    }


def canonical_writer_adapter(plans_to_write: list['CanonicalWritePlan'], config: dict = None) -> dict:
    """
    Adapter to pass Normalized CanonicalWritePlan to the real DB writer.
    Ensures unit is properly propagated.
    """
    from lib.pipeline.canonical_writer import write_financials_canonical
    
    written_count = 0
    responses = []
    for p in plans_to_write:
        if p.unit != 'millions_jpy':
            raise ValueError(f'canonical_writer_adapter received invalid unit: {p.unit}')
            
        res = write_financials_canonical(
            ticker=p.ticker,
            period=p.period,
            quarter=p.quarter,
            metrics_dict={p.metric: p.value},
            source=p.source,
            filing_id=p.filing_id,
            unit=p.unit,
            config=config
        )
        responses.append(res)
        written_count += res.get('written', 0)
        
    return {'written': written_count, 'supabase_response': responses}
