# src/edinet_orders/saver.py
"""
edinet_order_data テーブルへの UPSERT を担当するモジュール。

UPSERT キー: ON CONFLICT ON CONSTRAINT edinet_order_data_uniq
  → UNIQUE (ticker, period, fiscal_year, segment_name_key, source_type)
  → segment_name_key は generated column のため INSERT に含めない

supabase-py v2 の .upsert() は generated column を含む制約名を
on_conflict= で指定できないため、requests を使って直接 PostgREST に
Prefer: resolution=merge-duplicates を送る。

service_role_key を使用して RLS をバイパスする。
"""
from __future__ import annotations

import os
from typing import Any

import requests

# INSERT 対象カラム（segment_name_key は除外）
_INSERT_COLS = [
    "ticker", "company_name", "doc_id", "period", "fiscal_year",
    "segment_name", "source_type", "source_tag", "confidence",
    "null_reason", "source_unit",
    "raw_orders_received", "raw_order_backlog", "raw_construction_carryover",
    "raw_completed_construction", "raw_rpo",
    "orders_received", "order_backlog", "construction_carryover",
    "completed_construction", "rpo", "snippet",
]


def _get_creds() -> tuple[str, str]:
    """SUPABASE_URL と SUPABASE_SERVICE_ROLE_KEY を返す"""
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が環境変数に設定されていません"
        )
    return url.rstrip("/"), key


# supabase-py Client の型ヒントのみ（import は任意）
try:
    from supabase import Client
except ImportError:
    Client = None  # type: ignore


def _get_client():
    """Supabase service_role クライアントを生成する（SELECT確認用）"""
    from supabase import create_client
    url, key = _get_creds()
    return create_client(url, key)


def save_to_db(
    db_rows: list[dict[str, Any]],
    dry_run: bool = False,
    client=None,
) -> dict[str, Any]:
    """
    edinet_order_data テーブルへ UPSERT する。

    generated column (segment_name_key) を含む制約のため、
    supabase-py の .upsert() は使わず requests で直接 PostgREST API を呼ぶ。
    Prefer: resolution=merge-duplicates により UPSERT を実現する。

    Parameters
    ----------
    db_rows : list of dict
        transformer.transform_to_db_row() の出力リスト
    dry_run : bool
        True の場合は DB に書き込まず、変換結果のみ返す
    client :
        未使用（API 互換のため残す）

    Returns
    -------
    dict
        {
          "upserted": int,    # 総 UPSERT 件数
          "skipped": int,     # period=None でスキップした件数
          "errors": list,
          "dry_run": bool,
        }
    """
    stats: dict[str, Any] = {
        "upserted": 0,
        "skipped": 0,
        "errors": [],
        "quality_rejects": [],
        "dry_run": dry_run,
    }

    # period判定と保存前ガード判定
    valid_rows = []
    for row in db_rows:
        # DB重複などは呼び出し側や既存ロジックで対応、ここではRow-levelガードのみチェック
        save_candidate = row.get("save_candidate")
        classification = row.get("classification", "OTHER_REVIEW")
        
        if save_candidate is True:
            valid_rows.append(row)
        else:
            stats["skipped"] += 1
            stats["quality_rejects"].append({
                "ticker": row.get("ticker"),
                "classification": classification,
            })

    if dry_run:
        stats["rows"] = valid_rows
        print(f"[DRY RUN] {len(valid_rows)} rows ready (skipped={stats['skipped']})")
        return stats

    if not valid_rows:
        print("No valid rows to insert.")
        return stats

    # INSERT 対象カラムのみ抽出（_dryrun_* などの補助フィールドを除去）
    clean_rows = []
    for row in valid_rows:
        clean = {k: row[k] for k in _INSERT_COLS if k in row}
        clean_rows.append(clean)

    # PostgREST 直接 UPSERT
    # Prefer: resolution=merge-duplicates で ON CONFLICT DO UPDATE
    # ただし conflict 対象カラムを PostgREST が自動解決する
    # (generated column を含む制約でも動作する)
    base_url, key = _get_creds()
    endpoint = f"{base_url}/rest/v1/edinet_order_data"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # resolution=merge-duplicates: ON CONFLICT DO UPDATE (UPSERT)
        # return=minimal: 応答ボディを省略してパフォーマンス向上
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    # Supabase の generated column 制約では merge-duplicates が機能しないため、
    # 存在確認 (GET) → 存在すれば PATCH (UPDATE)、なければ POST (INSERT) とする。
    update_cols = [
        "orders_received", "raw_orders_received",
        "order_backlog", "raw_order_backlog",
        "construction_carryover", "raw_construction_carryover",
        "completed_construction", "raw_completed_construction",
        "rpo", "raw_rpo",
        "source_unit", "confidence", "null_reason", "snippet",
        "doc_id", "source_tag", "company_name",
    ]

    post_headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    patch_headers = {**post_headers}

    upsert_ok = 0
    for row in clean_rows:
        ticker = row.get("ticker", "")
        period = row.get("period", "")
        fiscal_year = row.get("fiscal_year", "")
        source_type = row.get("source_type", "edinet_yuho")
        segment_name = row.get("segment_name")  # None = 連結全体

        try:
            # 存在確認: segment_name が NULL の場合 is.null を使う
            params = {
                "ticker": f"eq.{ticker}",
                "period": f"eq.{period}",
                "fiscal_year": f"eq.{fiscal_year}",
                "source_type": f"eq.{source_type}",
                "select": "id",
                "limit": "1",
            }
            if segment_name is None:
                params["segment_name"] = "is.null"
            else:
                params["segment_name"] = f"eq.{segment_name}"

            chk = requests.get(endpoint, params=params, headers=post_headers, timeout=15)
            exists = chk.status_code == 200 and chk.json()

            if exists:
                # PATCH (UPDATE)
                patch_body = {k: row[k] for k in update_cols if k in row}
                resp = requests.patch(endpoint, json=patch_body, params={
                    k: v for k, v in params.items() if k != "select" and k != "limit"
                }, headers=patch_headers, timeout=30)
            else:
                # POST (INSERT)
                resp = requests.post(endpoint, json=row, headers=post_headers, timeout=30)

            if resp.status_code in (200, 201, 204):
                upsert_ok += 1
                action = "UPDATE" if exists else "INSERT"
                print(f"  [{action}] {ticker} {period} OK")
            else:
                err = {
                    "ticker": ticker,
                    "status": resp.status_code,
                    "body": resp.text[:300],
                }
                stats["errors"].append(err)
                print(f"  [ERROR] {ticker} HTTP {resp.status_code}: {resp.text[:150]}")

        except Exception as e:
            stats["errors"].append({"ticker": ticker, "reason": str(e)})
            print(f"  [ERROR] {ticker} request failed: {e}")

    stats["upserted"] = upsert_ok
    print(
        f"[UPSERT DONE] {upsert_ok}/{len(clean_rows)} rows saved to edinet_order_data"
        f" (errors={len(stats['errors'])}, skipped={stats['skipped']})"
    )

    return stats
