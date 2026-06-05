/**
 * period / quarter 正規化ユーティリティ
 *
 * PL行とセグメント行の join が表記揺れで失敗しないようにするため、
 * 共通の正規化関数を提供する。
 */

// ============================================================
// Period 正規化
// ============================================================

/**
 * period 文字列を統一形式 "YYYY-MM-DD" に正規化する。
 *
 * 対応形式:
 * - "2025-03-31" → そのまま
 * - "2025/03/31" → "2025-03-31"
 * - "2025-03-31T00:00:00.000Z" → "2025-03-31"
 * - "2025/3" → "2025-03-31" (月末日に変換)
 * - "2025/12" → "2025-12-31"
 */
export function normalizePeriod(raw: string | null | undefined): string {
    if (!raw) return "";
    let s = String(raw).trim();

    // ISO datetime → date部分のみ
    if (s.includes("T")) {
        s = s.split("T")[0];
    }

    // スラッシュ → ハイフン
    s = s.replace(/\//g, "-");

    // "YYYY-M" 形式 (例: "2025-3") → "YYYY-MM-DD" (月末)
    const shortMatch = s.match(/^(\d{4})-(\d{1,2})$/);
    if (shortMatch) {
        const year = parseInt(shortMatch[1], 10);
        const month = parseInt(shortMatch[2], 10);
        const lastDay = new Date(year, month, 0).getDate();
        return `${year}-${String(month).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
    }

    // "YYYY-MM-DD" 形式の正規化 (ゼロ埋め)
    const fullMatch = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
    if (fullMatch) {
        const y = fullMatch[1];
        const m = String(parseInt(fullMatch[2], 10)).padStart(2, "0");
        const d = String(parseInt(fullMatch[3], 10)).padStart(2, "0");
        return `${y}-${m}-${d}`;
    }

    return s;
}

// ============================================================
// Quarter 正規化
// ============================================================

/**
 * quarter 文字列を統一形式に正規化する。
 *
 * 対応形式:
 * - "1Q", "Q1", "q1", "第1四半期", "1" → "1Q"
 * - "2Q", "Q2", "q2", "第2四半期", "2" → "2Q"
 * - "3Q", "Q3", "q3", "第3四半期", "3" → "3Q"
 * - "4Q", "Q4", "q4", "第4四半期", "4", "FY", "通期" → "FY"
 */
export function normalizeQuarter(raw: string | null | undefined): string {
    if (!raw) return "";
    const s = String(raw).trim().toUpperCase();

    // 数字のみ: "1" → "1Q"
    if (/^\d$/.test(s)) {
        const n = parseInt(s, 10);
        if (n >= 1 && n <= 3) return `${n}Q`;
        if (n === 4) return "FY";
        return s;
    }

    // "Q1" スタイル → "1Q" スタイル
    const qMatch = s.match(/^Q(\d)$/);
    if (qMatch) {
        const n = parseInt(qMatch[1], 10);
        if (n >= 1 && n <= 3) return `${n}Q`;
        if (n === 4) return "FY";
    }

    // "1Q" スタイル
    const nqMatch = s.match(/^(\d)Q$/);
    if (nqMatch) {
        const n = parseInt(nqMatch[1], 10);
        if (n >= 1 && n <= 3) return `${n}Q`;
        if (n === 4) return "FY";
    }

    // 日本語
    if (s.includes("第1") || s.includes("第１")) return "1Q";
    if (s.includes("第2") || s.includes("第２")) return "2Q";
    if (s.includes("第3") || s.includes("第３")) return "3Q";
    if (s.includes("第4") || s.includes("第４")) return "FY";

    // FY / 通期
    if (s === "FY" || s === "通期" || s === "FULL" || s === "ANNUAL") return "FY";
    if (s === "4Q") return "FY";

    return s;
}
