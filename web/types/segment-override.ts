/**
 * segment_cell_overrides テーブルの型定義
 */
export interface SegmentCellOverride {
    id: string;
    ticker: string;
    fiscal_year: number;
    quarter: string;       // '1Q' | '3Q'
    segment_name: string;
    metric: string;        // 'sales' | 'operating_profit'
    value: number | null;
    base_source: string | null;
    input_scope: string;
    note: string | null;
    created_by: string | null;
    updated_by: string | null;
    is_deleted: boolean;
    created_at: string;
    updated_at: string;
}

/**
 * Override 保存リクエスト (1セル分)
 */
export interface SegmentOverrideSaveRequest {
    ticker: string;
    fiscal_year: number;
    quarter: string;       // '1Q' | '3Q'
    segment_name: string;
    metric: string;        // 'sales' | 'operating_profit'
    value: number | null;
    base_source?: string;
    note?: string;
}
