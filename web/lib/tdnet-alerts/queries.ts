import { SupabaseClient } from "@supabase/supabase-js";
import type { TdnetEvent, TdnetEventComment, EnrichedEvent } from "./types";

// ============================================================
// 一覧取得
// ============================================================
export async function fetchEvents(
  supabase: SupabaseClient,
  opts: {
    userId: string;
    limit?: number;
    eventType?: string;
    search?: string;
    unreadOnly?: boolean;
    starredOnly?: boolean;
    discordOnly?: boolean;
    todayOnly?: boolean;
    selectedDate?: string | null; // YYYY-MM-DD (JST)
    showArchived?: boolean;
  }
): Promise<EnrichedEvent[]> {
  const limit = opts.limit ?? 1000;

  // イベント取得
  // ソート: disclosed_at DESC NULLS LAST（実開示日時優先）→ detected_at DESC → created_at DESC
  let query = supabase
    .from("tdnet_events")
    .select("*")
    .order("disclosed_at", { ascending: false, nullsFirst: false })
    .order("detected_at", { ascending: false })
    .order("created_at", { ascending: false })
    .limit(limit);

  if (!opts.showArchived) {
    query = query.eq("status", "active");
  }

  if (opts.discordOnly) {
    query = query.eq("notify_to_discord", true);
  }

  if (opts.eventType) {
    if (opts.eventType === "forecast_up") {
      query = query.eq("event_type", "forecast").eq("event_subtype", "upward");
    } else if (opts.eventType === "forecast") {
      query = query.eq("event_type", "forecast");
    } else if (opts.eventType === "dividend") {
      query = query.eq("event_type", "dividend");
    } else {
      query = query.eq("event_type", opts.eventType);
    }
  }

  if (opts.search) {
    const s = opts.search.trim();
    query = query.or(`ticker.ilike.%${s}%,company_name.ilike.%${s}%,headline.ilike.%${s}%`);
  }

  // 日付フィルタ (JST日付 → UTC範囲変換)
  // JST の YYYY-MM-DD を UTC に変換: JST 00:00 = UTC -09:00 (前日 15:00)
  const _jstDateToUtcRange = (dateStr: string): { gte: string; lt: string } => {
    const [y, m, d] = dateStr.split("-").map(Number);
    // JST 00:00:00 → UTC: Date.UTC() でタイムゾーン非依存に計算
    const startUtc = new Date(Date.UTC(y, m - 1, d, 0, 0, 0) - 9 * 60 * 60 * 1000);
    const endUtc   = new Date(startUtc.getTime() + 24 * 60 * 60 * 1000);
    return { gte: startUtc.toISOString(), lt: endUtc.toISOString() };
  };

  if (opts.selectedDate) {
    // 特定日付フィルタ (selectedDate = "today" or "YYYY-MM-DD")
    const dateStr =
      opts.selectedDate === "today"
        ? new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Tokyo" }))
            .toLocaleDateString("sv") // "YYYY-MM-DD" (sv locale)
        : opts.selectedDate;
    const { gte, lt } = _jstDateToUtcRange(dateStr);
    console.log("[TDNET fetchEvents]", {
      selectedDate: opts.selectedDate,
      resolvedDateStr: dateStr,
      utcGte: gte,
      utcLt: lt,
      filterColumn: "detected_at",
    });
    query = query.gte("detected_at", gte).lt("detected_at", lt);
  } else if (opts.todayOnly) {
    // 後方互換
    const todayJst = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Tokyo" }))
      .toLocaleDateString("sv");
    const { gte, lt } = _jstDateToUtcRange(todayJst);
    console.log("[TDNET fetchEvents] todayOnly", { utcGte: gte, utcLt: lt, filterColumn: "detected_at" });
    query = query.gte("detected_at", gte).lt("detected_at", lt);
  } else if (!opts.search) {
    // 通常モード: 直近30日のみ取得（全期間だと古いデータが大量混入するため）
    const thirtyDaysAgo = new Date();
    thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
    thirtyDaysAgo.setHours(0, 0, 0, 0);
    console.log("[TDNET fetchEvents] 30-day window", { utcGte: thirtyDaysAgo.toISOString(), filterColumn: "detected_at" });
    query = query.gte("detected_at", thirtyDaysAgo.toISOString());
  }

  const { data: events, error } = await query;
  if (error) throw error;
  if (!events || events.length === 0) {
    console.log("[TDNET fetchEvents] DB取得: 0件");
    return [];
  }
  console.log("[TDNET fetchEvents] DB取得件数:", events.length, "| limit:", limit);

  // 既読情報を一括取得
  const eventIds = events.map((e: TdnetEvent) => e.id);
  const { data: reads } = await supabase
    .from("tdnet_event_reads")
    .select("event_id")
    .eq("user_id", opts.userId)
    .in("event_id", eventIds);

  const readSet = new Set((reads || []).map((r: { event_id: string }) => r.event_id));

  // スター情報を一括取得
  const { data: stars } = await supabase
    .from("tdnet_event_stars")
    .select("event_id")
    .eq("user_id", opts.userId)
    .in("event_id", eventIds);

  const starSet = new Set((stars || []).map((s: { event_id: string }) => s.event_id));

  // コメント数を一括取得
  const { data: commentCounts } = await supabase
    .from("tdnet_event_comments")
    .select("event_id")
    .in("event_id", eventIds);

  const commentCountMap = new Map<string, number>();
  (commentCounts || []).forEach((c: { event_id: string }) => {
    commentCountMap.set(c.event_id, (commentCountMap.get(c.event_id) || 0) + 1);
  });

  // Enriched events を作成
  let enriched: EnrichedEvent[] = events.map((e: TdnetEvent) => ({
    ...e,
    is_read: readSet.has(e.id),
    is_starred: starSet.has(e.id),
    comments_count: commentCountMap.get(e.id) || 0,
  }));

  // フィルタ
  if (opts.unreadOnly) {
    enriched = enriched.filter((e) => !e.is_read);
  }
  if (opts.starredOnly) {
    enriched = enriched.filter((e) => e.is_starred);
  }

  // ソート: 未読優先 → priority_rank asc → detected_at desc
  enriched.sort((a, b) => {
    if (a.is_read !== b.is_read) return a.is_read ? 1 : -1;
    if (a.priority_rank !== b.priority_rank) return a.priority_rank - b.priority_rank;
    return new Date(b.detected_at).getTime() - new Date(a.detected_at).getTime();
  });

  console.log("[TDNET fetchEvents] フィルター後件数:", enriched.length, "|",
    opts.unreadOnly ? "unreadOnly" : "",
    opts.starredOnly ? "starredOnly" : "",
    "(DB:", events.length, "-> 表示:", enriched.length, ")"
  );
  return enriched;
}

