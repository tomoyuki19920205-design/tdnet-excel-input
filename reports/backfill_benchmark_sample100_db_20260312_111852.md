# Backfill Benchmark Report: sample100_db

**Run ID:** 0f24a73d6ce8
**Date:** 2026-03-12T11:18:52.331445+09:00
**Phase 2:** Yes
**Workers:** XBRL=6 PDF=3
**Date Range:** 2025-03-12~2026-03-12

## Metrics

| Metric | Value |
|:---|---:|
| total_filings | 100 |
| filing_completed | 100 |
| filing_ok | 97 |
| filing_ok_xbrl | 0 |
| filing_ok_pdf | 97 |
| filing_quarantined | 3 |
| filing_failed | 0 |
| filing_needs_pdf | 100 |
| xbrl_success_rate | 0.0% |
| pdf_fallback_rate | 100.0% |
| quarantine_rate | 3.0% |
| failed_rate | 0.0% |
| via_xbrl | 0 |
| via_pdf | 97 |
| xbrl_stage_events | 100 |
| xbrl_stage_ok | 0 |
| xbrl_stage_needs_pdf | 100 |
| xbrl_stage_failed | 0 |
| xbrl_stage_quarantined | 0 |
| pdf_stage_events | 100 |
| pdf_stage_ok | 97 |
| pdf_stage_failed | 0 |
| pdf_stage_quarantined | 3 |
| upserted | 97 |
| retried | 0 |
| timeouts | 0 |
| cache_hit_pdf | 0 |
| cache_hit_xbrl | 0 |
| total_segment_rows | 1390 |
| avg_segments_per_filing | 14.3 |
| elapsed_sec | 289.8 |
| avg_sec_per_filing | 2.9 |
| avg_xbrl_sec | 1.641 |
| avg_pdf_sec | 2.832 |
| xbrl_stage_sec | 51.2 |
| pdf_stage_sec | 104.4 |
| avg_batch_size | 126.4 |
| upsert_inserted | 1290 |
| upsert_updated | 100 |
| upsert_failed_batches | 0 |
| batch_count | 11 |
| current_extraction_mode | pdf_only_effective |

## Duration Percentiles

| Stat | ms |
|:---|---:|
| p50_ms | 2922 |
| p90_ms | 4030 |
| max_ms | 5656 |
| min_ms | 858 |
| avg_ms | 2965 |
| count | 200 |

## 3-Year Full Backfill Estimate

| Parameter | Value |
|:---|---:|
| estimated_total_filings | 30000 |
| sample_filings | 100 |
| avg_xbrl_sec | 1.641 |
| avg_pdf_sec | 2.832 |
| xbrl_success_rate | 0.0% |
| pdf_fallback_rate | 100.0% |
| quarantine_rate | 3.0% |
| xbrl_workers | 6 |
| pdf_workers | 3 |
| retry_factor | 1.0 |
| xbrl_path_sec | 7958.8 |
| pdf_path_sec | 28320.0 |
| db_overhead_sec | 300.0 |
| base_case_sec | 36578.8 |
| base_case_hours | 10.16 |
| optimistic_sec | 29263.1 |
| optimistic_hours | 8.13 |
| pessimistic_sec | 47552.5 |
| pessimistic_hours | 13.21 |
| estimation_method | phase2_dual_path |
| note | Sample-based extrapolation; actual times may vary. |

## Observations

- High PDF fallback rate (100.0%) — consider improving XBRL extraction

---
*Note: Estimates are sample-based extrapolations. Actual times may vary.*
