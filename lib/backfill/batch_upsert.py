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
) -> BatchUpsertStats:
    """segment_records を batch 単位で explicit transaction upsert する。

    Args:
        records: worker が返した segment_records の集約
        decision_db: MigrationDB インスタンス
        batch_size: 1 transaction あたりの最大レコード数

    Returns:
        BatchUpsertStats
    """
    stats = BatchUpsertStats(total_records=len(records))

    if not records:
        return stats

    chunks = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    stats.total_batches = len(chunks)

    for batch_idx, chunk in enumerate(chunks):
        try:
            decision_db._conn.execute("BEGIN")
            batch_inserted = 0
            batch_updated = 0
            batch_no_change = 0

            for rec in chunk:
                result = decision_db.upsert_segment(
                    company_code=rec["ticker"],
                    fiscal_year_end=rec["period"],
                    quarter=rec["quarter"],
                    segment_name=rec["segment_name"],
                    segment_order=rec.get("segment_order", 0),
                    segment_sales=rec.get("segment_sales"),
                    segment_profit=rec.get("segment_profit"),
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
