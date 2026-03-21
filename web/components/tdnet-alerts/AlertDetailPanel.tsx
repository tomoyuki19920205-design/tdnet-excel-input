"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { createClient } from "@/lib/supabase/client";
import {
  markAsRead,
  markAsUnread,
  toggleStar,
  fetchComments,
  addComment,
} from "@/lib/tdnet-alerts/queries";
import type { EnrichedEvent, TdnetEventComment } from "@/lib/tdnet-alerts/types";
import { EVENT_TYPE_CONFIG, EVENT_SUBTYPE_LABELS, getDisplayCategory } from "@/lib/tdnet-alerts/types";

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
  const supabaseRef = useRef(createClient());

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
  }, [loadComments]);

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
  // DEBUG: 詳細パネルデバッグ (修正確認後に削除)
  console.log("DETAIL_PANEL:", {
    id: event.id?.slice(0, 8),
    event_type: event.event_type,
    normalized: displayCat,
    headline: event.headline?.slice(0, 40),
  });
  const badge = EVENT_TYPE_CONFIG[displayCat] || {
    label: "その他",
    emoji: "📄",
  };
  const subtypeLabel = event.event_subtype
    ? EVENT_SUBTYPE_LABELS[event.event_subtype] || event.event_subtype
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

  return (
    <div className="detail-panel">
      {/* Title */}
      <h2 className="detail-title">{event.display_title || event.headline}</h2>

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

      {/* Primary Metric */}
      {event.primary_metric_value && (
        <div className="detail-metric">
          <div>
            <div className="detail-metric-name">
              {event.primary_metric_name || "指標"}
            </div>
            <div className="detail-metric-value">
              {event.primary_metric_value}
            </div>
          </div>
          {event.primary_metric_yoy && (
            <span
              className={`detail-metric-yoy ${
                event.primary_metric_yoy.startsWith("+")
                  ? "positive"
                  : "negative"
              }`}
            >
              {event.primary_metric_yoy}
            </span>
          )}
        </div>
      )}

      {/* Summary */}
      {event.display_summary && (
        <div className="detail-summary">{event.display_summary}</div>
      )}

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
