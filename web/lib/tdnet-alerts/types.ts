// TypeScript types for TDNET Alerts

export interface TdnetEvent {
  id: string;
  created_at: string;
  detected_at: string;
  disclosed_at: string | null;
  ticker: string;
  company_name: string;
  market: string | null;
  event_type: string;
  event_subtype: string | null;
  headline: string;
  summary: string;
  source_title: string | null;
  source_url: string | null;
  pdf_url: string | null;
  raw_payload: Record<string, unknown>;
  strength_score: number | null;
  priority_rank: number;
  primary_metric_name: string | null;
  primary_metric_value: string | null;
  primary_metric_yoy: string | null;
  display_title: string;
  display_summary: string;
  formatted_message: string;
  sort_key: string | null;
  dedupe_key: string;
  notify_to_discord: boolean;
  discord_sent_at: string | null;
  archived_at: string | null;
  status: string;
  schema_version: number;
}

export interface TdnetEventRead {
  id: string;
  event_id: string;
  user_id: string;
  read_at: string;
}

export interface TdnetEventStar {
  id: string;
  event_id: string;
  user_id: string;
  starred_at: string;
}

export interface TdnetEventComment {
  id: string;
  created_at: string;
  event_id: string;
  user_id: string;
  comment: string;
}

// Enriched event with user-specific states
export interface EnrichedEvent extends TdnetEvent {
  is_read: boolean;
  is_starred: boolean;
  comments_count: number;
}

// Filter state
export type FilterType =
  | "all"
  | "unread"
  | "starred"
  | "buyback"
  | "forecast_up"
  | "forecast"
  | "dividend"
  | "earnings"
  | "discord"
  | "today";

export interface AlertsFilter {
  type: FilterType;
  search: string;
  showArchived: boolean;
}

// Event type display config
export const EVENT_TYPE_CONFIG: Record<
  string,
  { label: string; emoji: string; color: string }
> = {
  // 正規化後の6カテゴリ
  buyback: { label: "自社株買い", emoji: "📊", color: "#6366f1" },
  forecast: { label: "業績予想修正", emoji: "📈", color: "#f59e0b" },
  dividend: { label: "配当修正", emoji: "💰", color: "#10b981" },
  earnings: { label: "決算", emoji: "📋", color: "#3b82f6" },
  shareholder: { label: "大量保有", emoji: "👥", color: "#8b5cf6" },
  other: { label: "その他", emoji: "📄", color: "#94a3b8" },
  // 後方互換 (旧キー)
  forecast_revision: { label: "業績予想修正", emoji: "📈", color: "#f59e0b" },
  dividend_revision: { label: "配当修正", emoji: "💰", color: "#10b981" },
};

// headline ベースの軽い再判定キーワード (保険用)
const _HEADLINE_CATEGORY_RULES: [string, string[]][] = [
  ["buyback", ["自己株式", "自社株買", "自己株取得"]],
  ["forecast", ["業績予想", "予想修正", "上方修正", "下方修正"]],
  ["dividend", ["配当", "増配", "減配"]],
  ["earnings", ["決算短信", "決算"]],
  ["shareholder", ["大量保有", "変更報告書"]],
];

// event_type エイリアスマップ: 大文字・旧名含む全ての揺れを吸収
const _EVENT_TYPE_ALIAS: Record<string, string> = {
  // 正規化後 (小文字)
  buyback: "buyback",
  forecast: "forecast",
  dividend: "dividend",
  earnings: "earnings",
  shareholder: "shareholder",
  other: "other",
  // 旧パイプライン名
  forecast_revision: "forecast",
  dividend_revision: "dividend",
};

/**
 * event_type を表示用カテゴリに正規化する (唯一の正規化関数)。
 *
 * 1. trim().toLowerCase() で大文字小文字を統一
 * 2. エイリアスマップで既知カテゴリに解決
 * 3. 未知値は headline キーワードで再判定
 * 4. 最終フォールバック → "other"
 */
export function getDisplayCategory(eventType: string, headline?: string): string {
  const normalized = String(eventType ?? "").trim().toLowerCase();

  // 1) エイリアスマップで既知カテゴリに解決
  if (normalized in _EVENT_TYPE_ALIAS) {
    return _EVENT_TYPE_ALIAS[normalized];
  }

  // 2) headline ベース再判定 (保険)
  if (headline) {
    const h = headline.toLowerCase();
    for (const [cat, keywords] of _HEADLINE_CATEGORY_RULES) {
      if (keywords.some((kw) => h.includes(kw))) return cat;
    }
  }

  // 3) フォールバック
  return "other";
}

export const EVENT_SUBTYPE_LABELS: Record<string, string> = {
  resolution: "決議",
  status: "状況",
  result: "結果",
  cancellation: "消却",
  upward: "上方修正",
  downward: "下方修正",
  difference: "差異開示",
  neutral: "修正",
  increase: "増配",
  decrease: "減配",
  special_dividend: "特別配当",
  commemorative_dividend: "記念配当",
  maintain: "据え置き",
  undecided: "",  // サブタイプ未定の場合は親カテゴリラベルを表示
};
