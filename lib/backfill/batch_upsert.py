"""lib/backfill/batch_upsert.py — main スレッドでの batch upsert

worker は DB に触らず、ここで explicit transaction 単位で upsert する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("backfill.upsert")


@dataclass
class BatchUpsertStats:
    total_records: int = 0
    total_batches: int = 0
    succeeded_batches: int = 0
    failed_batches: int = 0
    inserted: int = 0
    updated: int = 0
    no_change: int = 0

    @property
    def average_batch_size(self) -> float:
        return self.total_records / max(self.total_batches, 1)


def batch_upsert_segments(
    records: list[dict],
    decision_db,
    *,
    batch_size: int = 200,
    source: str = "backfill",
    actor: str = "backfill",
    xbrl_cleanup_keys: dict | None = None,
) -> BatchUpsertStats:
    """segment_records を batch 単位で explicit transaction upsert する。

    Args:
        records:           worker が返した segment_records の集約
        decision_db:       MigrationDB インスタンス
        batch_size:        1 transaction あたりの最大レコード数
        xbrl_cleanup_keys: PDF V4 採用時に旧 XBRL 行を削除するためのキー辞書。
                           {ticker, fiscal_year_end, quarter, tdnet_doc_id} を含む。
                           None の場合は削除しない。

    Returns:
        BatchUpsertStats
    """
    stats = BatchUpsertStats(total_records=len(records))

    if not records:
        return stats

    # ── _xbrl_cleanup_meta の自動検出 ──
    # worker_v4 が PDF V4 採用時に segment_records の各レコードに付与するメタ。
    # xbrl_cleanup_keys 引数より records 埋め込みメタを優先する。
    _auto_cleanup = xbrl_cleanup_keys
    for _r in records:
        _meta = _r.get("_xbrl_cleanup_meta")
        if _meta:
            _auto_cleanup = _meta
            break  # 全レコードで同一メタなので最初の1件で十分

    chunks = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    stats.total_batches = len(chunks)

    # ── PDF V4 採用時の旧 XBRL 行削除（最初の batch の BEGIN 直後に実行）──
    _cleanup_done = False

    for batch_idx, chunk in enumerate(chunks):
        try:
            decision_db._conn.execute("BEGIN")

            # 旧 XBRL 行 + PDF V4 集計行の削除（初回 batch のみ、同一トランザクション内）
            if _auto_cleanup and not _cleanup_done:
                from lib.backfill.segment_partial_check import (
                    cleanup_old_xbrl_rows,
                    cleanup_aggregate_pdf_rows,
                )
                _deleted_xbrl = cleanup_old_xbrl_rows(
                    decision_db._conn,
                    ticker=_auto_cleanup["ticker"],
                    fiscal_year_end=_auto_cleanup["fiscal_year_end"],
                    quarter=_auto_cleanup["quarter"],
                    tdnet_doc_id=_auto_cleanup["tdnet_doc_id"],
                    reason="pdf_v4_adopted",
                )
                _deleted_agg = cleanup_aggregate_pdf_rows(
                    decision_db._conn,
                    ticker=_auto_cleanup["ticker"],
                    fiscal_year_end=_auto_cleanup["fiscal_year_end"],
                    quarter=_auto_cleanup["quarter"],
                    tdnet_doc_id=_auto_cleanup["tdnet_doc_id"],
                )
                _cleanup_done = True
                logger.info(
                    "[upsert] cleanup: ticker=%s fy=%s quarter=%s tdnet_doc_id=%s "
                    "xbrl_deleted=%d aggregate_deleted=%d",
                    _auto_cleanup["ticker"],
                    _auto_cleanup["fiscal_year_end"],
                    _auto_cleanup["quarter"],
                    _auto_cleanup["tdnet_doc_id"],
                    _deleted_xbrl,
                    _deleted_agg,
                )

            batch_inserted = 0
            batch_updated = 0
            batch_no_change = 0

            for rec in chunk:
                # _xbrl_cleanup_meta は DB スキーマ外のサイドバンドキー → 除去
                rec.pop("_xbrl_cleanup_meta", None)
                result = decision_db.upsert_segment(
                    company_code=rec["ticker"],
                    fiscal_year_end=rec["period"],
                    quarter=rec["quarter"],
                    segment_name=rec["segment_name"],
                    segment_order=rec.get("segment_order", 0),
                    segment_sales=rec.get("segment_sales"),
                    segment_profit=rec.get("segment_profit"),
                    unit_raw=rec.get("unit_raw"),
                    unit_multiplier=rec.get("unit_multiplier"),
                    raw_profit_label=rec.get("raw_profit_label", ""),
                    data_source=rec.get("source", source),
                    actor=actor,
                    source=source,
                    segment_name_norm=rec.get("segment_name_norm"),
                    extractor_route=rec.get("extractor_route"),
                    source_doc_type=rec.get("source_doc_type"),
                    disclosure_date=rec.get("disclosure_date"),
                    tdnet_doc_id=rec.get("tdnet_doc_id"),
                    row_type=rec.get("row_type"),
                )
                if result == "inserted":
                    batch_inserted += 1
                elif result == "updated":
                    batch_updated += 1
                else:
                    batch_no_change += 1

            decision_db._conn.execute("COMMIT")
            stats.succeeded_batches += 1
            stats.inserted += batch_inserted
            stats.updated += batch_updated
            stats.no_change += batch_no_change

            logger.info(
                f"[upsert] batch {batch_idx + 1}/{len(chunks)}: "
                f"inserted={batch_inserted} updated={batch_updated} "
                f"no_change={batch_no_change}"
            )

        except Exception as e:
            try:
                decision_db._conn.execute("ROLLBACK")
            except Exception:
                pass
            stats.failed_batches += 1
            logger.error(
                f"[upsert] batch {batch_idx + 1}/{len(chunks)} FAILED: {e} "
                f"({len(chunk)} records lost)"
            )

    return stats
