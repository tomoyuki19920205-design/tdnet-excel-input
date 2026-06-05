"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { createClient } from "@/lib/supabase/client";
import { fetchEvents, markAsRead, markAsUnread, toggleStar } from "@/lib/tdnet-alerts/queries";
import { useRealtimeAlerts } from "@/lib/tdnet-alerts/realtime";
import { audioManager } from "@/lib/tdnet-alerts/audio";
import type { EnrichedEvent, TdnetEvent, FilterType } from "@/lib/tdnet-alerts/types";
import { EVENT_TYPE_CONFIG, EVENT_SUBTYPE_LABELS, getDisplayCategory } from "@/lib/tdnet-alerts/types";
import AlertDetailPanel from "./AlertDetailPanel";

interface AlertsPageProps {
  userId: string;
  userEmail: string;
}

export default function AlertsPage({ userId, userEmail }: AlertsPageProps) {
  const [events, setEvents] = useState<EnrichedEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterType>("all");
  const [selectedDate, setSelectedDate] = useState<string | null>(null); // YYYY-MM-DD (JST)
  const [search, setSearch] = useState("");
  const [audioEnabled, setAudioEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const supabaseRef = useRef(createClient());
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dateInputRef = useRef<HTMLInputElement | null>(null);

  // Realtime 接続
  const { status: connectionStatus } = useRealtimeAlerts({
    onNewEvent: (newEvent: TdnetEvent) => {
      setEvents((prev) => {
        // 重複チェック
        if (prev.some((e) => e.id === newEvent.id)) return prev;
        const enriched: EnrichedEvent = {
          ...newEvent,
          is_read: false,
          is_starred: false,
          comments_count: 0,
        };
        return [enriched, ...prev];
      });
    },
  });

  // イベント読み込み
  const loadEvents = useCallback(async () => {
    setLoading(true);
    try {
      const opts: Parameters<typeof fetchEvents>[1] = {
        userId,
        limit: 1000,
      };

      if (filter === "unread") opts.unreadOnly = true;
      else if (filter === "starred") opts.starredOnly = true;
      else if (filter === "buyback") opts.eventType = "buyback";
      else if (filter === "forecast_up") opts.eventType = "forecast_up";
      else if (filter === "forecast") opts.eventType = "forecast";
      else if (filter === "dividend") opts.eventType = "dividend";
      else if (filter === "earnings") opts.eventType = "earnings";
      else if (filter === "discord") opts.discordOnly = true;
      else if (filter === "today") opts.selectedDate = "today";

      // 日付フィルタ（today フィルタより selectedDate が優先）
      if (selectedDate) opts.selectedDate = selectedDate;

      if (search.trim()) opts.search = search.trim();

      const data = await fetchEvents(supabaseRef.current, opts);
      setEvents(data);
    } catch (err) {
      console.error("Failed to load events:", err);
    } finally {
      setLoading(false);
    }
  }, [userId, filter, search, selectedDate]);

  useEffect(() => {
    loadEvents();
  }, [loadEvents]);

  // 音を初期化
  useEffect(() => {
    audioManager.restoreFromStorage();
    setAudioEnabled(audioManager.isEnabled);
  }, []);

  const handleToggleAudio = () => {
    const enabled = audioManager.toggle();
    setAudioEnabled(enabled);
  };

  const handleFilterChange = (f: FilterType) => {
    setFilter(f);
    // 「今日」以外のフィルタに切り替えた場合は日付選択を解除
    if (f !== "today") setSelectedDate(null);
  };

  // 「今日」ボタンクリック → date input を開く
  const handleTodayClick = () => {
    if (filter !== "today") {
      setFilter("today");
      setSelectedDate(null);
    }
    // date input を開く（showPicker 未対応ブラウザは focus/click にフォールバック）
    setTimeout(() => {
      const input = dateInputRef.current;
      if (input) {
        if (typeof input.showPicker === "function") {
          input.showPicker();
        } else {
          input.focus();
          input.click();
        }
      }
    }, 50);
  };

  // date input の変更 (YYYY-MM-DD)
  const handleDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value; // "YYYY-MM-DD" or ""
    if (val) {
      setSelectedDate(val);
      setFilter("today"); // today フィルタとして扱う
    } else {
      setSelectedDate(null);
    }
  };

  // 日付フィルタ解除
  const handleClearDate = () => {
    setSelectedDate(null);
    setFilter("all");
    if (dateInputRef.current) dateInputRef.current.value = "";
  };

  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    searchDebounceRef.current = setTimeout(() => {
      // search state が変わると useEffect で loadEvents が呼ばれる
    }, 300);
  };

  const handleSelectEvent = async (event: EnrichedEvent) => {
    setSelectedId(event.id);
    if (!event.is_read) {
      try {
        await markAsRead(supabaseRef.current, event.id, userId);
        setEvents((prev) =>
          prev.map((e) => (e.id === event.id ? { ...e, is_read: true } : e))
        );
      } catch (err) {
        console.error("Failed to mark as read:", err);
      }
    }
  };

  const handleToggleRead = async (event: EnrichedEvent, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (event.is_read) {
        await markAsUnread(supabaseRef.current, event.id, userId);
      } else {
        await markAsRead(supabaseRef.current, event.id, userId);
      }
      setEvents((prev) =>
        prev.map((ev) =>
          ev.id === event.id ? { ...ev, is_read: !ev.is_read } : ev
        )
      );
    } catch (err) {
      console.error("Failed to toggle read:", err);
    }
  };

  const handleToggleStar = async (event: EnrichedEvent, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await toggleStar(supabaseRef.current, event.id, userId, event.is_starred);
      setEvents((prev) =>
        prev.map((ev) =>
          ev.id === event.id ? { ...ev, is_starred: !ev.is_starred } : ev
        )
      );
    } catch (err) {
      console.error("Failed to toggle star:", err);
    }
  };

  const handleLogout = async () => {
    await supabaseRef.current.auth.signOut();
    window.location.href = "/login";
  };

  const selectedEvent = events.find((e) => e.id === selectedId) || null;
  const unreadCount = events.filter((e) => !e.is_read).length;

  const formatTime = (dt: string) => {
    const d = new Date(dt);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const MM = String(d.getMonth() + 1).padStart(2, "0");
    const DD = String(d.getDate()).padStart(2, "0");
    return `${MM}/${DD} ${hh}:${mm}`;
  };

  const getBadgeConfig = (eventType: string, headline?: string) => {
    const cat = getDisplayCategory(eventType, headline);
    const config = EVENT_TYPE_CONFIG[cat] || { label: "その他", emoji: "📄", color: "#94a3b8" };
    return { ...config, category: cat };
  };

  const getStrengthDisplay = (event: EnrichedEvent) => {
    if (event.primary_metric_value) {
      const yoy = event.primary_metric_yoy || "";
      return { value: event.primary_metric_value, yoy };
    }
    if (event.strength_score != null) {
      return { value: `${event.strength_score.toFixed(0)}`, yoy: "" };
    }
    return { value: "", yoy: "" };
  };

  const getPriorityClass = (rank: number) => {
    if (rank <= 10) return "priority-high";
    if (rank <= 30) return "priority-medium";
    return "";
  };

  // ============================================================
  // Discord対象タブ専用フォーマッタ
  // raw_payload.extracted から Discord通知相当の表示文字列を生成
  // ============================================================
  const formatDiscordStyleBody = (event: EnrichedEvent): string => {
    const rawVal = event.raw_payload;
    const rp: Record<string, unknown> | null =
      typeof rawVal === "string"
        ? (() => {
            try {
              return JSON.parse(rawVal) as Record<string, unknown>;
            } catch {
              return null;
            }
          })()
        : (rawVal as Record<string, unknown> | null) ?? null;

    const ext = (
      rp && typeof rp === "object" && rp.extracted && typeof rp.extracted === "object"
        ? rp.extracted
        : {}
    ) as Record<string, unknown>;

    const fmtPct = (v: unknown): string => {
      const n = Number(v);
      if (isNaN(n)) return "?%";
      const sign = n > 0 ? "+" : "";
      return `${sign}${n.toFixed(1)}%`;
    };
    const fmtBillion = (v: unknown): string => {
      const n = Number(v);
      if (isNaN(n)) return "---";
      if (Math.abs(n) >= 100) return `${(n / 100).toFixed(1)}億円`;
      return `${n.toFixed(0)}百万円`;
    };
    const fmtShares = (v: unknown): string => {
      const n = Number(v);
      if (isNaN(n)) return "---";
      if (n >= 10000) return `${(n / 10000).toFixed(1)}万株`;
      return `${n.toLocaleString()}株`;
    };
    const fmtDiv = (v: unknown): string => {
      const n = Number(v);
      if (isNaN(n)) return "---";
      return n === Math.floor(n) ? `${Math.floor(n)}円` : `${n}円`;
    };

    const lines: string[] = [];

    // ─── 1. 会社名（ティッカー）─── 必ず最上段
    const companyLabel = event.company_name
      ? `${event.company_name}（${event.ticker}）`
      : event.ticker;
    lines.push(companyLabel);

    // ─── 2. イベント種別ラベル + 主要数値 ───
    if (event.event_type === "forecast") {
      const subtypeLabel = event.event_subtype === "upward" ? "🔺 上方修正"
        : event.event_subtype === "difference" ? "📋 差異開示"
        : event.event_subtype === "downward" ? "🔻 下方修正"
        : "📊 業績修正";
      lines.push(subtypeLabel);

      const opPct  = ext.change_op_pct;
      const ordPct = ext.change_ordinary_pct;
      const netPct = ext.change_net_income_pct;
      const metrics: string[] = [];
      if (opPct  != null) metrics.push(`営業利益 ${fmtPct(opPct)}`);
      if (ordPct != null) metrics.push(`経常利益 ${fmtPct(ordPct)}`);
      if (netPct != null) metrics.push(`純利益 ${fmtPct(netPct)}`);
      if (metrics.length > 0) lines.push(metrics.join("  "));

      const epsPrev = ext.previous_eps;
      const epsRev  = ext.revised_eps;
      if (epsPrev != null && epsRev != null) {
        const p = Number(epsPrev), r = Number(epsRev);
        if (!isNaN(p) && !isNaN(r) && Math.abs(p) <= 10000 && Math.abs(r) <= 10000) {
          if (p !== 0) {
            const pct = (r - p) / Math.abs(p) * 100;
            lines.push(`EPS: ${fmtDiv(p)}→${fmtDiv(r)}(${fmtPct(pct)})`);
          } else {
            lines.push(`EPS: ${fmtDiv(p)}→${fmtDiv(r)}`);
          }
        }
      }

    } else if (event.event_type === "buyback") {
      const subtypeLabel = event.event_subtype === "tostnet" ? "📊 自社株買い（ToSTNeT）" : "📊 自社株買い（取得枠決議）";
      lines.push(subtypeLabel);

      const ratio  = ext.ratio_to_outstanding;
      const shares = ext.shares_limit;
      const amount = ext.amount_limit_million_yen;
      const start  = ext.start_date;
      const end    = ext.end_date;
      const specs: string[] = [];
      if (ratio  != null) specs.push(`比率 ${Number(ratio).toFixed(2)}%`);
      if (shares != null) specs.push(`株数 ${fmtShares(shares)}`);
      if (amount != null) specs.push(`金額 ${fmtBillion(amount)}`);
      if (specs.length > 0) lines.push(specs.join("  "));
      if (event.event_subtype === "tostnet" && start) {
        lines.push(`買付日: ${String(start)}`);
      } else if (start && end) {
        lines.push(`取得期間: ${String(start)}〜${String(end)}`);
      } else if (start) {
        lines.push(`取得開始: ${String(start)}`);
      }

    } else if (event.event_type === "dividend") {
      const subtypeLabel = event.event_subtype === "increase" ? "💰 増配"
        : event.event_subtype === "decrease" ? "📉 減配"
        : "💰 配当修正";
      lines.push(subtypeLabel);

      const prev = ext.previous_dividend_per_share;
      const rev  = ext.revised_dividend_per_share;
      if (rev != null) {
        const r = Number(rev);
        const p = prev != null ? Number(prev) : null;
        if (!isNaN(r)) {
          if (p !== null && !isNaN(p) && p !== 0) {
            const pct = (r - p) / Math.abs(p) * 100;
            lines.push(`配当: ${fmtDiv(p)}→${fmtDiv(r)}(${fmtPct(pct)})`);
          } else {
            lines.push(`配当: ${fmtDiv(r)}`);
          }
        }
      }
      const period = ext.fiscal_period;
      if (period) lines.push(String(period));

    } else {
      // fallback: subtype があれば表示
      if (event.event_subtype) lines.push(event.event_subtype);
    }

    // ─── 3. ヘッドライン（開示タイトル）───
    if (event.headline) lines.push(event.headline);

    // ─── 4. 開示URL ───
    const url = event.source_url ||
      (rp && typeof rp.doc_url === "string" ? rp.doc_url : "");
    if (url) lines.push(`開示: ${url}`);

    // ─── 5. Discord送信済み ───
    if (event.discord_sent_at) {
      const d = new Date(event.discord_sent_at);
      const mm  = String(d.getMonth() + 1).padStart(2, "0");
      const dd  = String(d.getDate()).padStart(2, "0");
      const hh  = String(d.getHours()).padStart(2, "0");
      const min = String(d.getMinutes()).padStart(2, "0");
      lines.push(`🔔 Discord送信済み: ${d.getFullYear()}-${mm}-${dd} ${hh}:${min}`);
    }

    return lines.filter((s) => s.trim()).join("\n") || event.headline || "";
  };

  // "today" は別途JSXで日付ピッカー付きボタンとして実装するため除外
  const filters: { key: FilterType; label: string }[] = [
    { key: "all", label: `全件 (${events.length})` },
    { key: "unread", label: `未読 (${unreadCount})` },
    { key: "starred", label: "⭐ スター" },
    { key: "buyback", label: "📊 自社株買" },
    { key: "forecast_up", label: "📈 上方修正" },
    { key: "forecast", label: "📉 業績修正" },
    { key: "dividend", label: "💰 配当" },
    { key: "earnings", label: "📋 決算" },
    { key: "discord", label: "🔔 Discord対象" },
  ];

  // selectedDate の表示ラベル (YYYY-MM-DD → MM/DD)
  const todayBtnLabel = (() => {
    if (selectedDate) {
      const [, m, d] = selectedDate.split("-");
      return `📅 ${m}/${d}`;
    }
    return "📅 今日";
  })();

  return (
    <div className="alerts-layout">
      {/* Header */}
      <header className="alerts-header">
        <div className="alerts-header-left">
          <a
            href={process.env.NEXT_PUBLIC_COMPANY_VIEWER_URL ?? "http://localhost:3000/"}
            className="site-link"
          >
            🏢 Company Viewer
          </a>
          <h1 className="alerts-header-title">TDNET Alerts</h1>
          <span className="stat-badge unread">未読 {unreadCount}</span>
          <span className="stat-badge total">全 {events.length}件</span>
        </div>
        <div className="alerts-header-right">
          <span className="stat-badge" title={`接続: ${connectionStatus}`}>
            <span className={`connection-dot ${connectionStatus}`} />
            {connectionStatus === "connected" ? "Live" : connectionStatus}
          </span>
          <button
            className={`audio-toggle ${audioEnabled ? "enabled" : ""}`}
            onClick={handleToggleAudio}
            title={audioEnabled ? "音をOFFにする" : "クリックで音をON"}
          >
            {audioEnabled ? "🔔 ON" : "🔕 OFF"}
          </button>
          <span
            style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}
          >
            {userEmail}
          </span>
          <button className="logout-btn" onClick={handleLogout}>
            ログアウト
          </button>
        </div>
      </header>

      {/* Filter Bar */}
      <div className="filter-bar">
        {filters.map((f) => (
          <button
            key={f.key}
            className={`filter-chip ${filter === f.key && !selectedDate ? "active" : ""}`}
            onClick={() => handleFilterChange(f.key)}
          >
            {f.label}
          </button>
        ))}

        {/* 日付ピッカー付き「今日」ボタン */}
        <div className="date-filter-wrap">
          <button
            className={`filter-chip ${filter === "today" ? "active" : ""}`}
            onClick={handleTodayClick}
            title="クリックで日付を選択"
          >
            {todayBtnLabel}
          </button>
          {selectedDate && (
            <button
              className="date-clear-btn"
              onClick={handleClearDate}
              title="日付フィルタを解除"
              aria-label="日付フィルタを解除"
            >
              ×
            </button>
          )}
          {/* hidden date input: showPicker() で開く */}
          <input
            ref={dateInputRef}
            type="date"
            className="date-picker-hidden"
            value={selectedDate ?? ""}
            onChange={handleDateChange}
            tabIndex={-1}
            aria-hidden="true"
          />
        </div>

        <input
          type="text"
          className="filter-search"
          placeholder="🔍 ティッカー / 会社名 / ヘッドライン"
          value={search}
          onChange={(e) => handleSearchChange(e.target.value)}
        />
      </div>

      {/* Main Content */}
      <div className="alerts-content">
        {/* List Pane */}
        <div className="alerts-list-pane">
          {loading ? (
            <div className="loading-spinner">読み込み中...</div>
          ) : events.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">📭</div>
              <div>イベントがありません</div>
              <div style={{ fontSize: "0.8rem" }}>
                フィルタを変更するか、新着を待ってください
              </div>
            </div>
          ) : (
            (() => {
            const isDiscordTab = filter === "discord";
            return events.map((event, _idx) => {
              const badge = getBadgeConfig(event.event_type, event.headline);
              const strength = getStrengthDisplay(event);
              const priorityClass = !event.is_read ? getPriorityClass(event.priority_rank) : "";
              const subtypeLabel = event.event_subtype
                ? (EVENT_SUBTYPE_LABELS[event.event_subtype] ?? event.event_subtype)
                : "";

              // ─── カード本文を決定 ────────────────────────────────
              let cardBody: string;
              if (isDiscordTab) {
                cardBody = formatDiscordStyleBody(event);
              } else {
                const fm = event.formatted_message?.trim() || "";
                let mainText = fm
                  || [event.display_title, event.display_summary]
                      .filter((s) => s?.trim())
                      .join("\n")
                  || event.headline
                  || "";
                const isShort = !mainText.includes("\n");
                if (
                  isShort &&
                  event.headline?.trim() &&
                  !mainText.includes(event.headline.trim())
                ) {
                  mainText = mainText + "\n" + event.headline;
                }
                cardBody = mainText;
              }
              // DEBUG LOG
              console.log("[TDNET card render]", {
                filter,
                isDiscordTab,
                ticker: event.ticker,
                eventType: event.event_type,
                subtype: event.event_subtype,
                cardBody: cardBody.slice(0, 80),
              });
              // ─────────────────────────────────────────────────────

              return (
                <div
                  key={event.id}
                  className={`alert-card ${!event.is_read ? "unread" : ""} ${
                    selectedId === event.id ? "selected" : ""
                  } ${priorityClass}`}
                  onClick={() => handleSelectEvent(event)}
                >
                  {/* Row 1: Time + Badge + Actions */}
                  <div className="alert-card-header">
                    <span className="alert-time">
                      {formatTime(event.detected_at)}
                    </span>
                    <span className={`alert-badge ${badge.category}`}>
                      {badge.emoji} {subtypeLabel || badge.label}
                    </span>
                    <span className="alert-card-actions">
                      <button
                        className={`action-btn ${event.is_starred ? "active" : ""}`}
                        onClick={(e) => handleToggleStar(event, e)}
                        title="スター"
                      >
                        {event.is_starred ? "⭐" : "☆"}
                      </button>
                      <button
                        className={`action-btn ${!event.is_read ? "active" : ""}`}
                        onClick={(e) => handleToggleRead(event, e)}
                        title={event.is_read ? "未読に戻す" : "既読にする"}
                      >
                        {event.is_read ? "📖" : "📩"}
                      </button>
                      {event.comments_count > 0 && (
                        <span style={{ fontSize: "0.72rem", color: "var(--accent-purple)" }}>
                          💬{event.comments_count}
                        </span>
                      )}
                    </span>
                  </div>

                  {/* Main content */}
                  <div className="alert-card-body">{cardBody}</div>
                </div>
              );
            });
            })()
          )}
        </div>

        {/* Detail Pane */}
        <div className="alerts-detail-pane">
          {selectedEvent ? (
            <AlertDetailPanel
              event={selectedEvent}
              userId={userId}
              onUpdate={(updated) => {
                setEvents((prev) =>
                  prev.map((e) => (e.id === updated.id ? updated : e))
                );
              }}
            />
          ) : (
            <div className="detail-empty">
              イベントを選択してください
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
