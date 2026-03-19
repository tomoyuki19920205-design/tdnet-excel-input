# ============================================================
# migrator.py — ParseResult → DB 書き込みロジック
# ============================================================
from __future__ import annotations

import logging

from .migration_db import MigrationDB
from .parse_models import CompanyBlock, LogEntry, ParseResult

logger = logging.getLogger("migration")


class MigrationSummary:
    """移行サマリ"""

    def __init__(self) -> None:
        self.companies_processed: int = 0
        self.quarters_inserted: int = 0
        self.segments_inserted: int = 0
        self.memos_inserted: int = 0
        self.notes_inserted: int = 0
        self.skips: int = 0
        self.errors: int = 0
        self.warns: int = 0

    def __str__(self) -> str:
        return (
            "=== Migration Summary ===\n"
            f"Companies processed : {self.companies_processed}\n"
            f"Quarters inserted   : {self.quarters_inserted}\n"
            f"Segments inserted   : {self.segments_inserted}\n"
            f"Memos inserted      : {self.memos_inserted}\n"
            f"Notes inserted      : {self.notes_inserted}\n"
            f"SKIPs               : {self.skips}\n"
            f"ERRORs              : {self.errors}\n"
            f"WARNs               : {self.warns}\n"
        )


def run_migration(
    parse_result: ParseResult,
    db: MigrationDB,
    run_id: str,
    *,
    sheet_name: str = "PL",
    dry_run: bool = False,
) -> MigrationSummary:
    """
    パース結果をDBへ書き込む。

    Args:
        parse_result: Excelパース結果
        db: MigrationDBインスタンス
        run_id: 実行ID
        sheet_name: シート名
        dry_run: True の場合DB書き込みなし

    Returns:
        MigrationSummary — 処理サマリ
    """
    summary = MigrationSummary()

    # ログエントリの書き込み
    for log_entry in parse_result.logs:
        if log_entry.log_level == "SKIP":
            summary.skips += 1
        elif log_entry.log_level == "ERROR":
            summary.errors += 1
        elif log_entry.log_level == "WARN":
            summary.warns += 1

        if not dry_run:
            db.insert_log(
                run_id=run_id,
                log_level=log_entry.log_level,
                log_type=log_entry.log_type,
                message=log_entry.message,
                sheet_name=sheet_name,
                row_start=log_entry.row_start,
                row_end=log_entry.row_end,
                company_code=log_entry.company_code,
                fiscal_year=log_entry.fiscal_year,
                quarter=log_entry.quarter,
            )

    # 企業ブロックの書き込み
    for block in parse_result.blocks:
        summary.companies_processed += 1

        # 補助メモ（C〜L列）
        memo_kwargs = {
            "col_c": block.memo_c,
            "col_d": block.memo_d,
            "col_e": block.memo_e,
            "col_f": block.memo_f,
            "col_g": block.memo_g,
            "col_h": block.memo_h,
            "col_i": block.memo_i,
            "col_j": block.memo_j,
            "col_k": block.memo_k,
            "col_l": block.memo_l,
        }
        has_memo = any(v is not None for v in memo_kwargs.values())
        if has_memo:
            summary.memos_inserted += 1
            if not dry_run:
                db.upsert_company_memo(
                    company_code=block.company_code,
                    **memo_kwargs,
                )

        # 四半期レコード
        for rec in block.records:
            summary.quarters_inserted += 1

            if not dry_run:
                # PL数値
                db.upsert_quarterly_result(
                    company_code=rec.company_code,
                    fiscal_year_end=rec.fiscal_year_end,
                    quarter=rec.quarter,
                    sales=rec.sales,
                    gross_profit=rec.gross_profit,
                    gross_margin=rec.gross_margin,
                    sga=rec.sga,
                    operating_profit=rec.operating_profit,
                )

                # Z列メモ（履歴型）
                if rec.note is not None:
                    db.insert_quarterly_note(
                        company_code=rec.company_code,
                        fiscal_year_end=rec.fiscal_year_end,
                        quarter=rec.quarter,
                        note=rec.note,
                    )
                    summary.notes_inserted += 1

                # セグメント
                for seg in rec.segments:
                    db.upsert_segment(
                        company_code=rec.company_code,
                        fiscal_year_end=rec.fiscal_year_end,
                        quarter=rec.quarter,
                        segment_name=seg.segment_name,
                        segment_order=seg.segment_order,
                        segment_sales=seg.segment_sales,
                        segment_profit=seg.segment_profit,
                    )
                    summary.segments_inserted += 1

    if not dry_run:
        db.commit()

    return summary
