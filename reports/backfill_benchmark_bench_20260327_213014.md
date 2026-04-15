# Backfill Benchmark Report: unnamed

**Run ID:** e25910829640
**Date:** 2026-03-27T21:30:14.639454+09:00
**Phase 2:** No
**Workers:** 4
**Date Range:** 2025-03-27~2026-03-27

## Metrics

| Metric | Value |
|:---|---:|
| total_filings | 100 |
| filing_completed | 100 |
| filing_ok | 52 |
| filing_ok_xbrl | 46 |
| filing_ok_pdf | 0 |
| filing_quarantined | 48 |
| filing_failed | 0 |
| filing_needs_pdf | 0 |
| xbrl_success_rate | 46.0% |
| pdf_fallback_rate | 0.0% |
| quarantine_rate | 48.0% |
| failed_rate | 0.0% |
| via_xbrl | 46 |
| via_pdf | 0 |
| xbrl_stage_events | 100 |
| xbrl_stage_ok | 52 |
| xbrl_stage_needs_pdf | 0 |
| xbrl_stage_failed | 0 |
| xbrl_stage_quarantined | 48 |
| pdf_stage_events | 0 |
| pdf_stage_ok | 0 |
| pdf_stage_failed | 0 |
| pdf_stage_quarantined | 0 |
| upserted | 0 |
| retried | 15 |
| timeouts | 0 |
| cache_hit_pdf | 99 |
| cache_hit_xbrl | 81 |
| total_segment_rows | 462 |
| avg_segments_per_filing | 8.9 |
| elapsed_sec | 270.6 |
| avg_sec_per_filing | 2.71 |
| avg_xbrl_sec | 1.649 |
| avg_pdf_sec | 0.0 |
| xbrl_stage_sec | 0.0 |
| pdf_stage_sec | 0.0 |
| avg_batch_size | 0.0 |
| upsert_inserted | 0 |
| upsert_updated | 0 |
| upsert_failed_batches | 0 |
| batch_count | 0 |
| current_extraction_mode | mixed |

## Duration Percentiles

| Stat | ms |
|:---|---:|
| p50_ms | 5468 |
| p90_ms | 10250 |
| max_ms | 56625 |
| min_ms | 1688 |
| avg_ms | 6591 |
| count | 100 |

## Observations

- Cache reuse is effective (180/100 cache hits)
- High quarantine rate (48/100 = 48%) — extraction quality needs review
- Significant retry activity (15 filings retried)

---
*Note: Estimates are sample-based extrapolations. Actual times may vary.*
