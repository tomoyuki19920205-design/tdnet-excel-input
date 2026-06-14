# Database Schema & Views (Disaster Recovery Pack)

## Core Tables

### 1. `tdnet_events`
Stores timely disclosure events from TDNET.
- `id` (uuid, primary key)
- `ticker` (text, e.g. "7203")
- `company_name` (text)
- `pubdate` (timestamp)
- `title` (text)
- `document_url` (text)
- `raw_payload` (jsonb) - Raw extracted data
- `notification_compare_json` (jsonb) - YOY compare logic data for UI
- `extracted` (boolean)

### 2. `canonical_financials`
Stores normalized, source-of-truth financial data.
- `id` (uuid)
- `ticker` (text)
- `fiscal_year` (text)
- `fiscal_quarter` (text)
- `sales`, `operating_profit`, `ordinary_profit`, `net_profit` (numeric)
- `source` (text) - e.g. 'TDNET', 'J-Quants'
- `is_forecast` (boolean)

### 3. `financials`
Denormalized table optimized for the Company Viewer frontend.
- Contains pre-computed columns for UI rendering.
- `ticker`, `fiscal_year`, `fiscal_quarter`, `sales`, `operating_profit`, `eps`, etc.

## Core Views

### `api_latest_financials_canonical`
View used to fetch the latest full-year financial estimates for YoY comparisons.
```sql
CREATE OR REPLACE VIEW api_latest_financials_canonical AS
SELECT * FROM canonical_financials 
WHERE is_forecast = true 
ORDER BY fiscal_year DESC;
```
*(This represents the logical definition used by the system)*
