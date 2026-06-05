// ============================================================
// 金額カラム共通定義 — 百万円正規化の対象
// ============================================================

/**
 * 百万円正規化の対象となる金額カラム。
 * writer / backfill / viewer すべてでこの定義を参照する。
 * 将来カラム追加時はここに追記するだけで全体に反映される。
 */
export const MONEY_COLUMNS = [
    "sales",
    "gross_profit",
    "operating_profit",
    // --- 将来追加 ---
    // "ordinary_profit",
    // "profit",
    // "net_income",
    // "assets",
    // "equity",
    // "operating_cf",
    // "investing_cf",
    // "financing_cf",
] as const;

// ============================================================
// 表示フォーマッタ
// ============================================================

/**
 * 数値を百万円単位で桁区切り表示にフォーマットする。
 * DB は全件 unit='million_yen' 統一済みのため、値をそのまま表示する。
 * null/undefined は "–" を返す。
 */
export function formatMillions(val: number | null | undefined): string {
    if (val === null || val === undefined) return "–";
    return Math.round(val).toLocaleString("ja-JP");
}

/**
 * 数値を桁区切り表示にフォーマットする (元値そのまま)
 * null/undefined は "–" を返す
 */
export function formatNumber(val: number | null | undefined): string {
    if (val === null || val === undefined) return "–";
    return val.toLocaleString("ja-JP");
}

/**
 * 数値を % 表示にフォーマットする
 * null/undefined は "–" を返す
 */
export function formatPercent(val: number | null | undefined): string {
    if (val === null || val === undefined) return "–";
    return `${val.toFixed(1)}%`;
}

/**
 * 日付文字列を「YYYY/MM/DD」形式に変換する
 * null/undefined/空文字は "–" を返す
 */
export function formatDate(val: string | null | undefined): string {
    if (!val) return "–";
    try {
        const d = new Date(val);
        if (isNaN(d.getTime())) return val;
        return d.toLocaleDateString("ja-JP", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
        });
    } catch {
        return val;
    }
}

/**
 * 値が null/undefined/空文字なら "–" を返す
 */
export function displayValue(val: string | number | null | undefined): string {
    if (val === null || val === undefined || val === "") return "–";
    return String(val);
}
