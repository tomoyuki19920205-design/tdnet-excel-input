#!/usr/bin/env python3
"""Build the 15-company quarterly predictability v2 from J-Quants observations.

Read-only inputs: data/jquants.db.jquants_financials_normalized.  The cutoff is
intentionally date-exclusive because J-Quants ``disclosed_date`` has no time.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common_ticker import normalize_ticker

DB = ROOT / "data" / "jquants.db"
OUT = ROOT / "output"
CUTOFF_JST = "2026-07-15T15:20:00+09:00"
SAFE_DISCLOSED_DATE = "2026-07-15"  # Strictly excluded, since no time is available.
QS = ("1Q", "2Q", "3Q", "4Q")
SOURCE_Q = {"1Q": "1Q", "2Q": "2Q", "3Q": "3Q", "4Q": "FY"}
TARGETS = [
    ("1418", "インターライフ"), ("205A", "ロゴスHD"),
    ("3547", "ユニシアHD"), ("3558", "ジェイドグループ"),
    ("3697", "SHIFT"), ("2164", "地域新聞社"),
    ("2168", "パソナグループ"), ("2449", "プラップジャパン"),
    ("4197", "アスマーク"), ("9238", "バリュークリエーション"),
    ("198A", "ポストプライム"), ("2337", "いちご"),
    ("244A", "グロースエクパートナーズ"), ("2484", "出前館"),
    ("280A", "TMH"),
]
TARGET_CODES = {code for code, _ in TARGETS}


def rnd(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pstdev(values: list[float]) -> float | None:
    return statistics.pstdev(values) if len(values) >= 2 else None


def js(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_text(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def actual_row(row: dict) -> bool:
    """A forecast/revision row with neither PL value is not a quarterly actual."""
    return row["net_sales"] is not None or row["operating_profit"] is not None


def belongs_to_target(local_code: str, target_code: str) -> bool:
    """Use shared normalization, but never merge a numeric predecessor into an alpha IPO.

    J-Quants has both numeric five-character codes such as ``19800`` and direct
    alpha codes such as ``198A0``.  ``normalize_ticker`` maps the former through
    its legacy alias table, so an additional raw-code guard is necessary for an
    alpha target to avoid inheriting an unrelated predecessor's history.
    """
    raw = str(local_code).strip().upper()
    if normalize_ticker(raw) != target_code:
        return False
    return target_code in raw if any(c.isalpha() for c in target_code) else not any(c.isalpha() for c in raw)


def quarter_sort_key(label: str) -> int:
    return {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}.get(label, 9)


def select_observations(conn: sqlite3.Connection) -> tuple[dict, int, int]:
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("""
        SELECT rowid AS observation_id, local_code, disclosed_date,
               current_fiscal_year_end_date AS fiscal_year_end,
               type_of_current_period AS source_quarter,
               type_of_document, net_sales, operating_profit, fetched_at, raw_json
        FROM jquants_financials_normalized
        WHERE disclosed_date < ?
        ORDER BY local_code, current_fiscal_year_end_date,
                 type_of_current_period, disclosed_date, rowid
    """, (SAFE_DISCLOSED_DATE,))]
    day_excluded = conn.execute("""
        SELECT COUNT(*) FROM jquants_financials_normalized
        WHERE disclosed_date = ?
    """, (SAFE_DISCLOSED_DATE,)).fetchone()[0]
    target_day_excluded = conn.execute("""
        SELECT local_code FROM jquants_financials_normalized WHERE disclosed_date = ?
    """, (SAFE_DISCLOSED_DATE,)).fetchall()
    target_day_excluded = sum(
        normalize_ticker(r[0]) in TARGET_CODES and belongs_to_target(r[0], normalize_ticker(r[0]))
        for r in target_day_excluded
    )

    candidates: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        code = normalize_ticker(row["local_code"])
        if code not in TARGET_CODES or not belongs_to_target(row["local_code"], code) or row["source_quarter"] not in ("1Q", "2Q", "3Q", "FY"):
            continue
        if actual_row(row):
            row["code"] = code
            candidates[(code, row["fiscal_year_end"], row["source_quarter"])].append(row)

    # The latest eligible disclosure wins. Rowid is a deterministic tie-breaker.
    selected = {
        key: max(group, key=lambda r: (r["disclosed_date"], r["observation_id"]))
        for key, group in candidates.items()
    }
    return selected, day_excluded, target_day_excluded


def row_for_history(code: str, name: str, fy: str, q: str, group: dict[str, dict], selected_years: set[str]) -> dict:
    srcq = SOURCE_Q[q]
    source = group.get(srcq)
    cumulative_sales = source["net_sales"] / 1_000_000 if source and source["net_sales"] is not None else None
    cumulative_op = source["operating_profit"] / 1_000_000 if source and source["operating_profit"] is not None else None
    complete = all(group.get(x) and group[x]["net_sales"] is not None and group[x]["operating_profit"] is not None for x in ("1Q", "2Q", "3Q", "FY"))
    standalone_sales = standalone_op = margin = sales_share = op_share = None
    if complete:
        previous = {"1Q": None, "2Q": "1Q", "3Q": "2Q", "4Q": "3Q"}[q]
        prev_sales = group[previous]["net_sales"] / 1_000_000 if previous else 0
        prev_op = group[previous]["operating_profit"] / 1_000_000 if previous else 0
        standalone_sales = cumulative_sales - prev_sales
        standalone_op = cumulative_op - prev_op
        fy_sales = group["FY"]["net_sales"] / 1_000_000
        fy_op = group["FY"]["operating_profit"] / 1_000_000
        margin = standalone_op / standalone_sales if standalone_sales else None
        sales_share = standalone_sales / fy_sales if fy_sales else None
        op_share = standalone_op / fy_op if fy_op else None
    return {
        "code": code, "company_name": name, "analysis_as_of_jst": CUTOFF_JST,
        "disclosed_date_rule": f"< {SAFE_DISCLOSED_DATE}", "fiscal_year_end": fy,
        "quarter": q, "source_quarter": srcq,
        "selection_status": "selected_for_statistics" if complete and fy in selected_years else ("complete_not_latest_three" if complete else "incomplete_year"),
        "cumulative_sales_million_yen": rnd(cumulative_sales),
        "cumulative_operating_profit_million_yen": rnd(cumulative_op),
        "cumulative_ordinary_profit_million_yen": None,
        "cumulative_net_income_million_yen": None, "cumulative_eps": None,
        "standalone_sales_million_yen": rnd(standalone_sales),
        "standalone_operating_profit_million_yen": rnd(standalone_op),
        "standalone_ordinary_profit_million_yen": None,
        "standalone_net_income_million_yen": None, "standalone_eps": None,
        "standalone_operating_margin": rnd(margin), "q_sales_to_fy_sales": rnd(sales_share),
        "q_operating_profit_to_fy_operating_profit": rnd(op_share),
        "source_observation_id": source["observation_id"] if source else None,
        "source_local_code": source["local_code"] if source else None,
        "source_disclosed_date": source["disclosed_date"] if source else None,
        "source_fetched_at": source["fetched_at"] if source else None,
        "source_type_of_document": source["type_of_document"] if source else None,
        "source_raw_json_sha256": sha256_text(source["raw_json"]) if source else None,
        "source_raw_json_identifiers": js({"local_code": source["local_code"], "type_of_document": source["type_of_document"]}) if source else None,
        "filing_id": None, "source_doc_id": None, "canonical_id": None,
        "lineage_id": None, "correction_flag": None,
        "quality_reason": "complete_cumulative_pl" if complete else "missing_sales_or_operating_profit_for_required_cumulative_quarter",
    }


def selected_year_data(group: dict[str, dict]) -> dict[str, dict] | None:
    if not all(group.get(x) and group[x]["net_sales"] is not None and group[x]["operating_profit"] is not None for x in ("1Q", "2Q", "3Q", "FY")):
        return None
    cumulative_sales = {q: group[SOURCE_Q[q]]["net_sales"] / 1_000_000 for q in QS}
    cumulative_op = {q: group[SOURCE_Q[q]]["operating_profit"] / 1_000_000 for q in QS}
    result = {}
    for q, prior in (("1Q", None), ("2Q", "1Q"), ("3Q", "2Q"), ("4Q", "3Q")):
        sales = cumulative_sales[q] - (cumulative_sales[prior] if prior else 0)
        op = cumulative_op[q] - (cumulative_op[prior] if prior else 0)
        result[q] = {
            "sales": sales, "operating_profit": op,
            "margin": op / sales if sales else None,
            "sales_share": sales / cumulative_sales["4Q"] if cumulative_sales["4Q"] else None,
        }
    return result


def classify_sales(years: list[dict[str, dict]]) -> tuple[str, dict]:
    if not years:
        return "insufficient_data", {"reason": "no_complete_fiscal_year"}
    if len(years) < 3:
        return "limited_history", {"reason": "fewer_than_three_complete_fiscal_years"}
    q_stats = {q: [y[q]["sales_share"] for y in years] for q in QS}
    q_std = {q: pstdev(v) for q, v in q_stats.items()}
    max_q = [max(QS, key=lambda q: y[q]["sales_share"]) for y in years]
    match = max(Counter(max_q).values())
    concentration = [max(y[q]["sales_share"] for q in QS) for y in years]
    mean_std = avg([v for v in q_std.values() if v is not None]) or 0
    conc_avg = avg(concentration) or 0
    if match == len(years) and mean_std <= 0.06:
        pattern = "seasonal_but_stable" if conc_avg >= 0.40 else "stable"
    elif match <= 1 and mean_std >= 0.12:
        pattern = "highly_lumpy"
    else:
        pattern = "lumpy"
    return pattern, {"q_share_stats": {q: {"mean": rnd(avg(v)), "max": rnd(max(v)), "min": rnd(min(v)), "stddev": rnd(q_std[q])} for q, v in q_stats.items()}, "max_sales_q_by_year": max_q, "max_sales_q_match_count": match, "max_q_concentration_by_year": [rnd(v) for v in concentration], "max_q_concentration_avg": rnd(conc_avg), "mean_q_share_stddev": rnd(mean_std)}


def classify_operating_profit(years: list[dict[str, dict]]) -> tuple[str, dict]:
    if not years:
        return "insufficient_data", {"reason": "no_complete_fiscal_year"}
    if len(years) < 3:
        return "limited_history", {"reason": "fewer_than_three_complete_fiscal_years"}
    margins = {q: [y[q]["margin"] for y in years if y[q]["margin"] is not None] for q in QS}
    margin_std = {q: pstdev(v) for q, v in margins.items()}
    max_q = [max(QS, key=lambda q: y[q]["operating_profit"]) for y in years]
    match = max(Counter(max_q).values())
    signs = {q: [0 if abs(y[q]["operating_profit"]) < 0.5 else (1 if y[q]["operating_profit"] > 0 else -1) for y in years] for q in QS}
    sign_flips = sum(sum(a != 0 and b != 0 and a != b for a, b in zip(v, v[1:])) for v in signs.values())
    annual_op = [sum(y[q]["operating_profit"] for q in QS) for y in years]
    abs_conc = [max(abs(y[q]["operating_profit"]) for q in QS) / sum(abs(y[q]["operating_profit"]) for q in QS) if sum(abs(y[q]["operating_profit"]) for q in QS) else None for y in years]
    mean_std = avg([v for v in margin_std.values() if v is not None]) or 0
    if sign_flips >= 3 or (match <= 1 and mean_std >= 0.15):
        pattern = "highly_lumpy"
    elif sign_flips >= 1:
        pattern = "structural_change"
    elif match == len(years) and mean_std <= 0.025:
        pattern = "stable"
    elif match == len(years) and mean_std <= 0.04:
        pattern = "seasonal_but_stable"
    else:
        pattern = "lumpy"
    return pattern, {"q_margin_stats": {q: {"mean": rnd(avg(v)), "max": rnd(max(v)), "min": rnd(min(v)), "stddev": rnd(margin_std[q])} for q, v in margins.items()}, "max_operating_profit_q_by_year": max_q, "max_operating_profit_q_match_count": match, "profit_sign_reversal_count": sign_flips, "annual_operating_profit_by_year": [rnd(v) for v in annual_op], "max_absolute_q_op_concentration_by_year": [rnd(v) for v in abs_conc], "mean_q_margin_stddev": rnd(mean_std)}


def gate_for(sales_pattern: str, op_pattern: str) -> str:
    stable = {"stable", "seasonal_but_stable"}
    insufficient = {"limited_history", "insufficient_data"}
    if sales_pattern in insufficient or op_pattern in insufficient:
        return "insufficient"
    if sales_pattern in stable and op_pattern in stable:
        return "high"
    if sales_pattern in stable or op_pattern in stable:
        return "medium"
    return "low"


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True) as conn:
        selected, day_excluded, target_day_excluded = select_observations(conn)
    by_company: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for (code, fy, q), row in selected.items():
        by_company[code][fy][q] = row

    history: list[dict] = []
    summary: list[dict] = []
    for code, name in TARGETS:
        groups = by_company.get(code, {})
        complete_fys = [fy for fy, group in sorted(groups.items()) if selected_year_data(group) is not None]
        chosen = set(complete_fys[-3:])
        selected_years = [selected_year_data(groups[fy]) for fy in complete_fys[-3:]]
        for fy in sorted(groups):
            for q in QS:
                history.append(row_for_history(code, name, fy, q, groups[fy], chosen))
        if not groups:
            for q in QS:
                history.append(row_for_history(code, name, None, q, {}, set()))
        sales_pattern, sales_detail = classify_sales(selected_years)
        op_pattern, op_detail = classify_operating_profit(selected_years)
        summary.append({
            "code": code, "company_name": name, "analysis_as_of_jst": CUTOFF_JST,
            "disclosed_date_rule": f"disclosed_date < {SAFE_DISCLOSED_DATE}",
            "jquants_code_normalization": "src.common_ticker.normalize_ticker",
            "available_complete_fiscal_years": len(complete_fys),
            "selected_fiscal_year_ends": ";".join(complete_fys[-3:]),
            "sales_pattern": sales_pattern, "operating_profit_pattern": op_pattern,
            "quarterly_predictability_gate": gate_for(sales_pattern, op_pattern),
            "sales_pattern_basis_json": js(sales_detail),
            "operating_profit_pattern_basis_json": js(op_detail),
            "sales_vs_profit_state": "sales_stable_profit_unstable" if sales_pattern in {"stable", "seasonal_but_stable"} and op_pattern not in {"stable", "seasonal_but_stable"} else ("both_unstable" if sales_pattern not in {"stable", "seasonal_but_stable", "limited_history", "insufficient_data"} and op_pattern not in {"stable", "seasonal_but_stable", "limited_history", "insufficient_data"} else "other"),
            "source_table": "jquants_financials_normalized",
            "ordinary_profit_status": "not_available_in_source_table",
            "net_income_status": "not_available_in_source_table",
            "eps_status": "not_available_in_source_table",
        })

    write_csv(OUT / "quarterly_history_15_jquants_asof_v2.csv", history)
    write_csv(OUT / "quarterly_predictability_15_jquants_asof_v2.csv", summary)
    target_history = [r for r in history if r["code"] == "2337" and r["selection_status"] == "selected_for_statistics"]
    shares = {r["fiscal_year_end"]: {} for r in target_history}
    for row in target_history:
        shares[row["fiscal_year_end"]][row["quarter"]] = row["q_sales_to_fy_sales"]
    checks = {"2024-02-29": [0.123, 0.198, 0.144, 0.536], "2025-02-28": [0.300, 0.134, 0.250, 0.315], "2026-02-28": [0.135, 0.416, 0.237, 0.212]}
    check_results = {fy: all(abs((shares.get(fy, {}).get(q) or -99) - expected) <= 0.003 for q, expected in zip(QS, expected_values)) for fy, expected_values in checks.items()}
    if not all(check_results.values()):
        raise RuntimeError(f"2337 Q sales-share reproduction failed: {js({'actual': shares, 'checks': check_results})}")
    lines = [
        "# 四半期予測可能性 v2（J-Quants元観測・as-of）", "",
        f"- 基準日時: {CUTOFF_JST}",
        f"- 安全側の開示日条件: `disclosed_date < {SAFE_DISCLOSED_DATE}`",
        "- 正本: `data/jquants.db` / `jquants_financials_normalized`",
        "- Viewer照合先: `api_latest_financials_canonical`（値照合のみ）",
        f"- 基準日当日の除外: 全銘柄 {day_excluded}件、対象15社 {target_day_excluded}件", "",
        "## 2337 売上構成比再現",
        f"- 実測: `{js(shares)}`", f"- 許容差 ±0.3pt: `{js(check_results)}`", "",
        "## 分類ルール",
        "- 3完全年度未満は `limited_history`、完全年度なしは `insufficient_data`。",
        "- 売上はQ売上構成比の標準偏差、最大Q一致数、最大Q集中度で分類。",
        "- 営業利益はQ営業利益率、最大利益Q一致数、黒字赤字の符号反転、絶対額集中度で別分類。",
        "- gateは両方安定でhigh、片方だけ安定でmedium、両方不安定でlow、履歴不足でinsufficient。", "",
        "## 銘柄別結果",
    ]
    for r in summary:
        lines.append(f"- {r['code']} {r['company_name']}: 年度数={r['available_complete_fiscal_years']} / sales={r['sales_pattern']} / op={r['operating_profit_pattern']} / gate={r['quarterly_predictability_gate']}")
    lines.extend(["", "## 制約", "- J-Quantsの`disclosed_date`は日付のみで、同日内の開示時刻は判定できないため、基準日当日を全除外した。", "- 元表は売上・営業利益のみ。経常利益、純利益、EPSはnull。", "- 観測ID・開示日・取得日・raw JSON識別ハッシュは履歴CSVに保存。"])
    (OUT / "quarterly_predictability_15_jquants_asof_v2.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(js({"history_rows": len(history), "summary_rows": len(summary), "day_excluded": day_excluded, "target_day_excluded": target_day_excluded, "2337_checks": check_results}))


if __name__ == "__main__":
    main()
