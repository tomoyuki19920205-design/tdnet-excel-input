/**
 * web/lib/company-viewer/memo-api.ts
 *
 * FinancialsTable.tsx が "type" import する型定義のみ。
 * 実際の DB アクセス関数は不要（read-only 表示のため）。
 */

export type GridData = string[][];

export type ManualTableType =
  | "pl_cum"
  | "pl_q"
  | "segment_cum"
  | "segment_q"
  | "segment_manual";
