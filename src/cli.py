# ============================================================
# cli.py — 手動修正CLI（Phase2 Step2）
# ============================================================
"""
3つのサブコマンドで decision_db を手動修正する。
全変更は audit_log に記録される。

Usage:
    python -m src.cli quarter --code 7203 --fy 2026-03-31 --q 1Q --sales 1000
    python -m src.cli segment --code 7203 --fy 2026-03-31 --q 1Q --seg-name 自動車 --sales 800
    python -m src.cli memo    --code 7203 --fy 2026-03-31 --q 1Q --text "好調"
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid

from src.migration.migration_db import MigrationDB


# ------------------------------------------------------------------
# バリデーション
# ------------------------------------------------------------------
def _validate_code(code: str) -> str:
    if not re.match(r"^\d{4,5}$", code):
        raise argparse.ArgumentTypeError(
            f"企業コードは4〜5桁の数字: '{code}'"
        )
    return code


def _validate_fy(fy: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", fy):
        raise argparse.ArgumentTypeError(
            f"年度はYYYY-MM-DD形式: '{fy}'"
        )
    return fy


def _validate_quarter(q: str) -> str:
    q = q.upper()
    if q not in ("1Q", "2Q", "3Q", "4Q"):
        raise argparse.ArgumentTypeError(
            f"四半期は 1Q/2Q/3Q/4Q: '{q}'"
        )
    return q


# ------------------------------------------------------------------
# コマンド実装
# ------------------------------------------------------------------
def _cmd_quarter(args: argparse.Namespace, db: MigrationDB) -> None:
    """四半期数値の更新"""
    run_id = f"manual-{uuid.uuid4().hex[:8]}"
    actor = args.actor
    kwargs = {}
    if args.sales is not None:
        kwargs["sales"] = args.sales
    if args.gross_profit is not None:
        kwargs["gross_profit"] = args.gross_profit
    if args.gross_margin is not None:
        kwargs["gross_margin"] = args.gross_margin
    if args.sga is not None:
        kwargs["sga"] = args.sga
    if args.operating_profit is not None:
        kwargs["operating_profit"] = args.operating_profit

    if not kwargs:
        print("エラー: 更新する数値フィールドを1つ以上指定してください")
        sys.exit(1)

    result = db.upsert_quarterly_result(
        args.code, args.fy, args.q,
        **kwargs,
        actor=actor, source="manual", run_id=run_id,
    )
    db.commit()

    print(f"[{result}] {args.code} {args.fy} {args.q}")
    if result == "updated":
        logs = db.get_audit_log(run_id=run_id)
        for log in logs:
            print(f"  {log['field_name']}: {log['old_value']} → {log['new_value']}")
    elif result == "inserted":
        print(f"  新規レコード作成: {kwargs}")
    else:
        print("  変更なし")


def _cmd_segment(args: argparse.Namespace, db: MigrationDB) -> None:
    """セグメント数値の更新"""
    run_id = f"manual-{uuid.uuid4().hex[:8]}"
    actor = args.actor

    result = db.upsert_segment(
        args.code, args.fy, args.q,
        segment_name=args.seg_name,
        segment_order=args.seg_order,
        segment_sales=args.sales,
        segment_profit=args.profit,
        actor=actor, source="manual", run_id=run_id,
    )
    db.commit()

    print(f"[{result}] {args.code} {args.fy} {args.q} seg={args.seg_name}")
    if result == "updated":
        logs = db.get_audit_log(run_id=run_id)
        for log in logs:
            print(f"  {log['field_name']}: {log['old_value']} → {log['new_value']}")
    elif result == "inserted":
        print(f"  新規セグメント作成")
    else:
        print("  変更なし")


def _cmd_memo(args: argparse.Namespace, db: MigrationDB) -> None:
    """メモの追記（履歴型）"""
    run_id = f"manual-{uuid.uuid4().hex[:8]}"
    actor = args.actor

    # テキストの改行を保持（\\n → \n に変換）
    text = args.text.replace("\\n", "\n")

    result = db.insert_quarterly_note(
        args.code, args.fy, args.q, text,
        actor=actor, source="manual", run_id=run_id,
    )
    db.commit()

    print(f"[{result}] {args.code} {args.fy} {args.q}")
    if result == "inserted":
        print(f"  メモ追記: {text[:50]}{'...' if len(text) > 50 else ''}")
    else:
        print("  同一メモが既に存在（スキップ）")


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision-cli",
        description="決算DB 手動修正CLI",
    )
    parser.add_argument(
        "--db", default="decision_db.db",
        help="DBファイルパス（デフォルト: decision_db.db）",
    )
    parser.add_argument(
        "--actor",
        default=os.environ.get("DECISION_DB_ACTOR", "manual"),
        help="操作者名（デフォルト: DECISION_DB_ACTOR環境変数 or 'manual'）",
    )

    subs = parser.add_subparsers(dest="command", required=True)

    # quarter コマンド
    q_parser = subs.add_parser("quarter", help="四半期数値の更新")
    q_parser.add_argument("--code", required=True, type=_validate_code)
    q_parser.add_argument("--fy", required=True, type=_validate_fy)
    q_parser.add_argument("--q", required=True, type=_validate_quarter)
    q_parser.add_argument("--sales", type=float)
    q_parser.add_argument("--gross-profit", type=float, dest="gross_profit")
    q_parser.add_argument("--gross-margin", type=float, dest="gross_margin")
    q_parser.add_argument("--sga", type=float)
    q_parser.add_argument("--operating-profit", type=float, dest="operating_profit")
    q_parser.add_argument("--op", type=float, dest="operating_profit",
                          help="--operating-profit のエイリアス")

    # segment コマンド
    s_parser = subs.add_parser("segment", help="セグメント数値の更新")
    s_parser.add_argument("--code", required=True, type=_validate_code)
    s_parser.add_argument("--fy", required=True, type=_validate_fy)
    s_parser.add_argument("--q", required=True, type=_validate_quarter)
    s_parser.add_argument("--seg-name", required=True, help="セグメント名")
    s_parser.add_argument("--seg-order", type=int, default=0, help="表示順")
    s_parser.add_argument("--sales", type=float)
    s_parser.add_argument("--profit", type=float)

    # memo コマンド
    m_parser = subs.add_parser("memo", help="メモの追記（履歴型）")
    m_parser.add_argument("--code", required=True, type=_validate_code)
    m_parser.add_argument("--fy", required=True, type=_validate_fy)
    m_parser.add_argument("--q", required=True, type=_validate_quarter)
    m_parser.add_argument("--text", required=True, help="メモ内容（\\nで改行）")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    db = MigrationDB(args.db)
    try:
        if args.command == "quarter":
            _cmd_quarter(args, db)
        elif args.command == "segment":
            _cmd_segment(args, db)
        elif args.command == "memo":
            _cmd_memo(args, db)
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
