#!/usr/bin/env python3
"""Reclassify predictability v2 without changing any observation or Q value."""
from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
HISTORY = OUT / "quarterly_history_15_jquants_asof_v2.csv"
V2 = OUT / "quarterly_predictability_15_jquants_asof_v2.csv"
V3 = OUT / "quarterly_predictability_15_jquants_asof_v3.csv"
REPORT = OUT / "quarterly_predictability_15_jquants_asof_v3.md"
QS = ("1Q", "2Q", "3Q", "4Q")


def rnd(x, d=6): return round(x, d) if x is not None else None
def avg(v): return sum(v) / len(v) if v else None
def sd(v): return statistics.pstdev(v) if len(v) > 1 else 0.0
def dump(x): return json.dumps(x, ensure_ascii=False, separators=(",", ":"))
def stable(pattern): return pattern in {"stable", "seasonal_but_stable"}


def year_metrics(year):
    shares = {q: float(year[q]["q_sales_to_fy_sales"]) for q in QS}
    ops = {q: float(year[q]["standalone_operating_profit_million_yen"]) for q in QS}
    margins = {q: float(year[q]["standalone_operating_margin"]) for q in QS}
    s_rank = sorted(shares.items(), key=lambda x: (-x[1], x[0]))
    o_rank = sorted(ops.items(), key=lambda x: (-x[1], x[0]))
    annual_op = sum(ops.values())
    return {
        "shares": shares, "ops": ops, "margins": margins,
        "largest_q": s_rank[0][0], "largest_q_share": s_rank[0][1],
        "second_largest_q": s_rank[1][0], "second_largest_q_share": s_rank[1][1],
        "top_q_gap": s_rank[0][1] - s_rank[1][1],
        "max_op_q": o_rank[0][0], "max_op_value": o_rank[0][1],
        "second_op_q": o_rank[1][0], "second_op_value": o_rank[1][1],
        "top_op_gap_million_yen": o_rank[0][1] - o_rank[1][1],
        "annual_operating_profit_million_yen": annual_op,
        "annual_op_negative_or_near_zero": annual_op <= 0.5,
    }


def sales_class(years):
    if not years: return "insufficient_data", {"reason": "no_complete_year"}
    if len(years) < 3: return "limited_history", {"reason": "fewer_than_three_complete_years"}
    m = [year_metrics(y) for y in years]
    q_values = {q: [x["shares"][q] for x in m] for q in QS}
    q_stats = {q: {"mean": rnd(avg(v)), "max": rnd(max(v)), "min": rnd(min(v)), "stddev": rnd(sd(v)), "max_change": rnd(max(v)-min(v))} for q,v in q_values.items()}
    avg_std = avg([q_stats[q]["stddev"] for q in QS])
    max_change = max(q_stats[q]["max_change"] for q in QS)
    conc = [x["largest_q_share"] for x in m]
    gaps = [x["top_q_gap"] for x in m]
    maxq = [x["largest_q"] for x in m]
    mode_count = max(Counter(maxq).values())
    # Top-Q name is deliberately not a primary lumpy condition: small gaps are ties.
    if avg_std < .02 and max(conc) < .35:
        label = "stable"
    elif avg_std < .02 and avg(conc) >= .35 and mode_count >= 2:
        label = "seasonal_but_stable"
    elif avg_std >= .08 or max(conc) >= .45:
        label = "highly_lumpy"
    else:
        label = "lumpy"
    return label, {
        "q_share_stats": q_stats, "largest_q_by_year": maxq,
        "largest_q_share_by_year": [rnd(x) for x in conc],
        "second_largest_q_share_by_year": [rnd(x["second_largest_q_share"]) for x in m],
        "top_q_gap_by_year": [rnd(x) for x in gaps],
        "largest_q_concentration_range": rnd(max(conc)-min(conc)),
        "mean_q_share_stddev": rnd(avg_std), "max_q_share_change": rnd(max_change),
        "largest_q_mode_count": mode_count,
    }


def op_class(years, sales_pattern):
    if not years: return "insufficient_data", {"reason": "no_complete_year"}
    if len(years) < 3: return "limited_history", {"reason": "fewer_than_three_complete_years"}
    m = [year_metrics(y) for y in years]
    margins = {q: [x["margins"][q] for x in m] for q in QS}
    m_stats = {q: {"mean": rnd(avg(v)), "max": rnd(max(v)), "min": rnd(min(v)), "stddev": rnd(sd(v)), "max_change": rnd(max(v)-min(v))} for q,v in margins.items()}
    mean_margin_std = avg([m_stats[q]["stddev"] for q in QS])
    max_margin_change = max(m_stats[q]["max_change"] for q in QS)
    signs = {q: [0 if abs(x["ops"][q]) < .5 else (1 if x["ops"][q] > 0 else -1) for x in m] for q in QS}
    flips = sum(sum(a and b and a != b for a,b in zip(v,v[1:])) for v in signs.values())
    annual = [x["annual_operating_profit_million_yen"] for x in m]
    improving_loss = all(v <= 0.5 for v in annual) and all(b >= a for a,b in zip(annual,annual[1:])) and any(b > a for a,b in zip(annual,annual[1:]))
    maxq = [x["max_op_q"] for x in m]
    # Structural loss improvement takes precedence over volatile loss-quarter ranking.
    if improving_loss:
        label = "structural_trend"
    elif flips:
        label = "structural_change"
    elif sales_pattern == "highly_lumpy" and mean_margin_std >= .03:
        label = "lumpy"
    elif mean_margin_std < .02:
        label = "stable"
    elif mean_margin_std < .04:
        label = "lumpy"
    else:
        label = "highly_lumpy"
    return label, {
        "q_margin_stats": m_stats, "mean_q_margin_stddev": rnd(mean_margin_std),
        "max_q_margin_change": rnd(max_margin_change), "max_operating_profit_q_by_year": maxq,
        "second_operating_profit_q_by_year": [x["second_op_q"] for x in m],
        "top_operating_profit_gap_million_yen_by_year": [rnd(x["top_op_gap_million_yen"]) for x in m],
        "profit_sign_reversal_count": flips, "annual_operating_profit_by_year": [rnd(x) for x in annual],
        "annual_op_negative_or_near_zero": [x["annual_op_negative_or_near_zero"] for x in m],
        "improving_loss_trend": improving_loss,
    }


