# Regional market latest-price fallback plan — 2026-08-17

## Decision

`REGIONAL_MARKET_SOURCE_REQUIRES_LICENSE_REVIEW`

Phase 1 (ordinary-stock classification versus J-Quants price coverage) is implemented and tested. Phase 2/3 found no public terms that affirmatively permit the complete intended workflow—scheduled automated retrieval, persistent database storage, derived valuation calculation, and display/redistribution in Company Viewer—for all three exchanges. In accordance with the stop gate, no regional parser, DB canary, Viewer canary, or 104-ticker backfill was performed.

## Phase 1 producer correction

The old predicate used one flag for two different facts:

```text
is_common_stock = ProdCat 011 AND TSE market allow-list AND not preferred stock
```

It is now separated as:

```text
is_ordinary_stock = ProdCat 011 AND not explicitly named preferred stock
is_jquants_price_eligible = is_ordinary_stock AND J-Quants-supported TSE market
```

The universe snapshot persists both decisions. The legacy `is_common_stock` column/function remains a compatibility alias for the exchange-independent ordinary-stock meaning. Daily J-Quants queries and Supabase synchronization use `is_jquants_price_eligible`, preserving the existing TSE nightly population. Existing databases are migrated conservatively by copying the old flag into both new fields until each dated master snapshot is refreshed.

Expected contracts:

| Security | ordinary | J-Quants price eligible |
|---|---:|---:|
| Regional ordinary stock | true | false |
| TSE ordinary stock | true | true |
| Tokyo PRO ordinary stock | true | false |
| ETF / REIT / preferred stock | false | false |

## Source feasibility

### NAGOYA_SOURCE

Status: `UNSELECTED_PENDING_LICENSE_REVIEW`.

- Public candidate: <https://www.nse.or.jp/market/condition/report/>
- Preferred production candidate: the contracted official market-information service or CENTNET.
- Format: public daily PDF; contracted/paid information products include additional structured delivery.
- Available: date, security code, company, OHLC, volume; turnover is present in market reporting, but the per-security feed definition must be confirmed before mapping.
- Public history: daily reports and monthly statistics; retention is not guaranteed.
- Terms: <https://www.nse.or.jp/notes/> assigns copyright to the exchange and prohibits unauthorized alteration or sale. <https://www.nse.or.jp/market/pay/information/> explicitly requires an information-use contract for real-time market information.
- Automation/storage/Viewer redistribution: not affirmatively permitted by the public terms.
- `robots.txt`: no policy; the URL resolves to the exchange NOT FOUND page.

### SAPPORO_SOURCE

Status: `UNSELECTED_PENDING_LICENSE_REVIEW`.

- Candidate: <https://www.sse.or.jp/market/daily>
- Format: daily and historical PDF.
- Observed URL pattern: `https://www.sse.or.jp/files/dr/YYYY/MM/YYYYMMDD_daily_report.pdf`.
- Available: date, ticker, company, OHLC, volume in shares, turnover in thousands of yen. The official 2026-06-26 report shows these fields for 1832, 2218 and 9027.
- History: official search-visible reports extend to at least 2007; retention is not guaranteed.
- Terms: <https://www.sse.or.jp/privacy> is an accuracy/liability disclaimer. It does not grant scheduled automation, persistent storage, or redistribution. The PDFs state copyright and all rights reserved.
- `robots.txt`: direct access was blocked; no affirmative automation permission was found.

### FUKUOKA_SOURCE

Status: `UNSELECTED_PENDING_LICENSE_REVIEW`.

- Latest candidate: <https://www.fse.or.jp/market/daily.php>
- Historical structured candidate: <https://www.fse.or.jp/statistics/rate.php>
- Format: daily PDF; monthly and annual XLSX.
- Observed daily URL pattern: `https://www.fse.or.jp/files/mar_day/YYYYMMDDdaily.pdf`.
- Available: date, ticker, company, OHLC, volume in shares, turnover in thousands of yen. The official 2026-06-12 report shows these fields for 1771, 2058 and 2919.
- Monthly XLSX contains four prices, volume, turnover and averages, but is published on the third business day and cannot restore latest price promptly.
- Terms: <https://www.fse.or.jp/disclaimer/index.php> assigns copyright to the exchange and prohibits unauthorized alteration or sale; it does not grant automation, storage, or Viewer redistribution.
- `robots.txt`: returns the normal home page rather than a robots policy.

## Canonical latest-price design after license clearance

The existing schema can represent raw regional observations:

| Column | Regional policy |
|---|---|
| ticker/date | normalized four-character ticker and official trading date |
| open/high/low/close | official unadjusted exchange values |
| volume | shares |
| turnover | normalize documented source unit once to JPY |
| adj_factor/adj_close/adj_volume | NULL until an independently verified corporate-action policy exists |
| market_cap | close multiplied by the existing price-date share-count policy |
| source | `regional_nagoya`, `regional_sapporo`, or `regional_fukuoka` |

Priority must be `JQUANTS_TSE > REGIONAL_EXCHANGE`; dual-listed rows must never be overwritten by regional data.

Before a DB canary, `sync_market_data.py` needs a source-aware sync gate. It currently both requires a J-Quants universe match and emits `source="jquants"`, so inserting regional rows locally without correcting that boundary would either exclude them or falsify provenance.

## Latest-only execution gate

After written license confirmation for all intended uses:

1. Implement exchange-specific fetch/parsers behind a shared raw observation contract.
2. Keep PDF/XLSX parsing separate from canonical normalization.
3. Dry-run five official rows per exchange and require exact source equality.
4. Require deterministic output and reject missing/ambiguous close, date, ticker, or source unit.
5. Back up local and Supabase target keys.
6. Apply 3+3+3 DB canaries through the producer, never hard-coded SQL values.
7. Verify Viewer valuation fields and prove 7203/4088 unchanged.
8. Only then process the latest available observation for the 104 regional-only tickers.
9. Stop after latest-price recovery; adjusted historical backfill remains a separate phase.

## Not executed

- Latest parser canaries: 0.
- DB canary rows: 0.
- Local/Supabase writes: 0.
- Viewer checks after regional write: 0.
- 104-ticker backfill: 0.
- 418A cleanup: 0 (`KNOWN_PREEXISTING_CONTAMINATION`).

The canary CSV records the planned representatives and the license-review exclusion explicitly; it contains no fabricated normalized prices.
