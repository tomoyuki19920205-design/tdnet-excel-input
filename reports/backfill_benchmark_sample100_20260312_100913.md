# Backfill Benchmark Report: sample100

**Run ID:** cb2a40454f94
**Date:** 2026-03-12T10:09:13.548422+09:00
**Phase 2:** Yes
**Workers:** XBRL=6 PDF=3
**Date Range:** 2025-03-12~2026-03-12

## Metrics

| Metric | Value |
|:---|---:|
| total_filings | 100 |
| completed | 200 |
| ok | 95 |
| ok_xbrl | 0 |
| ok_pdf | 95 |
| needs_pdf | 100 |
| upserted | 0 |
| quarantined | 5 |
| failed | 0 |
| retried | 0 |
| timeouts | 0 |
| xbrl_success_rate | 0.0% |
| pdf_fallback_rate | 50.0% |
| quarantine_rate | 2.5% |
| failed_rate | 0.0% |
| via_xbrl | 0 |
| via_pdf | 100 |
| cache_hit_pdf | 0 |
| cache_hit_xbrl | 0 |
| total_segment_rows | 1105 |
| avg_segments_per_filing | 11.6 |
| elapsed_sec | 316.8 |
| avg_sec_per_filing | 1.58 |
| avg_xbrl_sec | 0.0 |
| avg_pdf_sec | 3.307 |
| xbrl_stage_sec | 63.5 |
| pdf_stage_sec | 121.8 |
| avg_batch_size | 0.0 |
| upsert_inserted | 0 |
| upsert_updated | 0 |
| upsert_failed_batches | 0 |
| batch_count | 0 |

## Duration Percentiles

| Stat | ms |
|:---|---:|
| p50_ms | 3171 |
| p90_ms | 5077 |
| max_ms | 32983 |
| min_ms | 219 |
| avg_ms | 3562 |
| count | 200 |

## 3-Year Full Backfill Estimate

| Parameter | Value |
|:---|---:|
| estimated_total_filings | 30000 |
| sample_filings | 200 |
| avg_xbrl_sec | 1.58 |
| avg_pdf_sec | 3.307 |
| xbrl_success_rate | 0.0% |
| pdf_fallback_rate | 50.0% |
| quarantine_rate | 2.5% |
| xbrl_workers | 6 |
| pdf_workers | 3 |
| retry_factor | 1.0 |
| xbrl_path_sec | 7702.5 |
| pdf_path_sec | 16537.2 |
| db_overhead_sec | 300.0 |
| base_case_sec | 24539.7 |
| base_case_hours | 6.82 |
| optimistic_sec | 19631.8 |
| optimistic_hours | 5.45 |
| pessimistic_sec | 31901.6 |
| pessimistic_hours | 8.86 |
| estimation_method | phase2_dual_path |
| note | Sample-based extrapolation; actual times may vary. |

## Observations

- High PDF fallback rate (50.0%) — consider improving XBRL extraction

---
*Note: Estimates are sample-based extrapolations. Actual times may vary.*