def gate(sales, op):
    if sales in {"limited_history", "insufficient_data"} or op in {"limited_history", "insufficient_data"}: return "insufficient"
    if stable(sales) and stable(op): return "high"
    if stable(sales) and op in {"structural_change", "structural_trend", "lumpy"}: return "medium"
    return "low"


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def main():
    history_bytes = HISTORY.read_bytes()
    with HISTORY.open(encoding="utf-8-sig", newline="") as f: history = list(csv.DictReader(f))
    with V2.open(encoding="utf-8-sig", newline="") as f: v2 = {r["code"]: r for r in csv.DictReader(f)}
    grouped = defaultdict(lambda: defaultdict(dict))
    names = {}
    for row in history:
        names[row["code"]] = row["company_name"]
        if row["selection_status"] == "selected_for_statistics": grouped[row["code"]][row["fiscal_year_end"]][row["quarter"]] = row
    rows = []
    for code in v2:
        years = [grouped[code][fy] for fy in sorted(grouped[code])]
        sales, sales_detail = sales_class(years)
        op, op_detail = op_class(years, sales)
        rows.append({
            "code": code, "company_name": names[code], "source_history_file": HISTORY.name,
            "source_history_sha256": hashlib.sha256(history_bytes).hexdigest(),
            "available_complete_fiscal_years": len(years),
            "selected_fiscal_year_ends": ";".join(sorted(grouped[code])),
            "sales_pattern_v2": v2[code]["sales_pattern"], "sales_pattern_v3": sales,
            "operating_profit_pattern_v2": v2[code]["operating_profit_pattern"], "operating_profit_pattern_v3": op,
            "quarterly_predictability_gate_v2": v2[code]["quarterly_predictability_gate"], "quarterly_predictability_gate_v3": gate(sales, op),
            "sales_vs_profit_state_v3": "sales_stable_profit_stable" if stable(sales) and stable(op) else ("sales_stable_profit_structural_trend" if stable(sales) and op == "structural_trend" else ("sales_stable_profit_unstable" if stable(sales) else ("both_unstable" if sales not in {"limited_history", "insufficient_data"} and op not in {"limited_history", "insufficient_data"} else "limited_history"))),
            "sales_metrics_json": dump(sales_detail), "operating_profit_metrics_json": dump(op_detail),
            "classification_rule_version": "v3_top_q_gap_and_margin_stability",
        })
    write_csv(V3, rows)
    counts = Counter(r["sales_pattern_v3"] for r in rows)
    op_counts = Counter(r["operating_profit_pattern_v3"] for r in rows)
    changes = [r for r in rows if r["sales_pattern_v2"] != r["sales_pattern_v3"] or r["operating_profit_pattern_v2"] != r["operating_profit_pattern_v3"] or r["quarterly_predictability_gate_v2"] != r["quarterly_predictability_gate_v3"]]
    lines = ["# 四半期予測可能性 v3（分類閾値監査）", "", f"- 入力: `{HISTORY.name}`（SHA-256 `{hashlib.sha256(history_bytes).hexdigest()}`）", "- 元観測・累計値・単独Q値は変更せず、分類指標と閾値のみを再計算。", "", "## 分類変更", "- 売上の最大Q名の変更だけではlumpyにしない。平均Q構成比標準偏差、最大Q集中度、top_q_gapで判定。", "- 売上stable: 平均Q構成比標準偏差 <2pt、最大Q集中度 <35%。", "- seasonal_but_stable: 同一最大Qが複数年度、集中度概ね35%以上、構成比ブレ <2pt。", "- highly_lumpy: 平均Q構成比標準偏差 >=8pt または最大Q集中度 >=45%。", "- 利益は最大利益Qの名称でなくQ営業利益率の分散・変化幅・符号反転で判定。連続赤字縮小はstructural_trend。", "", "## 件数", f"- sales: `{dump(dict(counts))}`", f"- operating profit: `{dump(dict(op_counts))}`", "", "## v2から変更", *[f"- {r['code']}: sales {r['sales_pattern_v2']}→{r['sales_pattern_v3']}; op {r['operating_profit_pattern_v2']}→{r['operating_profit_pattern_v3']}; gate {r['quarterly_predictability_gate_v2']}→{r['quarterly_predictability_gate_v3']}" for r in changes], "", "## 重点監査", *[f"- {r['code']}: sales metrics={r['sales_metrics_json']}; op metrics={r['operating_profit_metrics_json']}" for r in rows if r['code'] in {'2337','3547','2449','2484','3697','1418'}]]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(dump({"rows": len(rows), "changed": [r['code'] for r in changes], "sales_counts": counts, "op_counts": op_counts, "source_sha256": hashlib.sha256(history_bytes).hexdigest()}))

if __name__ == "__main__": main()
