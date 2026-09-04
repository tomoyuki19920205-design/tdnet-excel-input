#!/usr/bin/env python3
# run_edinet_orders.py
"""
EDINET受注データ 抽出→DB保存 エントリーポイント

【動的モード（Nightly用）】:
    # 指定日に提出されたXBRL有報から受注を抽出 (dry-run / DB保存なし)
    python run_edinet_orders.py --date 2026-06-24 --dry-run

    # カナリア1社のみ dry-run
    python run_edinet_orders.py --date 2026-06-24 --edinet-code E01632 --dry-run --max-docs 1

    # 本番適用 (--apply明示が必要)
    python run_edinet_orders.py --date 2026-06-24 --apply

    # 今日分 dry-run
    python run_edinet_orders.py --today --dry-run

【静的モード（旧手動バッチ用 / 既存のまま残す）】:
    # 抽出 + DB保存（静的survey_detail.json 32社）
    python run_edinet_orders.py

    # DRY RUN（DB保存なし）
    python run_edinet_orders.py --dry-run

    # 特定企業のみ
    python run_edinet_orders.py --tickers 1812 6141 6834

    # 既存JSONから保存のみ（再抽出しない）
    python run_edinet_orders.py --from-json scratch/orders_extracted_30_v4.json

重要:
    デフォルトは dry-run。--apply を明示しない限りDBには書き込まない。
    動的モードは --date または --today が指定された場合に動作する。
    動的モードでは古い絶対パスの survey_detail.json を使わない。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from lib.runtime_paths import runtime_path

# .env 読み込み
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))

from src.edinet_orders.extractor import extract
from src.edinet_orders.transformer import transform_to_db_row
from src.edinet_orders.saver import save_to_db

SURVEY_JSON = Path(r"C:\Users\takuy\.gemini\antigravity\brain\8ceab1ef-6c13-410f-9a78-5f3b53e47b74\scratch\survey_detail.json")
SCRATCH_DIR = Path(__file__).parent / "scratch"

JST = timezone(timedelta(hours=9))

# ============================================================
# 動的モード専用: EDINET APIから指定日の有報を取得
# ============================================================

def _normalize_seccode(sec_code: str | None) -> str | None:
    """5桁 secCode → 4桁 ticker。"""
    if not sec_code:
        return None
    code = str(sec_code).strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    if len(code) == 4:
        return code
    return code


def _fetch_docs_for_date(target_date: str) -> list[dict]:
    """EDINET documents API で指定日の有価証券報告書一覧を取得。
    副作用なし（APIコールのみ）。DB書き込みなし。
    """
    import requests
    api_key = os.environ.get("EDINET_API_KEY", "")
    if not api_key:
        print("[ERROR] EDINET_API_KEY is not set.")
        return []
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    params = {"date": target_date, "type": 2, "Subscription-Key": api_key}
    print(f"[API] Fetching EDINET documents for {target_date} ...")
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [ERROR] HTTP {resp.status_code}")
            return []
        data = resp.json()
        if data.get("statusCode", 200) != 200:
            print(f"  [ERROR] API status: {data.get('message', '')}")
            return []
    except Exception as e:
        print(f"  [ERROR] request error: {e}")
        return []

    results = data.get("results", [])
    print(f"  [API] total docs in response: {len(results)}")
    return results


def _filter_yuho_xbrl(docs: list[dict]) -> list[dict]:
    """有価証券報告書/四半期報告書 かつ xbrlFlag=1 のみ抽出。"""
    filtered = []
    for r in docs:
        code = r.get("docTypeCode", "")
        # 120 = 有報、130 = 訂正有報、140 = 四半期報告書、150 = 訂正四半期報告書
        if code not in ("120", "130", "140", "150"):
            continue
        if r.get("xbrlFlag", "0") != "1":
            continue
        filtered.append(r)
    return filtered


def _download_xbrl(doc_id: str, cache_dir: str | None = None) -> bool:
    """EdinetClient を使ってXBRL ZIPをダウンロード or キャッシュから読む。
    キャッシュヒット or ダウンロード成功で True を返す。DB書き込みなし。
    """
    try:
        from lib.backfill.edinet_client import EdinetClient
        client = EdinetClient(cache_dir=cache_dir)
        result = client.download_xbrl_zip(doc_id, cache_dir=cache_dir)
        if result.succeeded:
            if result.cache_hit:
                print(f"    [CACHE HIT] {doc_id}")
            else:
                print(f"    [DOWNLOADED] {doc_id} → {result.cache_path}")
            return True
        else:
            reason = result.failure_reason or result.skipped_reason or "unknown"
            print(f"    [DOWNLOAD SKIP/FAIL] {doc_id}: {reason}")
            return False
    except Exception as e:
        print(f"    [DOWNLOAD ERROR] {doc_id}: {e}")
        return False


def _run_dynamic_mode(args: argparse.Namespace) -> None:
    """動的モード本体: 指定日の有報APIから受注を抽出してdry-run/apply。

    --apply なしはDB書き込みゼロ保証。
    """
    from src.edinet_orders.extractor import extract_from_company
    from src.edinet_orders.transformer import transform_to_db_row

    # apply確認 (二重ガード)
    is_apply = getattr(args, "apply", False)
    if is_apply:
        print("[MODE] APPLY — edinet_order_data への書き込みを行います")
        print("[WARN] --apply が指定されています。DB書き込みが発生します。")
    else:
        print("[MODE] DRY-RUN — DB書き込みなし (--apply なし)")

    # 対象日の解決
    if getattr(args, "today", False):
        target_date = datetime.now(JST).strftime("%Y-%m-%d")
    else:
        target_date = args.date
    print(f"[TARGET] date={target_date}")

    # 安全弁
    max_docs = getattr(args, "max_docs", None)
    if max_docs is None:
        max_docs = 3 if not is_apply else 50  # apply時も安全弁
    print(f"[LIMIT] max_docs={max_docs}")

    # カナリア絞り込み
    filter_edinet_code = getattr(args, "edinet_code", None)
    filter_doc_id = getattr(args, "doc_id", None)

    # 1. EDINET documents API 取得
    all_docs = _fetch_docs_for_date(target_date)
    print(f"[FILTER] documents API total: {len(all_docs)}")

    # 2. 有価証券報告書 + XBRLあり フィルタ
    yuho_docs = _filter_yuho_xbrl(all_docs)
    print(f"[FILTER] 有報/訂正有報 xbrlFlag=1: {len(yuho_docs)} 件")

    # 3. カナリア絞り込み
    if filter_edinet_code:
        yuho_docs = [d for d in yuho_docs if d.get("edinetCode", "") == filter_edinet_code]
        print(f"[FILTER] edinetCode={filter_edinet_code}: {len(yuho_docs)} 件")
    if filter_doc_id:
        yuho_docs = [d for d in yuho_docs if d.get("docID", "") == filter_doc_id]
        print(f"[FILTER] docID={filter_doc_id}: {len(yuho_docs)} 件")

    # 4. max_docs 上限適用
    if len(yuho_docs) > max_docs:
        print(f"[LIMIT] {len(yuho_docs)} → {max_docs} 件に制限")
        yuho_docs = yuho_docs[:max_docs]

    if not yuho_docs:
        print("[INFO] 処理対象の有報が0件です。終了します。")
        _write_dryrun_report(args, target_date, [], [])
        return

    # キャッシュディレクトリ
    cache_dir = os.environ.get(
        "EDINET_CACHE_DIR",
        str(runtime_path(Path(__file__).parent / "data" / "edinet_cache"))
    )

    # 5. XBRL取得 → 抽出 → 変換
    extracted_list = []
    db_rows = []
    skip_reasons = []

    for doc in yuho_docs:
        doc_id = doc.get("docID", "")
        edinet_code = doc.get("edinetCode", "")
        filer_name = doc.get("filerName", "")
        sec_code = (doc.get("secCode") or "").strip()
        ticker = _normalize_seccode(sec_code)
        period_end = (doc.get("periodEnd") or "")[:10]
        doc_description = doc.get("docDescription", "")
        submit_dt = doc.get("submitDateTime", "")

        print(f"\n[DOC] {doc_id} | {edinet_code} | {filer_name} | {doc_description}")
        print(f"       ticker={ticker} | period_end={period_end} | submit={submit_dt}")

        if not doc_id:
            skip_reasons.append({"edinet_code": edinet_code, "reason": "no_doc_id"})
            continue

        if not period_end:
            print(f"  [WARN] period_end が不明。periodEnd フィールドがAPI応答に含まれていない可能性。")
            import re
            m = re.search(r"－(\d{4}/\d{2}/\d{2})\)$", doc_description)
            if m:
                period_end = m.group(1).replace("/", "-")
                print(f"  [INFO] タイトルから period_end={period_end} を補完しました。")

        # XBRL取得
        dl_ok = _download_xbrl(doc_id, cache_dir=cache_dir)
        if not dl_ok:
            skip_reasons.append({"doc_id": doc_id, "edinet_code": edinet_code,
                                  "company": filer_name, "reason": "xbrl_download_failed"})
            continue

        # 受注抽出
        target_spec = {
            "ticker": ticker or edinet_code,
            "company": filer_name,
            "doc_id": doc_id,
            "docs": [doc]
        }
        extracted = extract_from_company(target_spec, cache_dir=cache_dir)
        extracted["edinet_code"] = edinet_code
        extracted["submit_datetime"] = submit_dt
        extracted["doc_description"] = doc_description
        extracted["api_period_end"] = period_end  # API由来またはタイトル補完の期末日を保持
        extracted_list.append(extracted)

        has_value = (
            extracted.get("orders_received") is not None
            or extracted.get("order_backlog") is not None
        )
        print(f"  [EXTRACT] orders_received={extracted.get('orders_received')} "
              f"order_backlog={extracted.get('order_backlog')} "
              f"unit={extracted.get('unit')} "
              f"confidence={extracted.get('confidence')}")
        if not has_value:
            print(f"  [EXTRACT] 受注高/受注残高 未検出。notes={extracted.get('notes', '')}")
            if extracted.get("confidence") == "low" and not has_value:
                skip_reasons.append({"doc_id": doc_id, "edinet_code": edinet_code,
                                      "company": filer_name, "reason": "no_order_values"})
                continue

        # transformer: API由来のperiod_endを使い、staticJSONのfiscal_endには依存しない
        fiscal_end = period_end or None
        row = transform_to_db_row(extracted, fiscal_end=fiscal_end)
        row["doc_id"] = doc_id
        row["edinet_code"] = edinet_code
        db_rows.append(row)
        print(f"  [CANDIDATE] ticker={row.get('ticker')} period={row.get('period')} "
              f"orders_received={row.get('orders_received')} "
              f"order_backlog={row.get('order_backlog')} "
              f"source_unit={row.get('source_unit')}")

    # 6. 件数サマリー
    print(f"\n{'='*55}")
    print(f"  [SUMMARY] target_date={target_date}")
    print(f"  [SUMMARY] documents API: {len(all_docs)} 件")
    print(f"  [SUMMARY] 有報+XBRL フィルタ後: {len(yuho_docs)} 件 (上限適用済)")
    print(f"  [SUMMARY] 抽出対象: {len(extracted_list)} 件")
    print(f"  [SUMMARY] edinet_order_data 保存候補: {len(db_rows)} 件")
    print(f"  [SUMMARY] skip/reject: {len(skip_reasons)} 件")
    print(f"{'='*55}")

    # 7. dry-run ガード (最重要)
    if not is_apply:
        print("\n[DRY_RUN] DB save skipped — edinet_order_data への書き込みはありません")
        print("          本番書き込みには --apply を明示してください")
    else:
        # apply 実行
        if db_rows:
            print("\n[APPLY] edinet_order_data へ保存中...")
            from src.edinet_orders.saver import save_to_db as _save
            stats = _save(db_rows, dry_run=False)
            print(f"  upserted={stats.get('upserted', 0)} "
                  f"skipped={stats.get('skipped', 0)} "
                  f"errors={len(stats.get('errors', []))}")
        else:
            print("\n[APPLY] 保存候補が0件のため、DB書き込みをスキップ")

    # 8. レポート出力
    _write_dryrun_report(args, target_date, db_rows, skip_reasons)


def _write_dryrun_report(
    args: argparse.Namespace,
    target_date: str,
    db_rows: list[dict],
    skip_reasons: list[dict],
) -> None:
    """dry-run/apply の結果レポートを scratch に書き出す。"""
    runtime_path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    run_id = ts
    base = f"edinet_order_pipeline_dryrun_{target_date.replace('-', '')}_{run_id}"

    is_apply = getattr(args, "apply", False)
    filter_edinet_code = getattr(args, "edinet_code", None)
    max_docs = getattr(args, "max_docs", None)

    json_path = getattr(args, "output_json", None)
    md_path = getattr(args, "output_md", None)

    if json_path is None:
        json_path = runtime_path(SCRATCH_DIR) / f"{base}.json"
    else:
        json_path = Path(json_path)

    if md_path is None:
        md_path = runtime_path(SCRATCH_DIR) / f"{base}.md"
    else:
        md_path = Path(md_path)

    # JSON
    report_data = {
        "run_id": run_id,
        "target_date": target_date,
        "mode": "APPLY" if is_apply else "DRY-RUN",
        "db_saved": is_apply,
        "filter_edinet_code": filter_edinet_code,
        "max_docs": max_docs,
        "edinet_order_data_candidates": db_rows,
        "skip_reasons": skip_reasons,
    }
    json_path.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Markdown
    md_lines = [
        f"# EDINET受注 パイプライン Dry-run レポート: {target_date}",
        "",
        f"## 実行情報",
        f"- run_id: `{run_id}`",
        f"- target_date: `{target_date}`",
        f"- mode: `{'APPLY' if is_apply else 'DRY-RUN'}`",
        f"- DB保存: `{'あり' if is_apply else 'なし（DRY-RUN）'}`",
        f"- Discord通知: `なし（notify_to_discord=False 固定）`",
        f"- filter_edinet_code: `{filter_edinet_code or 'なし(全件)'}`",
        f"- max_docs: `{max_docs}`",
        "",
        f"## edinet_order_data 抽出行 (DB保存判定対象): {len(db_rows)} 件",
        "",
        "| ticker | edinet_code | company_name | period | orders_received | order_backlog | source_unit | classification | save_candidate |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in db_rows:
        md_lines.append(
            f"| {r.get('ticker')} | {r.get('edinet_code')} | {r.get('company_name')} "
            f"| {r.get('period')} | {r.get('orders_received')} | {r.get('order_backlog')} "
            f"| {r.get('source_unit')} | {r.get('classification', '')} | {r.get('save_candidate', '')} |"
        )

    md_lines += [
        "",
        f"## Skip/Reject: {len(skip_reasons)} 件",
        "",
        "| doc_id | edinet_code | company | reason |",
        "|---|---|---|---|",
    ]
    for s in skip_reasons:
        md_lines.append(
            f"| {s.get('doc_id', '')} | {s.get('edinet_code', '')} "
            f"| {s.get('company', '')} | {s.get('reason', '')} |"
        )

    md_lines += [
        "",
        "## 後段イベント生成見込み",
        "",
        "上記 edinet_order_data のうち、save_candidate が True (PASS_SAVE_CANDIDATE) の行が実際にDBに保存された後、",
        "`tools/generate_edinet_order_events.py --date {}` を実行することで、".format(target_date),
        "以下のキーが一致する場合に tdnet_events のイベント候補が生成されます:",
        "",
        "- edinet_order_data.ticker × API取得有報の secCode → ticker",
        "- edinet_order_data.period = API取得有報の periodEnd",
        "",
        "## 注意事項",
        "- このレポートはdry-runの結果です。DBには書き込まれていません。" if not is_apply else "- APPLYが実行されました。DB書き込みが発生しています。",
        "- 本番適用には `--apply` を明示的に付与して再実行してください。" if not is_apply else "",
    ]

    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\n[REPORT] JSON → {json_path}")
    print(f"[REPORT] MD  → {md_path}")


def _load_survey() -> list[dict]:
    with open(SURVEY_JSON, encoding="utf-8") as f:
        return json.load(f)


def _build_fiscal_end_map(survey_data: list[dict]) -> dict[str, str]:
    return {
        d["ticker"]: d["fiscal_end"]
        for d in survey_data
        if d.get("ticker") and d.get("fiscal_end")
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="EDINET受注データ 抽出→DB保存",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # ── 動的モード専用引数 ──
    parser.add_argument("--date", type=str, default=None,
                        help="指定日(YYYY-MM-DD)の有報を動的取得 [動的モード]")
    parser.add_argument("--today", action="store_true",
                        help="JSTの今日の有報を動的取得 [動的モード]")
    parser.add_argument("--edinet-code", type=str, default=None,
                        help="EDINETコードで絞り込み (例: E01632) [動的モード]")
    parser.add_argument("--doc-id", type=str, default=None,
                        help="特定docIDで絞り込み [動的モード]")
    parser.add_argument("--apply", action="store_true",
                        help="DB保存を実行する (指定なしはdry-run) [動的/静的共通]")
    parser.add_argument("--max-docs", type=int, default=None,
                        help="処理上限件数 (動的モードのdry-run時デフォルト=3) [動的モード]")
    parser.add_argument("--no-notify", action="store_true",
                        help="Discord通知を送らない (現時点ではデフォルトで通知なし)")
    parser.add_argument("--output-json", type=str, default=None,
                        help="レポートJSON保存先 [動的モード]")
    parser.add_argument("--output-md", type=str, default=None,
                        help="レポートMD保存先 [動的モード]")
    # ── 静的モード専用引数 (既存のまま) ──
    parser.add_argument("--dry-run", action="store_true",
                        help="[静的モード] DB保存をスキップ")
    parser.add_argument("--tickers", nargs="+",
                        help="[静的モード] 対象銘柄コードを指定（省略時は全社）")
    parser.add_argument("--from-json", type=Path,
                        help="[静的モード] 既存JSONから保存（再抽出しない）")
    parser.add_argument("--save-json", type=Path,
                        help="[静的モード] 抽出結果JSONの保存先")
    args = parser.parse_args()

    # ── 動的モード判定 ──
    use_dynamic = bool(args.date or args.today)
    if use_dynamic:
        # 動的モード: 古い survey_detail.json に依存しない
        _run_dynamic_mode(args)
        return

    print("=" * 60)
    print("EDINET受注データ保存パイプライン")
    print(f"  mode    : {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"  tickers : {args.tickers or 'ALL'}")
    print("=" * 60)

    survey_data = _load_survey()
    fiscal_end_map = _build_fiscal_end_map(survey_data)
    print(f"survey_detail: {len(survey_data)} entries, fiscal_end mapped: {len(fiscal_end_map)}")

    # ── 1. 抽出 ──
    if args.from_json:
        print(f"\n[SKIP EXTRACT] Loading from: {args.from_json}")
        with open(args.from_json, encoding="utf-8") as f:
            extracted_list = json.load(f)
        # from-json はリスト形式
        if isinstance(extracted_list, list) and extracted_list and "rows" in extracted_list[0]:
            extracted_list = extracted_list[0]["rows"]  # DRY RUN JSON形式への対応
    else:
        # survey_data をフィルタ（tickers指定があれば絞り込み）
        target_survey = survey_data
        if args.tickers:
            target_survey = [d for d in survey_data if d.get("ticker") in args.tickers]
            print(f"\n[FILTER] {len(target_survey)} companies selected")

        print(f"\n[EXTRACT] Start extracting {len([d for d in target_survey if d.get('doc_id')])} companies...")
        extracted_list = extract(target_survey)

    # ── 2. JSON 保存 ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = args.save_json or (runtime_path(SCRATCH_DIR) / f"edinet_orders_{ts}.json")
    runtime_path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(extracted_list, f, ensure_ascii=False, indent=2)
    print(f"\n[JSON] Saved {len(extracted_list)} records → {json_path}")

    # ── 3. DB形式へ変換 ──
    print("\n[TRANSFORM] Converting to DB format...")
    db_rows = []
    for item in extracted_list:
        ticker = item.get("ticker")
        fiscal_end = fiscal_end_map.get(ticker)
        if not fiscal_end:
            print(f"  WARNING: fiscal_end not found for {ticker}")
        row = transform_to_db_row(item, fiscal_end=fiscal_end)
        db_rows.append(row)

    # ── 4. 統計表示 ──
    conf_stats: dict[str, int] = {}
    unit_stats: dict[str, int] = {}
    nr_stats: dict[str, int] = {}
    period_stats: dict[str, int] = {}
    or_cnt = ob_cnt = cc_cnt = comp_cnt = rpo_cnt = 0

    for r in db_rows:
        c = r.get("confidence", "low")
        conf_stats[c] = conf_stats.get(c, 0) + 1
        u = r.get("source_unit", "unknown")
        unit_stats[u] = unit_stats.get(u, 0) + 1
        nr = r.get("null_reason")
        if nr:
            nr_stats[nr] = nr_stats.get(nr, 0) + 1
        p = r.get("period")
        if p:
            period_stats[p] = period_stats.get(p, 0) + 1
        if r.get("orders_received") is not None:
            or_cnt += 1
        if r.get("order_backlog") is not None:
            ob_cnt += 1
        if r.get("construction_carryover") is not None:
            cc_cnt += 1
        if r.get("completed_construction") is not None:
            comp_cnt += 1
        if r.get("rpo") is not None:
            rpo_cnt += 1

    print(f"\n[STATS]")
    print(f"  Total rows      : {len(db_rows)}")
    print(f"  confidence      : {conf_stats}")
    print(f"  source_unit     : {unit_stats}")
    print(f"  null_reason     : {nr_stats}")
    print(f"  period values   : {period_stats}")
    print(f"  orders_received : {or_cnt}")
    print(f"  order_backlog   : {ob_cnt}")
    print(f"  construction_carryover : {cc_cnt}")
    print(f"  completed_construction : {comp_cnt}")
    print(f"  rpo             : {rpo_cnt}")

    # ── 5. BEFORE サンプル5件 ──
    print("\n[BEFORE INSERT - sample 5]") 
    for r in db_rows[:5]:
        print(
            f"  {r['ticker']} {r['company_name']}"
            f" period={r['period']} fiscal_year={r['fiscal_year']}"
            f" su={r['source_unit']} orders_received={r['orders_received']}"
            f" raw_or={r['raw_orders_received']}"
            f" rpo={r['rpo']} conf={r['confidence']}"
        )

    # ── 6. DB 保存 ──
    print(f"\n[SAVE] dry_run={args.dry_run}")
    stats = save_to_db(db_rows, dry_run=args.dry_run)

    # ── 7. AFTER サンプル5件（LIVE時のみ確認） ──
    if not args.dry_run and not stats["errors"]:
        try:
            from src.edinet_orders.saver import _get_client
            sb = _get_client()
            resp = (
                sb.table("edinet_order_data")
                .select(
                    "ticker,company_name,period,fiscal_year,"
                    "orders_received,order_backlog,rpo,"
                    "raw_orders_received,raw_order_backlog,raw_rpo,"
                    "source_unit,segment_name,segment_name_key,"
                    "confidence,null_reason"
                )
                .order("ticker")
                .limit(5)
                .execute()
            )
            print("\n[AFTER INSERT - sample 5 from DB]")
            for row in (resp.data or []):
                print(
                    f"  {row.get('ticker')} {row.get('company_name')}"
                    f" period={row.get('period')} fiscal_year={row.get('fiscal_year')}"
                    f" segment_name_key={row.get('segment_name_key')}"
                    f" su={row.get('source_unit')}"
                    f" orders_received={row.get('orders_received')}"
                    f" raw_or={row.get('raw_orders_received')}"
                    f" rpo={row.get('rpo')} conf={row.get('confidence')}"
                )
        except Exception as e:
            print(f"  [WARNING] Post-insert SELECT failed: {e}")

    print("\n[DONE]")
    print(f"  upserted : {stats.get('upserted', 0)}")
    print(f"  skipped  : {stats.get('skipped', 0)}")
    print(f"  errors   : {len(stats.get('errors', []))}")
    if stats.get("errors"):
        for err in stats["errors"]:
            print(f"    - {err}")


if __name__ == "__main__":
    main()
