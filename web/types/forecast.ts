// company-memo-app/types/forecast.ts から移植 (read-only 用途)
export interface ForecastRevision {
  pubdate: string | null;
  title: string | null;
  period: string | null;
  quarter: string | null;
  metric_name: string | null;
  before_value: number | null;
  after_value: number | null;
  delta_value: number | null;
  delta_pct: number | null;
  confidence: number | null;
  source_type: string | null;
}
