#!/usr/bin/env python3
"""Classify timing risk from existing as-of snapshots and v3 statistics only.

No database, Web, PDF retrieval, result label, or post-cutoff data is read.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
V3 = OUT / "quarterly_predictability_15_jquants_asof_v3.csv"
CSV_OUT = OUT / "quarterly_timing_risk_15_asof_v1.csv"
MD_OUT = OUT / "quarterly_timing_risk_15_asof_v1.md"
CUTOFF = "2026-07-15T15:20:00+09:00"

# Assessments are deliberately evidence-bounded.  ``unknown`` means that the
# saved as-of material did not establish the recognition trigger well enough.
ASSESSMENTS = {
    "1418": dict(model="mixed", recurring="medium", lump="medium", visibility="none", risk="medium", confidence="medium", date="2026-04-14T16:00:00+09:00", source="2026年2月期 決算短信〔日本基準〕(連結); 有価証券報告書 第16期", evidence="内装工事・音響照明の大型案件『完工』が増収益要因。一方で保守サービス受注増も記載。受注残・対象1Qの完工予定は非開示。", missing="対象1Qの案件別完工・検収予定、受注残、工事別利益寄与"),
    "205A": dict(model="project_completion", recurring="low", lump="high", visibility="none", risk="high", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は戸建住宅の企画・販売・施工。対象Qの引渡し・検収予定、受注残は確認できない。", missing="引渡し戸数・時期、受注残、完成基準か進行基準かの会計方針"),
    "3547": dict(model="recurring", recurring="high", lump="low", visibility="not_applicable", risk="low", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は教育・保育関連サービス。対象Qの個別案件計上予定は確認できないが、定常サービスとしてのみ確認。", missing="契約形態別売上、季節要因、対象Qの定員・稼働KPI"),
    "3558": dict(model="recurring", recurring="high", lump="low", visibility="not_applicable", risk="low", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はファッションEC・ブランド流通。定常的な日常販売モデルとして確認され、特定案件の検収・引渡しは確認されない。", missing="ブランド別取引条件、在庫・販促施策、対象Qの大型取引有無"),
    "3697": dict(model="project_progress", recurring="medium", lump="medium", visibility="none", risk="medium", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はソフトウェア品質保証・DX支援。IT案件の稼働・進捗が売上経路として示されるが、対象Qの案件進捗・受注残は非開示。", missing="対象Qの受注残、案件別進捗、準委任・請負別売上構成"),
    "2164": dict(model="unknown", recurring="unknown", lump="unknown", visibility="unknown", risk="unknown", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は地域情報誌・販促支援にとどまり、広告掲載・制作・販促支援のどの時点で売上認識するかを確認できない。", missing="契約・掲載・納品時の認識方針、定期掲載売上比率、対象Qの案件予定"),
    "2168": dict(model="recurring", recurring="high", lump="low", visibility="not_applicable", risk="low", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は人材サービス・BPO・地方創生。人材・BPOの継続サービス収入を主要経路として確認するが、個別案件の対象Q資料はない。", missing="各事業の売上比率、契約期間、地方創生案件の一括計上有無"),
    "2449": dict(model="mixed", recurring="medium", lump="medium", visibility="none", risk="medium", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はPR・コミュニケーション支援。継続支援とキャンペーン等の個別プロジェクトの重要度・認識時点は確認できない。", missing="リテナー比率、プロジェクト納品・イベント時の売上比率、対象Q案件"),
    "4197": dict(model="unknown", recurring="unknown", lump="unknown", visibility="unknown", risk="unknown", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は市場調査・リサーチ支援にとどまり、調査の実施・納品・検収のいずれで売上認識するかを確認できない。", missing="収益認識方針、継続調査比率、対象Qの納品・検収予定"),
    "9238": dict(model="unknown", recurring="unknown", lump="unknown", visibility="unknown", risk="unknown", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はマーケティングDX・不動産DX。各事業の定常収益と案件・成果報酬収益の比率および認識時点は確認できない。", missing="事業別収益認識方針、案件・成果報酬比率、対象Qの計上予定"),
    "198A": dict(model="recurring", recurring="high", lump="low", visibility="not_applicable", risk="low", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明でサブスクリプションと広告収益によるプラットフォーム収益を確認。対象FYの個別契約・広告案件の時期は不明。", missing="サブスクリプション対広告の比率、広告大型案件、対象QのKPI"),
    "2337": dict(model="mixed", recurring="high", lump="high", visibility="none", risk="high", confidence="high", date="2026-04-14T15:30:00+09:00", source="2026年2月期 決算短信〔日本基準〕(連結); FY2026説明資料（既存snapshot）", evidence="ストック収益+6%とフロー収益+14%、心築資産・レジデンス売却を確認。対象1Qの資産売却・引渡し予定額は非開示で、売却時期を特定できない。", missing="対象1Qの売却契約、物件名、引渡し予定日、売却益・フロー収益の四半期計画"),
    "244A": dict(model="project_progress", recurring="medium", lump="medium", visibility="none", risk="medium", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はDX・クラウド・ITコンサルティング。案件進捗・稼働が売上経路となり得るが、対象Qの受注・検収時期は確認できない。", missing="準委任・請負比率、受注残、対象Qの案件進捗・検収予定"),
    "2484": dict(model="recurring", recurring="high", lump="low", visibility="not_applicable", risk="low", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明はフードデリバリー。注文量・販促・配達経済性が売上・利益の経路であり、特定案件の検収・引渡し型とは確認されない。", missing="対象Qの注文数・販促・手数料KPI、事業別収益認識方針"),
    "280A": dict(model="unknown", recurring="unknown", lump="unknown", visibility="unknown", risk="unknown", confidence="low", date=None, source="Official IR library / investor information（既存snapshot）", evidence="保存済み事業説明は半導体製造装置・部材の循環型ソリューションにとどまり、装置・部材の納品／検収時点で認識するか、継続取引比率は確認できない。", missing="収益認識方針、受注残、対象Qの納品・検収予定、継続取引比率"),
}


def main() -> None:
    with V3.open(encoding="utf-8-sig", newline="") as f:
        v3 = list(csv.DictReader(f))
    rows = []
    for r in v3:
        code = r["code"]
        a = ASSESSMENTS[code]
        rows.append({
            "code": code, "company_name": r["company_name"],
            "statistical_sales_pattern": r["sales_pattern_v3"],
            "statistical_operating_profit_pattern": r["operating_profit_pattern_v3"],
            "statistical_gate": r["quarterly_predictability_gate_v3"],
            "revenue_recognition_model": a["model"],
            "recurring_revenue_importance": a["recurring"],
            "lump_sum_revenue_importance": a["lump"],
            "target_quarter_visibility": a["visibility"],
            "timing_risk": a["risk"], "evidence_summary": a["evidence"],
            "evidence_source": a["source"], "evidence_published_at": a["date"],
            "confidence": a["confidence"], "missing_information": a["missing"],
        })
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    model_counts = Counter(r["revenue_recognition_model"] for r in rows)
    risk_counts = Counter(r["timing_risk"] for r in rows)
    vis_counts = Counter(r["target_quarter_visibility"] for r in rows)
    md = ["# 売上・利益の計上タイミングリスク v1", "", f"- 基準日時: {CUTOFF}", "- v3の統計値は参照のみ。元観測・単独Q値は変更していない。", "- 根拠は既存as-of snapshotと既存保存資料のみ。Web検索・新規PDF取得は行っていない。", "", "## 件数", f"- 収益認識モデル: {dict(model_counts)}", f"- timing risk: {dict(risk_counts)}", f"- 対象Q可視性: {dict(vis_counts)}", "", "## 2337", "- mixed / recurring high / lump-sum high / visibility none / timing risk high。ストック収益とフロー収益の双方を確認したが、対象1Qの資産売却・引渡し予定は保存資料で特定できなかった。", "", "## 1418との違い", "- 1418は大型案件の完工要因と保守サービスの双方が確認されるmixed。受注残・対象1Q完工予定は未確認だが、資産売却・引渡しの一括計上が重要と確認された2337よりはmediumとした。", "", "## M&A成功報酬型", "- success_fee_closingを確認できた銘柄は0社。M&A仲介・成功報酬型を示す保存資料は対象15社では確認できなかった。", "", "## 銘柄別", *[f"- {r['code']} {r['company_name']}: {r['revenue_recognition_model']} / visibility={r['target_quarter_visibility']} / risk={r['timing_risk']}（{r['evidence_summary']}）" for r in rows]]
    MD_OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print({"rows": len(rows), "models": dict(model_counts), "risks": dict(risk_counts), "visibility": dict(vis_counts)})


if __name__ == "__main__":
    main()
