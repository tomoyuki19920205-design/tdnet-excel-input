/**
 * web/lib/company-viewer/kpi-api.ts
 *
 * FinancialsTable.tsx が "type" import する型定義のみ。
 * 実際の KPI 読み書き関数は不要（read-only 表示のため）。
 */

export type KpiDefMap   = Record<number, string>;
export type KpiValueMap = Record<string, Record<number, string>>;
