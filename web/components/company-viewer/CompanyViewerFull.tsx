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
  loadEdinetOrders,
  type CompanyInfo,
  type EdinetOrderRecord,
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
      .from("canonical_segments")
      .select(
        "id,ticker,period,quarter,segment_name,segment_key,metric,value,source,source_priority,data_basis,source_disclosure_date,source_doc_id,flags"
      )
      .eq("ticker", t)
      .order("period", { ascending: false })
      .order("quarter", { ascending: false })
      .limit(2000);

    if (error) {
      console.warn("[CompanyViewerFull] segments:", error.message);
      return [];
    }

    const rows = (data || []);
    
    // Group by period + quarter + segment_key + metric to pick highest priority row
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const grouped = new Map<string, any[]>();
    for (const r of rows) {
      const key = `${r.period}_${r.quarter}_${r.segment_key || r.segment_name}_${r.metric}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key)!.push(r);
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const selectedRows: any[] = [];
    for (const group of grouped.values()) {
      if (group.length === 1) {
        selectedRows.push(group[0]);
        continue;
      }
      
      // Sort to find the winner
      group.sort((a, b) => {
        // 1. data_basis = prior_comparative 優先
        const aPrior = a.data_basis === 'prior_comparative' ? 1 : 0;
        const bPrior = b.data_basis === 'prior_comparative' ? 1 : 0;
        if (aPrior !== bPrior) return bPrior - aPrior;
        
        // 2. source_disclosure_date 降順
        const aDate = a.source_disclosure_date || "";
        const bDate = b.source_disclosure_date || "";
        if (aDate !== bDate) return bDate.localeCompare(aDate);
        
        // 3. data_basis = official_current (or null)
        const aOff = (a.data_basis === 'official_current' || !a.data_basis) ? 1 : 0;
        const bOff = (b.data_basis === 'official_current' || !b.data_basis) ? 1 : 0;
        if (aOff !== bOff) return bOff - aOff;
        
        // 4. source_priority (小さい方が優先)
        const aPrio = a.source_priority ?? 999;
        const bPrio = b.source_priority ?? 999;
        return aPrio - bPrio;
      });
      selectedRows.push(group[0]);
    }

    // Now pivot the selected rows back to SegmentRecord (sales & profit combined)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const segmentMap = new Map<string, any>();
    for (const r of selectedRows) {
      const key = `${r.period}_${r.quarter}_${r.segment_key || r.segment_name}`;
      if (!segmentMap.has(key)) {
        segmentMap.set(key, {
          ticker: r.ticker,
          period: r.period || "",
          quarter: r.quarter || "",
          segment_name: r.segment_name || "",
          segment_sales: null,
          segment_profit: null,
          source: r.source,
          source_priority: r.source_priority,
        });
      }
      const seg = segmentMap.get(key);
      if (r.metric === "sales") seg.segment_sales = r.value;
      if (r.metric === "profit") seg.segment_profit = r.value;
      seg.source = r.source;
    }

    return Array.from(segmentMap.values()) as SegmentRecord[];
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
  const [edinetOrders, setEdinetOrders] = useState<EdinetOrderRecord[]>([]);
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
    setEdinetOrders([]);

    (async () => {
      const [infoR, plR, segR, ordersR] = await Promise.allSettled([
        loadCompanyInfo(supabase, ticker),
        loadFinancials(supabase, ticker),
        loadSegments(supabase, ticker),
        loadEdinetOrders(supabase, ticker),
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
      if (ordersR.status === "fulfilled") setEdinetOrders(ordersR.value);

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

      {/* ── EDINET Orders 本体 ── */}
      {!loading && edinetOrders.length > 0 && (
        <div className="cvf-edinet-orders" style={{ marginBottom: "20px", padding: "10px", backgroundColor: "var(--bg-card, #1e293b)", borderRadius: "6px", border: "1px solid var(--border-color, #334155)" }}>
          <h3 style={{ fontSize: "14px", fontWeight: "bold", margin: "0 0 10px 0", color: "var(--text-primary, #e2e8f0)" }}>EDINET受注KPI (受注高・受注残推移)</h3>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px", textAlign: "left", color: "var(--text-secondary, #cbd5e1)" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-color, #475569)" }}>
                <th style={{ padding: "4px 8px", fontWeight: "normal" }}>期末日</th>
                <th style={{ padding: "4px 8px", fontWeight: "normal" }}>受注高</th>
                <th style={{ padding: "4px 8px", fontWeight: "normal" }}>受注残</th>
                <th style={{ padding: "4px 8px", fontWeight: "normal" }}>doc_id</th>
              </tr>
            </thead>
            <tbody>
              {edinetOrders.map((r, i) => (
                <tr key={i} style={{ borderBottom: "1px solid var(--border-light, #334155)" }}>
                  <td style={{ padding: "4px 8px" }}>{r.period}</td>
                  <td style={{ padding: "4px 8px" }}>{r.orders_received != null ? r.orders_received.toLocaleString() + "百万円" : "未開示"}</td>
                  <td style={{ padding: "4px 8px" }}>{r.order_backlog != null ? r.order_backlog.toLocaleString() + "百万円" : "未開示"}</td>
                  <td style={{ padding: "4px 8px", opacity: 0.7 }} title={`source_unit: ${r.source_unit || "不明"}`}>{r.doc_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
