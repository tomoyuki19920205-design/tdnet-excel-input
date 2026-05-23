#!/usr/bin/env python3
r"""
cleanup_bad_segment_names.py
----------------------------
canonical_segments から、株主資本等変動計算書由来のゴミセグメントを一括削除する。

対象 source : backfill_v4_pdf, excel_legacy
削除対象     : segment_name に特定キーワードを含む行

Usage:
  # dry-run（一覧表示のみ、削除しない）
  python tools/cleanup_bad_segment_names.py --dry-run

  # 特定 ticker のみ dry-run
  python tools/cleanup_bad_segment_names.py --dry-run --ticker 6703

  # 実際に削除
  python tools/cleanup_bad_segment_names.py --apply

  # source を絞る
  python tools/cleanup_bad_segment_names.py --dry-run --source backfill_v4_pdf

  # 件数上限
  python tools/cleanup_bad_segment_names.py --dry-run --limit 100
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cleanup_bad_seg")

# ============================================================
# 削除対象の設定
# ============================================================

# 削除対象 source
_TARGET_SOURCES: tuple[str, ...] = ("backfill_v4_pdf", "excel_legacy")

# 削除対象のキーワード（segment_name にこれらを含む場合に削除対象）
# 株主資本等変動計算書の行名が誤ってセグメント名として登録されたもの
_BAD_KEYWORDS: tuple[str, ...] = (
    "当期首残高",
    "当期末残高",
    "当期変動額",
    "当期変動額合計",
    "剰余金の配当",
    "自己株式",
    "株主資本",
    "資本剰余金",
    "利益剰余金",
    "新株予約権",
    "非支配株主持分",
    "その他の包括利益累計額",
    "親会社株主に帰属する当期純利益",
)

# 削除禁止 source（安全確認用）
_PROTECTED_SOURCES: frozenset[str] = frozenset(("edinet_xbrl", "xbrl", "backfill_xbrl"))

# ページングサイズ
_PAGE_SIZE = 500


# ============================================================
# 環境変数読み込み・接続設定
# ============================================================
def _load_env() -> None:
    for fname in (".env.local", ".env"):
        env_path = _PROJECT_ROOT / fname
        if not env_path.exists():
            continue
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break


def _get_rest_config() -> tuple[str, dict]:
    _load_env()
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    )
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が未設定です")
        sys.exit(1)
    rest_url = f"{supabase_url}/rest/v1"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    return rest_url, headers


# ============================================================
# Supabase クエリ
# ============================================================

def _is_ok(status_code: int) -> bool:
    """PostgREST の正常レスポンスコード (200, 204, 206)。"""
    return status_code in (200, 204, 206)


def _build_or_clause(keywords: tuple[str, ...]) -> str:
    """PostgREST の or= フィルタ文字列を生成。"""
    parts = ",".join(f"segment_name.ilike.*{kw}*" for kw in keywords)
    return f"({parts})"


def _fetch_bad_rows(
    rest_url: str,
    headers: dict,
    *,
    sources: tuple[str, ...],
    ticker: str | None,
    limit: int | None,
) -> list[dict]:
    """削除対象行を全件取得する（ページネーション付き）。"""
    collected: list[dict] = []
    offset = 0

    source_in = ",".join(sources)
    or_clause  = _build_or_clause(_BAD_KEYWORDS)

    while True:
        page_limit = _PAGE_SIZE if limit is None else min(_PAGE_SIZE, limit - len(collected))
        if page_limit <= 0:
            break

        params: dict[str, str] = {
            "select": (
                "id,ticker,period,quarter,source,segment_name,"
                "source_priority,sales_ytd,profit_ytd"
            ),
            "source": f"in.({source_in})",
            "or":     or_clause,
            "order":  "ticker.asc,period.asc,quarter.asc,source.asc,segment_name.asc",
            "limit":  str(page_limit),
            "offset": str(offset),
        }
        if ticker:
            params["ticker"] = f"eq.{ticker}"

        resp = requests.get(
            f"{rest_url}/canonical_segments",
            headers={**headers, "Prefer": "count=exact"},
            params=params,
            timeout=30,
        )
        if not _is_ok(resp.status_code):
            logger.error("Fetch error %s: %s", resp.status_code, resp.text[:400])
            break

        rows = resp.json()
        if not isinstance(rows, list):
            logger.error("Unexpected response type: %r", type(rows))
            break

        # Content-Range から総件数をログ
        if offset == 0:
            cr = resp.headers.get("Content-Range", "")
            logger.info("  総件数 (Content-Range): %s", cr)

        collected.extend(rows)
        offset += len(rows)

        if len(rows) < page_limit:
            break
        if limit is not None and len(collected) >= limit:
            break

    return collected


def _delete_by_ids(
    rest_url: str,
    headers: dict,
    ids: list,
    *,
    batch_size: int = 200,
) -> int:
    """id リストを指定して canonical_segments から削除。バッチ分割あり。"""
    deleted = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        id_list = ",".join(str(x) for x in batch)
        resp = requests.delete(
            f"{rest_url}/canonical_segments",
            headers={**headers, "Prefer": "return=minimal"},
            params={"id": f"in.({id_list})"},
            timeout=30,
        )
        if not _is_ok(resp.status_code):
            logger.error(
                "Delete error batch[%d:%d] status=%s: %s",
                i, i + len(batch), resp.status_code, resp.text[:300],
            )
        else:
            deleted += len(batch)
            logger.info(
                "  Deleted batch[%d:%d] (%d rows, cumulative=%d)",
                i, i + len(batch), len(batch), deleted,
            )
    return deleted


# ============================================================
# レポート
# ============================================================
def _print_report(rows: list[dict], *, max_print: int = 80) -> None:
    SEP = "=" * 80
    print(f"\n{SEP}")
    print(f"  削除対象: {len(rows)} 件")
    print(SEP)
    if not rows:
        return

    # source 別集計
    by_source: dict[str, int] = {}
    by_ticker: dict[str, int] = {}
    by_keyword: dict[str, int] = {}
    for r in rows:
        src = r.get("source", "?")
        tk  = r.get("ticker", "?")
        nm  = r.get("segment_name", "")
        by_source[src] = by_source.get(src, 0) + 1
        by_ticker[tk]  = by_ticker.get(tk, 0) + 1
        for kw in _BAD_KEYWORDS:
            if kw in nm:
                by_keyword[kw] = by_keyword.get(kw, 0) + 1
                break

    print("\n[source 別件数]")
    for src, cnt in sorted(by_source.items()):
        print(f"  {src:30s}: {cnt:6d} 件")

    print(f"\n[キーワード別件数]")
    for kw, cnt in sorted(by_keyword.items(), key=lambda x: -x[1]):
        print(f"  {kw:25s}: {cnt:6d} 件")

    top_n = 20
    print(f"\n[ticker 別件数 (上位 {top_n})]")
    for tk, cnt in sorted(by_ticker.items(), key=lambda x: -x[1])[:top_n]:
        print(f"  {tk:10s}: {cnt:6d} 件")

    n_print = min(len(rows), max_print)
    print(f"\n[代表例 (先頭 {n_print}/{len(rows)} 件)]")
    print(
        f"  {'ticker':8s} {'period':12s} {'quarter':6s} {'source':25s} "
        f"{'priority':8s} segment_name"
    )
    print("  " + "-" * 105)
    for r in rows[:n_print]:
        print(
            f"  {r.get('ticker','?'):8s} "
            f"{r.get('period','?'):12s} "
            f"{r.get('quarter','?'):6s} "
            f"{r.get('source','?'):25s} "
            f"{str(r.get('source_priority','?')):8s} "
            f"{r.get('segment_name','?')}"
        )
    if len(rows) > max_print:
        print(f"  ... ({len(rows) - max_print} 件省略)")


# ============================================================
# 後監査
# ============================================================
def _post_audit(rest_url: str, headers: dict, *, sources: tuple[str, ...]) -> None:
    print(f"\n{'='*80}")
    print("  後監査: 削除後の状態確認")
    print(f"{'='*80}")

    # 残存確認
    remaining = _fetch_bad_rows(
        rest_url, headers, sources=sources, ticker=None, limit=None
    )
    if not remaining:
        print("  ✅ 残存なし（削除対象: 0 件）")
    else:
        print(f"  ⚠️  残存あり: {len(remaining)} 件")
        for r in remaining[:10]:
            print(
                f"    {r.get('ticker')} / {r.get('period')} / {r.get('quarter')} "
                f"source={r.get('source')} name={r.get('segment_name')}"
            )

    # 6703 / 2026-03-31 / FY の確認
    print("\n[6703 / 2026-03-31 / FY の canonical_segments 状態]")
    resp = requests.get(
        f"{rest_url}/canonical_segments",
        headers=headers,
        params={
            "select": "ticker,period,quarter,source,segment_name,source_priority",
            "ticker":  "eq.6703",
            "period":  "eq.2026-03-31",
            "quarter": "eq.FY",
            "order":   "source_priority.asc,segment_name.asc",
            "limit":   "30",
        },
        timeout=30,
    )
    if _is_ok(resp.status_code):
        rows6703 = resp.json()
        if rows6703:
            print(f"  {'source':25s} {'priority':8s} segment_name")
            print("  " + "-" * 80)
            for r in rows6703:
                bad_flag = ""
                if any(kw in (r.get("segment_name") or "") for kw in _BAD_KEYWORDS):
                    bad_flag = "  ⚠️ SHOULD_BE_DELETED"
                    if r.get("source") in _PROTECTED_SOURCES:
                        bad_flag = "  [protected — skip]"
                print(
                    f"  {r.get('source','?'):25s} "
                    f"{str(r.get('source_priority','?')):8s} "
                    f"{r.get('segment_name','?')}{bad_flag}"
                )
        else:
            print("  (データなし)")
    else:
        print(f"  クエリ失敗: {resp.status_code}")

    # 保護 source が残っていることを確認
    print("\n[保護 source の残存確認 (削除されていないこと)]")
    for src in sorted(_PROTECTED_SOURCES):
        resp2 = requests.get(
            f"{rest_url}/canonical_segments",
            headers=headers,
            params={"select": "id", "source": f"eq.{src}", "limit": "1"},
            timeout=20,
        )
        if _is_ok(resp2.status_code):
            rows2 = resp2.json()
            mark = "✅" if rows2 else "ℹ️"
            print(f"  {mark} {src}: {'1件以上存在' if rows2 else '(0件 — 元々登録なし)'}")
        else:
            print(f"  ? {src}: クエリ失敗 {resp2.status_code}")


# ============================================================
# メイン
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="canonical_segments から株主資本等変動計算書由来のゴミセグメントを削除する"
    )
    mode_grp = parser.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--dry-run", action="store_true",
                          help="削除せず一覧表示のみ")
    mode_grp.add_argument("--apply",   action="store_true",
                          help="実際に削除を実行する（確認プロンプトあり）")

    parser.add_argument("--ticker", default=None,
                        help="絞り込む ticker (例: 6703)")
    parser.add_argument(
        "--source", default=None,
        help=f"絞り込む source。複数はカンマ区切り。"
             f"デフォルト: {','.join(_TARGET_SOURCES)}"
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="取得件数上限（dry-run 確認用）")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="DELETE バッチサイズ (default: 200)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="削除確認をスキップ (--apply 時のみ有効)")
    parser.add_argument("--verbose", "-v", action="store_true")

    opts = parser.parse_args()

    if opts.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # source フィルタの検証
    if opts.source:
        sources = tuple(s.strip() for s in opts.source.split(","))
        invalid = [s for s in sources if s not in _TARGET_SOURCES]
        if invalid:
            logger.error(
                "削除禁止の source が指定されました: %s\n"
                "許可される source: %s",
                invalid, list(_TARGET_SOURCES),
            )
            return 1
    else:
        sources = _TARGET_SOURCES

    mode = "DRY-RUN" if opts.dry_run else "APPLY"
    logger.info("=== cleanup_bad_segment_names [%s] ===", mode)
    logger.info("  target sources : %s", list(sources))
    logger.info("  bad keywords   : %d 語", len(_BAD_KEYWORDS))
    if opts.ticker:
        logger.info("  ticker filter  : %s", opts.ticker)
    if opts.limit:
        logger.info("  limit          : %d", opts.limit)

    rest_url, headers = _get_rest_config()

    # ── STEP 1: 削除対象取得 ────────────────────────────────────────────
    logger.info("STEP 1: 削除対象行を取得中…")
    bad_rows = _fetch_bad_rows(
        rest_url, headers,
        sources=sources,
        ticker=opts.ticker,
        limit=opts.limit,
    )
    logger.info("  取得件数: %d 件", len(bad_rows))

    # 安全チェック: protected source が混入していないことを確認
    protected_found = [r for r in bad_rows if r.get("source") in _PROTECTED_SOURCES]
    if protected_found:
        logger.error(
            "!!! 保護 source の行が混入しています (%d 件) !!! 中止します。",
            len(protected_found),
        )
        for r in protected_found[:5]:
            logger.error("  %s", r)
        return 1

    # ── STEP 2: 一覧表示 ────────────────────────────────────────────────
    _print_report(bad_rows)

    if opts.dry_run:
        print("\n[DRY-RUN] 削除は実行されていません。")
        print("  削除を実行するには: python tools/cleanup_bad_segment_names.py --apply")
        return 0

    # ── STEP 3: 削除確認 ────────────────────────────────────────────────
    if not bad_rows:
        print("\n削除対象が0件のため終了します。")
        return 0

    if not opts.yes:
        print(f"\n以下を削除します:")
        print(f"  件数   : {len(bad_rows)} 件")
        print(f"  source : {list(sources)}")
        print(f"  キーワード数: {len(_BAD_KEYWORDS)} 語")
        confirm = input("\n本当に削除しますか？ (yes/no): ").strip().lower()
        if confirm != "yes":
            print("中止しました。")
            return 0

    # ── STEP 4: 削除実行 ────────────────────────────────────────────────
    logger.info("STEP 4: 削除実行中 (%d 件)…", len(bad_rows))
    ids = [r["id"] for r in bad_rows]
    deleted = _delete_by_ids(rest_url, headers, ids, batch_size=opts.batch_size)
    logger.info("削除完了: %d / %d 件", deleted, len(bad_rows))

    if deleted != len(bad_rows):
        logger.warning("削除件数が期待値と異なります (%d != %d)", deleted, len(bad_rows))

    # ── STEP 5: 後監査 ──────────────────────────────────────────────────
    logger.info("STEP 5: 後監査…")
    _post_audit(rest_url, headers, sources=sources)

    return 0


if __name__ == "__main__":
    sys.exit(main())
