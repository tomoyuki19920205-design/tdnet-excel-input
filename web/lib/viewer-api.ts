/**
 * web/lib/viewer-api.ts
 *
 * company-memo-app/lib/viewer-api.ts から以下の3関数のみ移植:
 *   - normalizeTicker (内部ヘルパー)
 *   - loadCompanyInfo
 *   - loadFinancials
 *   - loadForecastRevision
 *
 * 読み取り専用。メモ・KPI・セグメント編集は含まない。
 * Supabase クライアントは呼び出し元から受け取る（依存注入）。
 */
import type { SupabaseClient } from "@supabase/supabase-js";
import type { FinancialRecord } from "@/types/financial";
import type { ForecastRevision } from "@/types/forecast";

// ============================================================
// 内部ヘルパー
// ============================================================

/** ticker を正規化する（先頭4桁の数字を抽出してゼロパディング）*/
function normalizeTicker(ticker: string): string {
  if (!ticker) return "";
  const trimmed = ticker.trim();
  // 数字のみの場合: 4桁にゼロパディング
  const numOnly = trimmed.match(/^(\d+)$/);
  if (numOnly) {
    return numOnly[1].padStart(4, "0");
  }
  // 先頭4桁数字 + 英字 の場合はそのまま
  return trimmed.toUpperCase();
}

/** period 文字列 ("2025-03-31") から西暦年を整数で返す */
export function extractFiscalYear(period: string): number {
  const m = period.match(/^(\d{4})/);
  return m ? parseInt(m[1], 10) : 0;
}

const QUARTER_ORDER: Record<string, number> = {
  "1Q": 0, "2Q": 1, "3Q": 2, "4Q": 3, "FY": 4,
};

function sortFinancials(rows: FinancialRecord[]): FinancialRecord[] {
  return [...rows].sort((a, b) => {
    const periodCmp = (b.period || "").localeCompare(a.period || "");
    if (periodCmp !== 0) return periodCmp;
    const qa = QUARTER_ORDER[a.quarter] ?? 9;
    const qb = QUARTER_ORDER[b.quarter] ?? 9;
    return qb - qa;
  });
}

// ============================================================
// 会社情報
// ============================================================

export interface CompanyInfo {
  ticker: string;
  companyName: string | null;
}

/**
 * companies テーブルから会社名を取得する。
 * テーブル未存在・RLSエラー時は companyName: null にフォールバック。
 */
export async function loadCompanyInfo(
  supabase: SupabaseClient,
  ticker: string,
): Promise<CompanyInfo> {
  const t = normalizeTicker(ticker);
  if (!t) return { ticker, companyName: null };

  try {
    const { data, error } = await supabase
      .from("companies")
      .select("name_ja")
      .eq("ticker_code", t)
      .maybeSingle();

    if (error) {
      console.warn("[viewer-api] companies 取得スキップ:", error.message, "code:", error.code);
      return { ticker: t, companyName: null };
    }

    return { ticker: t, companyName: data?.name_ja ?? null };
  } catch (err) {
    console.warn("[viewer-api] companies 例外:", err);
    return { ticker: t, companyName: null };
  }
}

// ============================================================
// PL (financials)
// ============================================================

/**
 * api_latest_financials ビューから直近PLデータを取得する。
 * RLSで弾かれた場合は error オブジェクトを返す。
 */
export async function loadFinancials(
  supabase: SupabaseClient,
  ticker: string,
): Promise<{ data: FinancialRecord[]; error: string | null }> {
  const t = normalizeTicker(ticker);
  if (!t) return { data: [], error: null };

  try {
    const { data, error } = await supabase
      .from("api_latest_financials_canonical")
      .select("ticker,period,quarter,sales,gross_profit,operating_profit,source,updated_at")
      .eq("ticker", t)
      .neq("source", "legacy_excel")
      .order("period", { ascending: false })
      .order("quarter", { ascending: false })
      .limit(50);

    if (error) {
      const msg = `[api_latest_financials] ${error.message} (code: ${error.code})`;
      console.error("[viewer-api] PL取得エラー:", msg);
      return { data: [], error: msg };
    }

    if (!data || data.length === 0) return { data: [], error: null };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const records: FinancialRecord[] = (data as any[]).map((row) => ({
      ticker: row.ticker,
      period: row.period ?? "",
      quarter: row.quarter ?? "",
      sales: row.sales ?? null,
      gross_profit: row.gross_profit ?? null,
      operating_profit: row.operating_profit ?? null,
      ordinary_profit: null,
      net_income: null,
      eps: null,
      source: row.source ?? "",
      updated_at: row.updated_at ?? "",
    }));

    return { data: sortFinancials(records), error: null };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[viewer-api] PL例外:", msg);
    return { data: [], error: msg };
  }
}

// ============================================================
// 業績修正
// ============================================================

/**
 * forecast_revision テーブルから業績修正データを取得する。
 * テーブル未存在の場合は空配列（エラーなし）で返す。
 */
export async function loadForecastRevision(
  supabase: SupabaseClient,
  ticker: string,
): Promise<ForecastRevision[]> {
  const t = normalizeTicker(ticker);
  if (!t) return [];

  try {
    const { data, error } = await supabase
      .from("forecast_revision")
      .select("*")
      .eq("ticker", t)
      .order("pubdate", { ascending: false })
      .limit(10);

    if (error) {
      // テーブル未存在は想定内（警告のみ）
      console.warn("[viewer-api] forecast_revision スキップ:", error.message);
      return [];
    }

    return (data as ForecastRevision[]) ?? [];
  } catch (err) {
    console.warn("[viewer-api] forecast_revision 例外:", err);
    return [];
  }
}
