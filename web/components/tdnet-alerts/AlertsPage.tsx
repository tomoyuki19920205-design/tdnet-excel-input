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
  const [discordSortMode, setDiscordSortModeState] = useState<"timeline" | "category">(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("tdnet_discord_sort");
      if (saved === "category") return "category";
    }
    return "timeline";
  });
  const setDiscordSortMode = (mode: "timeline" | "category") => {
    setDiscordSortModeState(mode);
    if (typeof window !== "undefined") localStorage.setItem("tdnet_discord_sort", mode);
  };

  // 左ペイン幅（localStorage永続化）
  const [leftPaneWidth, setLeftPaneWidthState] = useState<number>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("tdnet_left_pane_width");
      const n = saved ? parseInt(saved, 10) : 0;
      if (n >= 360) return n;
    }
    return 400;
  });
  // 右ペインタブ（"detail" | "company"）
  const [rightPaneTab, setRightPaneTab] = useState<"detail" | "company">("company");

  const supabaseRef = useRef(createClient());
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dateInputRef = useRef<HTMLInputElement | null>(null);
  // ドラッグ用 ref
  const isDraggingRef = useRef(false);
  const dragStartXRef = useRef(0);
  const dragStartWidthRef = useRef(0);

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

  // ペインリサイズ：drag イベント
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      const delta = e.clientX - dragStartXRef.current;
      const newW = Math.max(360, dragStartWidthRef.current + delta);
      setLeftPaneWidthState(newW);
    };
    const handleMouseUp = (e: MouseEvent) => {
      if (!isDraggingRef.current) return;
      isDraggingRef.current = false;
      const delta = e.clientX - dragStartXRef.current;
      const newW = Math.max(360, dragStartWidthRef.current + delta);
      setLeftPaneWidthState(newW);
      localStorage.setItem("tdnet_left_pane_width", String(newW));
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
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
    setRightPaneTab("company"); // 開示クリック時は Company Viewer をデフォルト表示
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

    // ─── 1. ticker 会社名 ───
    const companyLabel = event.company_name
      ? `${event.ticker} ${event.company_name}`
      : event.ticker;
    lines.push(companyLabel);

    // ─── 2. イベント種別 + サマリ数値（1行で一目判断）───
    if (event.event_type === "forecast") {
      const typeEmoji = event.event_subtype === "upward" ? "🔺 上方修正"
        : event.event_subtype === "difference" ? "📋 差異開示"
        : event.event_subtype === "downward" ? "🔻 下方修正"
        : "📊 業績修正";
      // サマリ: 最初に見つかった差異率を1行に添える
      const opPct  = ext.change_op_pct;
      const ordPct = ext.change_ordinary_pct;
      const netPct = ext.change_net_income_pct;
      const summaryPct = opPct ?? ordPct ?? netPct;
      const summaryPctLabel = opPct != null ? "営業利益"
        : ordPct != null ? "経常利益"
        : netPct != null ? "純利益"
        : null;
      const summaryStr = summaryPctLabel != null
        ? `${summaryPctLabel} ${fmtPct(summaryPct)}`
        : "";
      lines.push(summaryStr ? `${typeEmoji}  ${summaryStr}` : typeEmoji);

      // ─── 3. 詳細数値 ───
      const metrics: string[] = [];
      if (opPct  != null) metrics.push(`営業利益 ${fmtPct(opPct)}`);
      if (ordPct != null) metrics.push(`経常利益 ${fmtPct(ordPct)}`);
      if (netPct != null) metrics.push(`純利益 ${fmtPct(netPct)}`);
      // サマリで使った指標と同じだが、全指標を並べる（1項目なら重複するが可読性優先）
      if (metrics.length > 1) lines.push(metrics.join("  "));
      // EPS
      const epsPrev = ext.previous_eps;
      const epsRev  = ext.revised_eps;
      if (epsPrev != null && epsRev != null) {
        const p = Number(epsPrev), r = Number(epsRev);
        if (!isNaN(p) && !isNaN(r) && Math.abs(p) <= 10000 && Math.abs(r) <= 10000) {
          const ePct = p !== 0 ? (r - p) / Math.abs(p) * 100 : null;
          lines.push(`EPS: ${fmtDiv(p)}→${fmtDiv(r)}${ePct !== null ? `(${fmtPct(ePct)})` : ""}`);
        }
      }
      // ─── 4. 対象期 ───
      const periodLabel = ext.period_label;
      if (periodLabel) lines.push(String(periodLabel));

    } else if (event.event_type === "buyback") {
      const typeLabel = event.event_subtype === "tostnet"
        ? "📊 自社株買い（ToSTNeT）"
        : "📊 自社株買い（取得枠決議）";
      const ratio = ext.ratio_to_outstanding;
      // サマリ: 比率を添える
      const ratioStr = ratio != null ? `${Number(ratio).toFixed(2)}%` : "";
      lines.push(ratioStr ? `${typeLabel}  ${ratioStr}` : typeLabel);

      // ─── 3. 詳細数値 ───
      const shares = ext.shares_limit;
      const amount = ext.amount_limit_million_yen;
      const specs: string[] = [];
      if (ratio  != null) specs.push(`割合 ${Number(ratio).toFixed(2)}%`);
      if (shares != null) specs.push(`株数 ${fmtShares(shares)}`);
      if (amount != null) specs.push(`金額 ${fmtBillion(amount)}`);
      if (specs.length > 0) lines.push(specs.join("  "));
      // ─── 4. 期間 ───
      const start = ext.start_date;
      const end   = ext.end_date;
      if (event.event_subtype === "tostnet" && start) {
        lines.push(`買付日: ${String(start)}`);
      } else if (start && end) {
        lines.push(`取得期間: ${String(start)}〜${String(end)}`);
      } else if (start) {
        lines.push(`取得開始: ${String(start)}`);
      }

    } else if (event.event_type === "dividend") {
      const typeLabel = event.event_subtype === "increase" ? "💰 増配"
        : event.event_subtype === "decrease" ? "📉 減配"
        : "💰 配当修正";
      // サマリ: 増配率を添える
      const prev = ext.previous_dividend_per_share;
      const rev  = ext.revised_dividend_per_share;
      let pctStr = "";
      let pv: number | null = null, rv: number | null = null;
      if (prev != null && rev != null) {
        pv = Number(prev); rv = Number(rev);
        if (!isNaN(pv) && !isNaN(rv) && pv !== 0) {
          pctStr = fmtPct((rv - pv) / Math.abs(pv) * 100);
        }
      }
      lines.push(pctStr ? `${typeLabel}  ${pctStr}` : typeLabel);

      // ─── 3. 詳細数値 ───
      if (rv != null && !isNaN(rv)) {
        if (pv !== null && !isNaN(pv) && pv !== 0) {
          lines.push(`配当: ${fmtDiv(pv)}→${fmtDiv(rv)}(${fmtPct((rv - pv) / Math.abs(pv) * 100)})`);
        } else {
          lines.push(`配当: ${fmtDiv(rv)}`);
        }
      }
      // ─── 4. 対象期 ───
      const period = ext.fiscal_period;
      if (period) lines.push(String(period));

    } else {
      // fallback
      if (event.event_subtype) lines.push(event.event_subtype);
    }

    // ─── 6. Discord送信済み（最下部）───
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
      {/* Discord対象タブのソートボタン（Discord選択時のみ表示）*/}
      {filter === "discord" && (
        <div className="discord-sort-bar">
          <span className="discord-sort-label">⇅ 並び順:</span>
          <button
            id="discord-sort-timeline"
            className={`discord-sort-btn ${discordSortMode === "timeline" ? "active" : ""}`}
            onClick={() => setDiscordSortMode("timeline")}
          >
            時系列
          </button>
          <button
            id="discord-sort-category"
            className={`discord-sort-btn ${discordSortMode === "category" ? "active" : ""}`}
            onClick={() => setDiscordSortMode("category")}
          >
            カテゴリー別
          </button>
        </div>
      )}

      {/* Main Content */}
      <div className="alerts-content">
        {/* List Pane */}
        <div
          className="alerts-list-pane"
          style={{ width: leftPaneWidth, flexShrink: 0 }}
        >
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

            // Discordタブ: ソート順をクライアントサイドで切り替え
            const displayEvents = isDiscordTab
              ? [...events].sort((a, b) => {
                  // 未読を常に上
                  if (a.is_read !== b.is_read) return a.is_read ? 1 : -1;
                  if (discordSortMode === "timeline") {
                    // 時系列: disclosed_at DESC NULLS LAST → detected_at DESC → created_at DESC
                    const da = a.disclosed_at ? new Date(a.disclosed_at).getTime() : 0;
                    const db = b.disclosed_at ? new Date(b.disclosed_at).getTime() : 0;
                    if (da !== db) return db - da;
                    const dda = new Date(a.detected_at).getTime();
                    const ddb = new Date(b.detected_at).getTime();
                    if (dda !== ddb) return ddb - dda;
                    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
                  } else {
                    // カテゴリー別: priority_rank → disclosed_at DESC → detected_at DESC
                    if (a.priority_rank !== b.priority_rank) return a.priority_rank - b.priority_rank;
                    const da = a.disclosed_at ? new Date(a.disclosed_at).getTime() : 0;
                    const db = b.disclosed_at ? new Date(b.disclosed_at).getTime() : 0;
                    if (da !== db) return db - da;
                    return new Date(b.detected_at).getTime() - new Date(a.detected_at).getTime();
                  }
                })
              : events;

            return displayEvents.map((event, _idx) => {
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

        {/* リサイズドラッガー */}
        <div
          className="pane-divider"
          onMouseDown={(e) => {
            isDraggingRef.current = true;
            dragStartXRef.current = e.clientX;
            dragStartWidthRef.current = leftPaneWidth;
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
            e.preventDefault();
          }}
        />

        {/* Detail Pane */}
        <div className="alerts-detail-pane">
          {selectedEvent ? (
            <>
              {/* 右ペインタブ */}
              <div className="right-pane-tabs">
                <button
                  id="right-tab-company"
                  className={`right-pane-tab-btn ${rightPaneTab === "company" ? "active" : ""}`}
                  onClick={() => setRightPaneTab("company")}
                >
                  🏢 Company Viewer
                </button>
                <button
                  id="right-tab-detail"
                  className={`right-pane-tab-btn ${rightPaneTab === "detail" ? "active" : ""}`}
                  onClick={() => setRightPaneTab("detail")}
                >
                  📋 イベント詳細
                </button>
              </div>

              {/* タブコンテンツ */}
              {rightPaneTab === "company" ? (() => {
                const cvBase = process.env.NEXT_PUBLIC_COMPANY_VIEWER_URL || "http://localhost:3000/";
                const cvUrl = `${cvBase}?ticker=${encodeURIComponent(selectedEvent.ticker)}`;
                return (
                  <div className="cv-launch-pane">
                    <div className="cv-launch-ticker">
                      {selectedEvent.ticker}
                      {selectedEvent.company_name && (
                        <span className="cv-launch-company">{selectedEvent.company_name}</span>
                      )}
                    </div>
                    <a
                      href={cvUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="cv-launch-btn"
                    >
                      🏢 Company Viewerを別タブで開く
                    </a>
                    <p className="cv-launch-note">
                      ログイン済みの場合はそのまま表示されます。<br />
                      未ログインの場合はログイン後に自動表示されます。
                    </p>
                  </div>
                );
              })() : (
                <AlertDetailPanel
                  event={selectedEvent}
                  userId={userId}
                  onUpdate={(updated) => {
                    setEvents((prev) =>
                      prev.map((e) => (e.id === updated.id ? updated : e))
                    );
                  }}
                />
              )}
            </>
          ) : (
            <div className="detail-empty">
              開示をクリックすると右側に Company Viewer が表示されます
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
