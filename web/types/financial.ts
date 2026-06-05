// company-memo-app/types/financial.ts から移植 (read-only 用途)
export interface FinancialRecord {
  ticker: string;
  period: string;
  quarter: string;
  sales: number | null;
  gross_profit: number | null;
  operating_profit: number | null;
  ordinary_profit: number | null;
  net_income: number | null;
  eps: number | null;
  source: string;
  updated_at: string;
}
