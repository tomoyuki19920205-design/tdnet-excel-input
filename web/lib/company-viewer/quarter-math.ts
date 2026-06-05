/**
 * Q単体計算ユーティリティ
 *
 * financials の累計行から Q単体値を計算する。
 * 累計値: 1Q=1Q累計, 2Q=2Q累計-1Q累計, 3Q=3Q累計-2Q累計, FY=FY累計-3Q累計
 */

import type { FinancialRecord } from "@/types/financial";

// ============================================================
// Quarter 順序定義
// ============================================================
const QUARTER_CALC_ORDER: Record<string, number> = {
    "1Q": 0,
    "2Q": 1,
    "3Q": 2,
    "FY": 3,
};

/** 前四半期を返す (1Q→null, 2Q→1Q, 3Q→2Q, FY→3Q) */
const PREV_QUARTER: Record<string, string | null> = {
    "1Q": null,
    "2Q": "1Q",
    "3Q": "2Q",
    "FY": "3Q",
};

// ============================================================
// Q単体データ型
// ============================================================
export interface QStandaloneRow {
    period: string;
    quarter: string;
    sales: number | null;
    grossProfit: number | null;
    grossMarginRate: number | null; // 粗利率 = GP / SALES (%)
    sgAndA: number | null;       // 管理費 = GP - OP
    operatingProfit: number | null;
    opMargin: number | null;     // 営業利益率 = OP / SALES (%)
}

// ============================================================
// 累計PL拡張型 (管理費・営業利益率を追加)
// ============================================================
export interface CumulativeRow {
    period: string;
    quarter: string;
    sales: number | null;
    grossProfit: number | null;
    grossMarginRate: number | null; // 粗利率 = GP / SALES (%)
    sgAndA: number | null;       // 管理費 = GP - OP
    operatingProfit: number | null;
    opMargin: number | null;     // 営業利益率 = OP / SALES (%)
    source: string;
}

// ============================================================
// 安全な減算 (null安全)
// ============================================================
function safeSub(a: number | null, b: number | null): number | null {
    if (a === null || a === undefined) return null;
    if (b === null || b === undefined) return null;
    return a - b;
}

/** 管理費 = GP - OP */
function calcSgAndA(gp: number | null, op: number | null): number | null {
    return safeSub(gp, op);
}

/** 粗利率 = GP / SALES (%) */
function calcGrossMarginRate(gp: number | null, sales: number | null): number | null {
    if (gp === null || gp === undefined) return null;
    if (sales === null || sales === undefined || sales === 0) return null;
    const rate = (gp / sales) * 100;
    if (!isFinite(rate)) return null;
    return rate;
}

/** 営業利益率 = OP / SALES (%) */
function calcOpMargin(op: number | null, sales: number | null): number | null {
    if (op === null || op === undefined) return null;
    if (sales === null || sales === undefined || sales === 0) return null;
    return (op / sales) * 100;
}

// ============================================================
// period から年度を抽出 (例: "2025-03-31" → "2025-03-31")
// ============================================================
function extractPeriodYear(period: string): string {
    // period は "YYYY-MM-DD" 形式。年度単位のグルーピングにそのまま使える。
    return period;
}

// ============================================================
// 予想 source 定数 (実績と予想の区別に使用)
// ============================================================
/** J-Quants 予想系 source の集合。実績行と区別するために使用する。 */
export const FORECAST_SOURCES = new Set<string>([
    "jquants_nxf",            // 翌期予想 (NxFSales / NxFOP)
    "jquants_forecast_fy",    // 当期予想 (FSales / FOP、FY実績前)
    "jquants_forecast_next_fy",
    "jquants_forecast",
]);

// ============================================================
// 過去5年分フィルタ
// ============================================================
/**
 * 実績行は過去5年分、予想行（FORECAST_SOURCES）は全件を返す。
 * 予想行を period カウントから除外することで、来期予想が追加されても
 * 過去の実績データが押し出されないようにする。
 */
export function filterLast5Years(records: FinancialRecord[]): FinancialRecord[] {
    const actualRecords   = records.filter(r => !FORECAST_SOURCES.has(r.source ?? ""));
    const forecastRecords = records.filter(r =>  FORECAST_SOURCES.has(r.source ?? ""));

    // 実績の period のみでウィンドウを計算
    const uniquePeriods = [...new Set(actualRecords.map((r) => r.period))].sort().reverse();
    const last5Periods = new Set(uniquePeriods.slice(0, 5));

    return [
        ...actualRecords.filter(r => last5Periods.has(r.period)),
        ...forecastRecords,   // 予想行は全件（period 制限なし）
    ];
}

