export interface SegmentRecord {
    ticker: string;
    period: string;
    quarter: string;
    segment_name: string;
    segment_sales: number | null;
    segment_profit: number | null;
    source?: string;
    /** source_priority: api_latest_segments の優先順位 (小さいほど優先) */
    source_priority?: number | null;
    /** Per-metric source for badge display (set by overlay resolution) */
    _salesSource?: string;
    /** Per-metric source for badge display (set by overlay resolution) */
    _profitSource?: string;
}