// ============================================================
// 既読操作
// ============================================================
export async function markAsRead(supabase: SupabaseClient, eventId: string, userId: string) {
  const { error } = await supabase
    .from("tdnet_event_reads")
    .upsert({ event_id: eventId, user_id: userId }, { onConflict: "event_id,user_id" });
  if (error) throw error;
}

export async function markAsUnread(supabase: SupabaseClient, eventId: string, userId: string) {
  const { error } = await supabase
    .from("tdnet_event_reads")
    .delete()
    .eq("event_id", eventId)
    .eq("user_id", userId);
  if (error) throw error;
}

// ============================================================
// スター操作
// ============================================================
export async function toggleStar(supabase: SupabaseClient, eventId: string, userId: string, isStarred: boolean) {
  if (isStarred) {
    await supabase.from("tdnet_event_stars").delete().eq("event_id", eventId).eq("user_id", userId);
  } else {
    await supabase.from("tdnet_event_stars").upsert({ event_id: eventId, user_id: userId }, { onConflict: "event_id,user_id" });
  }
}

// ============================================================
// コメント操作
// ============================================================
export async function fetchComments(supabase: SupabaseClient, eventId: string): Promise<TdnetEventComment[]> {
  const { data, error } = await supabase
    .from("tdnet_event_comments")
    .select("*")
    .eq("event_id", eventId)
    .order("created_at", { ascending: true });
  if (error) throw error;
  return data || [];
}

export async function addComment(supabase: SupabaseClient, eventId: string, userId: string, comment: string) {
  const { error } = await supabase
    .from("tdnet_event_comments")
    .insert({ event_id: eventId, user_id: userId, comment });
  if (error) throw error;
}

export async function deleteComment(supabase: SupabaseClient, commentId: string, userId: string) {
  const { error } = await supabase
    .from("tdnet_event_comments")
    .delete()
    .eq("id", commentId)
    .eq("user_id", userId);
  if (error) throw error;
}

// ============================================================
// セグメント業績取得
// ============================================================
export interface SegmentRow {
  segment_name: string;
  segment_sales: number | null;
  segment_profit: number | null;
  period: string | null;
  quarter: string | null;
  data_source: string | null;
}

/**
 * segment_financials からセグメント業績を取得する。
 * company_code / fiscal_year_end / quarter で絞り込み。
 * period / quarter が null の場合は ticker のみで最新件を取得。
 */
export async function fetchSegmentFinancials(
  supabase: SupabaseClient,
  ticker: string,
  period?: string | null,
  quarter?: string | null,
): Promise<SegmentRow[]> {
  try {
    let q = supabase
      .from("segment_financials")
      .select("segment_name, segment_sales, segment_profit, period, quarter, data_source")
      .eq("ticker", ticker)
      .neq("data_source", "excel_legacy")  // excel_legacy を除外
      .order("period", { ascending: false })
      .order("segment_name", { ascending: true })
      .limit(100);

    // デバッグ: period/quarter filter 一時無効
    // if (period) q = q.eq("period", period);
    // if (quarter) q = q.eq("quarter", quarter);

    const { data, error } = await q;
    if (error) throw error;
    return (data || []) as SegmentRow[];
  } catch {
    // テーブル不在またはRLS拒否の場合は空を返す
    return [];
  }
}
