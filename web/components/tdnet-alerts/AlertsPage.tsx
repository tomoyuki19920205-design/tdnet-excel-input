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
  const [search, setSearch] = useState("");
  const [audioEnabled, setAudioEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const supabaseRef = useRef(createClient());
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        limit: 200,
      };

      if (filter === "unread") opts.unreadOnly = true;
      else if (filter === "starred") opts.starredOnly = true;
      else if (filter === "buyback") opts.eventType = "buyback";
      else if (filter === "forecast_up") opts.eventType = "forecast_up";
      else if (filter === "dividend") opts.eventType = "dividend";
      else if (filter === "earnings") opts.eventType = "earnings";
      else if (filter === "today") opts.todayOnly = true;

      if (search.trim()) opts.search = search.trim();

      const data = await fetchEvents(supabaseRef.current, opts);
      setEvents(data);
    } catch (err) {
      console.error("Failed to load events:", err);
    } finally {
      setLoading(false);
    }
  }, [userId, filter, search]);

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

  const filters: { key: FilterType; label: string }[] = [
    { key: "all", label: `全件 (${events.length})` },
    { key: "unread", label: `未読 (${unreadCount})` },
    { key: "starred", label: "⭐ スター" },
    { key: "today", label: "📅 今日" },
    { key: "buyback", label: "📊 自社株買" },
    { key: "forecast_up", label: "📈 上方修正" },
    { key: "dividend", label: "💰 配当" },
    { key: "earnings", label: "📋 決算" },
  ];

  return (
    <div className="alerts-layout">
      {/* Header */}
      <header className="alerts-header">
        <div className="alerts-header-left">
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
            className={`filter-chip ${filter === f.key ? "active" : ""}`}
            onClick={() => handleFilterChange(f.key)}
          >
            {f.label}
          </button>
        ))}
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
            events.map((event, _idx) => {
              const badge = getBadgeConfig(event.event_type, event.headline);
              const strength = getStrengthDisplay(event);
              const priorityClass = !event.is_read ? getPriorityClass(event.priority_rank) : "";
              const subtypeLabel = event.event_subtype
                ? (EVENT_SUBTYPE_LABELS[event.event_subtype] ?? event.event_subtype)
                : "";

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

                  {/* Main content: formatted_message 最優先 */}
                  {(() => {
                    const fm = event.formatted_message?.trim() || "";
                    let mainText = fm
                      || [event.display_title, event.display_summary]
                          .filter((s) => s?.trim())
                          .join("\n")
                      || event.headline
                      || "";
                    // 指標行なし(改行なし) → headline をメイン本文に統合
                    const isShort = !mainText.includes("\n");
                    if (
                      isShort &&
                      event.headline?.trim() &&
                      !mainText.includes(event.headline.trim())
                    ) {
                      mainText = mainText + "\n" + event.headline;
                    }

                    // DEBUG
                    if (_idx < 3) {
                      console.log("[TDNET ALERT render]", {
                        id: event.id,
                        ticker: event.ticker,
                        formatted_message: event.formatted_message,
                        headline: event.headline,
                        renderedMainText: mainText,
                      });
                    }

                    return (
                      <div className="alert-card-body">{mainText}</div>
                    );
                  })()}
                </div>
              );
            })
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