// ============================================================
// 累計PLデータ生成
// ============================================================
export function buildCumulativeRows(records: FinancialRecord[]): CumulativeRow[] {
    return records.map((r) => ({
        period: r.period,
        quarter: r.quarter,
        sales: r.sales,
        grossProfit: r.gross_profit,
        grossMarginRate: calcGrossMarginRate(r.gross_profit, r.sales),
        sgAndA: calcSgAndA(r.gross_profit, r.operating_profit),
        operatingProfit: r.operating_profit,
        opMargin: calcOpMargin(r.operating_profit, r.sales),
        source: r.source,
    }));
}

// ============================================================
// Q単体計算
// ============================================================
export function buildQStandaloneRows(records: FinancialRecord[]): QStandaloneRow[] {
    // 年度ごとにグルーピング
    const byPeriod = new Map<string, Map<string, FinancialRecord>>();
    for (const r of records) {
        const key = extractPeriodYear(r.period);
        if (!byPeriod.has(key)) byPeriod.set(key, new Map());
        byPeriod.get(key)!.set(r.quarter, r);
    }

    // 表示順と同じ順序で結果を生成
    const result: QStandaloneRow[] = [];

    for (const r of records) {
        const periodMap = byPeriod.get(extractPeriodYear(r.period));
        if (!periodMap) {
            result.push(emptyQRow(r.period, r.quarter));
            continue;
        }

        const prevQ = PREV_QUARTER[r.quarter];

        if (prevQ === null) {
            // 1Q: 単体 = 累計
            result.push({
                period: r.period,
                quarter: r.quarter,
                sales: r.sales,
                grossProfit: r.gross_profit,
                grossMarginRate: calcGrossMarginRate(r.gross_profit, r.sales),
                sgAndA: calcSgAndA(r.gross_profit, r.operating_profit),
                operatingProfit: r.operating_profit,
                opMargin: calcOpMargin(r.operating_profit, r.sales),
            });
        } else if (prevQ === undefined) {
            // 不明な quarter
            result.push(emptyQRow(r.period, r.quarter));
        } else {
            // 2Q/3Q/FY: 前四半期との差分
            const prevRecord = periodMap.get(prevQ);
            if (!prevRecord) {
                // 前四半期データなし → 安全に空表示
                result.push(emptyQRow(r.period, r.quarter));
            } else {
                const sales = safeSub(r.sales, prevRecord.sales);
                const gp = safeSub(r.gross_profit, prevRecord.gross_profit);
                const op = safeSub(r.operating_profit, prevRecord.operating_profit);
                const grossMarginRate = calcGrossMarginRate(gp, sales);
                const sgAndA = calcSgAndA(gp, op);
                const opMargin = calcOpMargin(op, sales);

                result.push({
                    period: r.period,
                    quarter: r.quarter,
                    sales,
                    grossProfit: gp,
                    grossMarginRate,
                    sgAndA,
                    operatingProfit: op,
                    opMargin,
                });
            }
        }
    }

    return result;
}

function emptyQRow(period: string, quarter: string): QStandaloneRow {
    return {
        period,
        quarter,
        sales: null,
        grossProfit: null,
        grossMarginRate: null,
        sgAndA: null,
        operatingProfit: null,
        opMargin: null,
    };
}

// ============================================================
// 表示用ソート (period ASC → quarter ASC: 1Q→2Q→3Q→FY)
// 古い→新しい（下に行くほど新しい）
// ============================================================
export function sortForDisplay<T extends { period: string; quarter: string }>(rows: T[]): T[] {
    return [...rows].sort((a, b) => {
        const periodCmp = (a.period || "").localeCompare(b.period || "");
        if (periodCmp !== 0) return periodCmp;
        const qa = QUARTER_CALC_ORDER[a.quarter] ?? 9;
        const qb = QUARTER_CALC_ORDER[b.quarter] ?? 9;
        return qa - qb; // 1Q(0) < 2Q(1) < 3Q(2) < FY(3)
    });
}
