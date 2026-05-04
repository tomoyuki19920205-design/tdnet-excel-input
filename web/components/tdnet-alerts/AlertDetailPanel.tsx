"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { createClient } from "@/lib/supabase/client";
import {
  markAsRead,
  markAsUnread,
  toggleStar,
  fetchComments,
  addComment,
  fetchSegmentFinancials,
} from "@/lib/tdnet-alerts/queries";
import type { EnrichedEvent, TdnetEventComment } from "@/lib/tdnet-alerts/types";
import type { SegmentRow } from "@/lib/tdnet-alerts/queries";
import { EVENT_TYPE_CONFIG, EVENT_SUBTYPE_LABELS, getDisplayCategory } from "@/lib/tdnet-alerts/types";
import { buildSegmentViewData } from "@/lib/tdnet-alerts/segment-normalize";

interface AlertDetailPanelProps {
  event: EnrichedEvent;
  userId: string;
  onUpdate: (event: EnrichedEvent) => void;
}

export default function AlertDetailPanel({
  event,
  userId,
  onUpdate,
}: AlertDetailPanelProps) {
  const [comments, setComments] = useState<TdnetEventComment[]>([]);
  const [newComment, setNewComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const [rawSegments, setRawSegments] = useState<SegmentRow[]>([]);
  const [segLoading, setSegLoading] = useState(false);
  const supabaseRef = useRef(createClient());

  // rawSegments が変わった時だけ統合キー計算を実行（再描画のたびには走らない）
  const segmentViewData = useMemo(() => buildSegmentViewData(rawSegments), [rawSegments]);

  const loadComments = useCallback(async () => {
    try {
      const data = await fetchComments(supabaseRef.current, event.id);
      setComments(data);
    } catch (err) {
      console.error("Failed to load comments:", err);
    }
  }, [event.id]);

  useEffect(() => {
    loadComments();
    setShowRaw(false);
    setRawSegments([]);
    // earnings / forecast イベントのみセグメント取得
    const cat = getDisplayCategory(event.event_type, event.headline);
    if (cat === "earnings" || cat === "forecast") {
      setSegLoading(true);
      // raw_payload から period / quarter を取得（なければ null で最新を取得）
      const payload = event.raw_payload as Record<string, unknown>;
      const period = (payload?.period ?? payload?.fiscal_year_end ?? null) as string | null;
      const quarter = (payload?.quarter ?? null) as string | null;
      fetchSegmentFinancials(supabaseRef.current, event.ticker, period, quarter)
        .then((rows) => setRawSegments(rows))
        .catch(() => setRawSegments([]))
        .finally(() => setSegLoading(false));
    }
  }, [loadComments, event.id, event.ticker, event.event_type, event.headline, event.raw_payload]);

  const handleToggleRead = async () => {
    try {
      if (event.is_read) {
        await markAsUnread(supabaseRef.current, event.id, userId);
      } else {
        await markAsRead(supabaseRef.current, event.id, userId);
      }
      onUpdate({ ...event, is_read: !event.is_read });
    } catch (err) {
      console.error("Failed to toggle read:", err);
    }
  };

  const handleToggleStar = async () => {
    try {
      await toggleStar(supabaseRef.current, event.id, userId, event.is_starred);
      onUpdate({ ...event, is_starred: !event.is_starred });
    } catch (err) {
      console.error("Failed to toggle star:", err);
    }
  };

  const handleSubmitComment = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newComment.trim()) return;
    setSubmitting(true);
    try {
      await addComment(supabaseRef.current, event.id, userId, newComment.trim());
      setNewComment("");
      await loadComments();
      onUpdate({ ...event, comments_count: comments.length + 1 });
    } catch (err) {
      console.error("Failed to add comment:", err);
    } finally {
      setSubmitting(false);
    }
  };

  const displayCat = getDisplayCategory(event.event_type, event.headline);
  const badge = EVENT_TYPE_CONFIG[displayCat] || {
    label: "その他",
    emoji: "📄",
  };
  const subtypeLabel = event.event_subtype
    ? (EVENT_SUBTYPE_LABELS[event.event_subtype] ?? event.event_subtype)
    : "";

  const formatDateTime = (dt: string | null) => {
    if (!dt) return "—";
    const d = new Date(dt);
    return d.toLocaleString("ja-JP", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const formatCommentTime = (dt: string) => {
    const d = new Date(dt);
    const MM = String(d.getMonth() + 1).padStart(2, "0");
    const DD = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${MM}/${DD} ${hh}:${mm}`;
  };

  // formatted_message → display_title+display_summary フォールバック
  let mainMessage = event.formatted_message?.trim()
    ? event.formatted_message
    : [event.display_title, event.display_summary]
        .filter((s) => s?.trim())
        .join("\n")
      || event.headline
      || "";

  // 指標行なし(改行なし) → headline をメイン本文に統合
  const isShort = !mainMessage.includes("\n");
  if (
    isShort &&
    event.headline?.trim() &&
    !mainMessage.includes(event.headline.trim())
  ) {
    mainMessage = mainMessage + "\n" + event.headline;
  }

  return (
    <div className="detail-panel">
      {/* Main message: Discord と同一内容 */}
      <div className="detail-main-message">{mainMessage}</div>

      {/* Meta info */}
      <div className="detail-meta">
        <span className="detail-meta-label">種別</span>
        <span className="detail-meta-value">
          {badge.emoji} {badge.label}
          {subtypeLabel && ` (${subtypeLabel})`}
        </span>

        <span className="detail-meta-label">ティッカー</span>
        <span className="detail-meta-value" style={{ fontFamily: "var(--font-mono)" }}>
          {event.ticker}
        </span>

        <span className="detail-meta-label">会社名</span>
        <span className="detail-meta-value">{event.company_name}</span>

        <span className="detail-meta-label">検知日時</span>
        <span className="detail-meta-value">{formatDateTime(event.detected_at)}</span>

        <span className="detail-meta-label">開示日時</span>
        <span className="detail-meta-value">{formatDateTime(event.disclosed_at)}</span>

        <span className="detail-meta-label">優先度</span>
        <span className="detail-meta-value">Rank {event.priority_rank}</span>
      </div>

      {/* Action buttons */}
      <div className="detail-actions">
        <button
          className={`detail-action-btn ${event.is_read ? "" : "active"}`}
          onClick={handleToggleRead}
        >
          {event.is_read ? "📖 既読" : "📩 未読"}
          {" — "}
          {event.is_read ? "未読に戻す" : "既読にする"}
        </button>
        <button
          className={`detail-action-btn ${event.is_starred ? "active" : ""}`}
          onClick={handleToggleStar}
        >
          {event.is_starred ? "⭐ スター済" : "☆ スター"}
        </button>
      </div>

      {/* Source links */}
      {(event.source_url || event.pdf_url) && (
        <div className="detail-links">
          {event.source_url && (
            <a
              href={event.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="detail-link"
            >
              🔗 原文
            </a>
          )}
          {event.pdf_url && (
            <a
              href={event.pdf_url}
              target="_blank"
              rel="noopener noreferrer"
              className="detail-link"
            >
              📄 PDF
            </a>
          )}
        </div>
      )}

      {/* Segment financials */}
      {(() => {
        const cat = getDisplayCategory(event.event_type, event.headline);
        if (cat !== "earnings" && cat !== "forecast") return null;
        return (
          <div className="segment-section">
            <div className="segment-title">📊 セグメント業績</div>
            {segLoading ? (
              <div className="segment-empty">読み込み中...</div>
            ) : segmentViewData.length === 0 ? (
              <div className="segment-empty">セグメント業績なし</div>
            ) : (
              <table className="segment-table">
                <thead>
                  <tr>
                    <th>セグメント</th>
                    <th>売上高 (百万円)</th>
                    <th>営業利益 (百万円)</th>
                    <th>利益率</th>
                  </tr>
                </thead>
                <tbody>
                  {segmentViewData.map((row) => {
                    const margin =
                      row.segment_sales && row.segment_profit && row.segment_sales !== 0
                        ? ((row.segment_profit / row.segment_sales) * 100).toFixed(1) + "%"
                        : "—";
                    return (
                      <tr key={row.display_key}>
                        <td>{row.display_name}</td>
                        <td>{row.segment_sales != null ? row.segment_sales.toLocaleString() : "—"}</td>
                        <td>{row.segment_profit != null ? row.segment_profit.toLocaleString() : "—"}</td>
                        <td>{margin}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        );
      })()}

      {/* Comments */}
      <div className="comments-section">
        <div className="comments-title">💬 コメント ({comments.length})</div>
        {comments.map((c) => (
          <div key={c.id} className="comment-item">
            <div className="comment-header">
              <span className="comment-user">{c.user_id.slice(0, 8)}</span>
              <span className="comment-time">
                {formatCommentTime(c.created_at)}
              </span>
            </div>
            <div className="comment-text">{c.comment}</div>
          </div>
        ))}
        <form className="comment-form" onSubmit={handleSubmitComment}>
          <input
            type="text"
            className="comment-input"
            placeholder="コメントを入力..."
            value={newComment}
            onChange={(e) => setNewComment(e.target.value)}
          />
          <button
            type="submit"
            className="comment-submit"
            disabled={submitting || !newComment.trim()}
          >
            送信
          </button>
        </form>
      </div>

      {/* Raw Payload toggle */}
      <button
        className="raw-toggle"
        onClick={() => setShowRaw(!showRaw)}
      >
        {showRaw ? "▼ Raw payload を隠す" : "▶ Raw payload を表示"}
      </button>
      {showRaw && (
        <pre className="raw-payload">
          {JSON.stringify(event.raw_payload, null, 2)}
        </pre>
      )}
    </div>
  );
}
