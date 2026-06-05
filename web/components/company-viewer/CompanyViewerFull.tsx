"use client";

/**
 * web/components/company-viewer/CompanyViewerFull.tsx
 *
 * company-memo-app の FinancialsTable を TDNET Alerts 右ペインに統合する
 * ラッパーコンポーネント。
 *
 * Props:
 *   - ticker: string          選択中ティッカー（AlertsPage から受け取る）
 *   - supabase: SupabaseClient 認証済みクライアント（AlertsPage から受け取る）
 *   - companyViewerBaseUrl?: string  別タブボタン用 base URL（省略可）
 *
 * 機能:
 *   - ticker が変わると自動でデータを再フェッチ
 *   - FinancialsTable（累計PL + Q単体 + セグメント表）を表示
 *   - メモ保存・KPI編集は右ペインでは無効（read-only 表示のみ）
 *   - 「Company Viewer を別タブで開く」リンクを上部に表示
 */

import React, { useEffect, useState, useRef } from "react";
import type { SupabaseClient } from "@supabase/supabase-js";
import FinancialsTable from "@/components/company-viewer/FinancialsTable";
import {
  loadCompanyInfo,
  loadFinancials,
  type CompanyInfo,
} from "@/lib/viewer-api";
import type { FinancialRecord } from "@/types/financial";
import type { SegmentRecord } from "@/types/segment";

// ============================================================
// Props
// ============================================================
interface Props {
  ticker: string;
  supabase: SupabaseClient;
  companyViewerBaseUrl?: string;
}

// ============================================================
// セグメントデータローダ（api_latest_segments）
// ============================================================
async function loadSegments(
  supabase: SupabaseClient,
  ticker: string,
): Promise<SegmentRecord[]> {
  try {
    // ticker 正規化（4桁ゼロパディング）
    const t = ticker.trim().match(/^\d+$/)
      ? ticker.trim().padStart(4, "0")
      : ticker.trim().toUpperCase();

    const { data, error } = await supabase
      .from("api_latest_segments")
      .select(
        "ticker,period,quarter,segment_name,segment_sales,segment_profit,source,source_priority",
      )
      .eq("ticker", t)
      .order("period", { ascending: false })
      .order("quarter", { ascending: false })
      .limit(200);

    if (error) {
      console.warn("[CompanyViewerFull] segments:", error.message);
      return [];
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return ((data as any[]) ?? []).map((r) => ({
      ticker: r.ticker,
      period: r.period ?? "",
      quarter: r.quarter ?? "",
      segment_name: r.segment_name ?? "",
      segment_sales: r.segment_sales ?? null,
      segment_profit: r.segment_profit ?? null,
      source: r.source ?? undefined,
      source_priority: r.source_priority ?? null,
    })) as SegmentRecord[];
  } catch (e) {
    console.warn("[CompanyViewerFull] segments exception:", e);
    return [];
  }
}

// ============================================================
// Component
// ============================================================
export default function CompanyViewerFull({
  ticker,
  supabase,
  companyViewerBaseUrl,
}: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<CompanyInfo | null>(null);
  const [financials, setFinancials] = useState<FinancialRecord[]>([]);
  const [segments, setSegments] = useState<SegmentRecord[]>([]);
  const currentTickerRef = useRef<string>("");

  // ticker が変わったらデータ再フェッチ
  useEffect(() => {
    if (!ticker) return;
    currentTickerRef.current = ticker;
    setLoading(true);
    setError(null);
    setInfo(null);
    setFinancials([]);
    setSegments([]);

    (async () => {
      const [infoR, plR, segR] = await Promise.allSettled([
        loadCompanyInfo(supabase, ticker),
        loadFinancials(supabase, ticker),
        loadSegments(supabase, ticker),
      ]);

      // ticker が変わっていたら結果を捨てる
      if (currentTickerRef.current !== ticker) return;

      if (infoR.status === "fulfilled") setInfo(infoR.value);

      if (plR.status === "fulfilled") {
        const { data, error: plErr } = plR.value;
        if (plErr) {
          setError(plErr);
        } else {
          setFinancials(data);
        }
      } else {
        setError(plR.reason?.message ?? "データ取得に失敗しました");
      }

      if (segR.status === "fulfilled") setSegments(segR.value);

      setLoading(false);
    })();
  }, [ticker, supabase]);

  const cvUrl = companyViewerBaseUrl
    ? `${companyViewerBaseUrl}?ticker=${encodeURIComponent(ticker)}`
    : null;

  return (
    <div className="cvf-root">
      {/* ── ヘッダーバー ── */}
      <div className="cvf-header">
        <div className="cvf-ticker-info">
          <span className="cvf-ticker">{ticker}</span>
          {info?.companyName && (
            <span className="cvf-company-name">{info.companyName}</span>
          )}
        </div>
        {cvUrl && (
          <a
            href={cvUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="cvf-open-btn"
            title="Company Viewerを別タブで開く"
          >
            🔗 別タブ
          </a>
        )}
      </div>

      {/* ── エラー表示 ── */}
      {!loading && error && (
        <div className="cvf-error">
          <span className="cvf-error-title">⚠️ データ取得エラー</span>
          <span className="cvf-error-msg">{error}</span>
        </div>
      )}

      {/* ── FinancialsTable 本体 ── */}
      <div className="cvf-body">
        <FinancialsTable
          data={financials}
          loading={loading}
          segments={segments}
          /* read-only: メモ・KPI・セグメント編集コールバックは渡さない */
        />
      </div>
    </div>
  );
}
