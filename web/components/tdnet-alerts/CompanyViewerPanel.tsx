"use client";

import React, { useEffect, useState, useRef } from "react";
import type { SupabaseClient } from "@supabase/supabase-js";
import {
  loadCompanyInfo,
  loadFinancials,
  loadForecastRevision,
  type CompanyInfo,
} from "@/lib/viewer-api";
import type { FinancialRecord } from "@/types/financial";
import type { ForecastRevision } from "@/types/forecast";

// ============================================================
// Props
// ============================================================
interface Props {
  ticker: string;
  supabase: SupabaseClient;
  companyViewerBaseUrl?: string; // 別タブで開く用
}

// ============================================================
// ヘルパー
// ============================================================
function fmtMillions(v: number | null): string {
  if (v === null) return "—";
  // 百万円単位 → 億円に変換して表示
  const oku = v / 100;
  if (Math.abs(oku) >= 100) return `${oku.toFixed(0)}億`;
  return `${oku.toFixed(1)}億`;
}

function fmtPeriod(period: string, quarter: string): string {
  // "2025-03-31" → "25/3期" + "FY" → "通期"
  const yearMatch = period.match(/^(\d{4})-(\d{2})/);
  if (!yearMatch) return `${period}/${quarter}`;
  const year = yearMatch[1].slice(2); // "25"
  const month = parseInt(yearMatch[2], 10); // 3
  const q = quarter === "FY" ? "通期" : quarter;
  return `${year}/${month}期 ${q}`;
}

// FY のみ表示 (一覧を絞る)
const MAX_ROWS = 6;

// ============================================================
// Component
// ============================================================
export default function CompanyViewerPanel({
  ticker,
  supabase,
  companyViewerBaseUrl,
}: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<CompanyInfo | null>(null);
  const [financials, setFinancials] = useState<FinancialRecord[]>([]);
  const [forecasts, setForecasts] = useState<ForecastRevision[]>([]);
  const currentTickerRef = useRef<string>("");

  useEffect(() => {
    if (!ticker) return;
    currentTickerRef.current = ticker;
    setLoading(true);
    setError(null);
    setInfo(null);
    setFinancials([]);
    setForecasts([]);

    (async () => {
      const [infoResult, plResult, fcResult] = await Promise.allSettled([
        loadCompanyInfo(supabase, ticker),
        loadFinancials(supabase, ticker),
        loadForecastRevision(supabase, ticker),
      ]);

      // ticker が切り替わっていたら古い結果は捨てる
      if (currentTickerRef.current !== ticker) return;

      if (infoResult.status === "fulfilled") setInfo(infoResult.value);

      if (plResult.status === "fulfilled") {
        const { data, error: plErr } = plResult.value;
        if (plErr) {
          setError(plErr);
        } else {
          setFinancials(data);
        }
      } else {
        setError(plResult.reason?.message ?? "PL取得に失敗しました");
      }

      if (fcResult.status === "fulfilled") setForecasts(fcResult.value);

      setLoading(false);
    })();
  }, [ticker, supabase]);

  // 別タブURL
  const cvUrl = companyViewerBaseUrl
    ? `${companyViewerBaseUrl}?ticker=${encodeURIComponent(ticker)}`
    : null;

  // 表示用データ: FY + 直近クォーターを最大 MAX_ROWS 件
  const displayRows = financials.slice(0, MAX_ROWS);

  return (
    <div className="cvp-root">
      {/* ヘッダー行 */}
      <div className="cvp-header">
        <div className="cvp-ticker-info">
          <span className="cvp-ticker">{ticker}</span>
          {info?.companyName && (
            <span className="cvp-company-name">{info.companyName}</span>
          )}
        </div>
        {cvUrl && (
          <a
            href={cvUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="cvp-open-btn"
            title="Company Viewerを別タブで開く"
          >
            🔗 別タブ
          </a>
        )}
      </div>

      {/* ローディング */}
      {loading && (
        <div className="cvp-loading">
          <span className="cvp-spinner" />
          データを読み込み中...
        </div>
      )}

      {/* エラー */}
      {!loading && error && (
        <div className="cvp-error">
          <div className="cvp-error-title">⚠️ データ取得エラー</div>
          <div className="cvp-error-msg">{error}</div>
          <div className="cvp-error-hint">
            RLSエラー (42501) または テーブル未アクセス権限の可能性があります。
          </div>
        </div>
      )}

      {/* PL テーブル */}
      {!loading && !error && (
        <>
          <div className="cvp-section-title">📊 PL 要約（百万円単位）</div>
          {displayRows.length === 0 ? (
            <div className="cvp-empty">PLデータなし</div>
          ) : (
            <table className="cvp-table">
              <thead>
                <tr>
                  <th>期</th>
                  <th>売上高</th>
                  <th>営業利益</th>
                </tr>
              </thead>
              <tbody>
                {displayRows.map((row, i) => (
                  <tr key={i}>
                    <td className="cvp-td-period">
                      {fmtPeriod(row.period, row.quarter)}
                    </td>
                    <td className="cvp-td-num">{fmtMillions(row.sales)}</td>
                    <td
                      className={`cvp-td-num ${
                        row.operating_profit !== null && row.operating_profit < 0
                          ? "cvp-negative"
                          : ""
                      }`}
                    >
                      {fmtMillions(row.operating_profit)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* 業績修正 */}
          {forecasts.length > 0 && (
            <>
              <div className="cvp-section-title">
                📝 業績修正 ({forecasts.length}件)
              </div>
              <div className="cvp-forecast-list">
                {forecasts.slice(0, 3).map((fc, i) => (
                  <div key={i} className="cvp-forecast-row">
                    <span className="cvp-fc-date">
                      {fc.pubdate?.slice(0, 10) ?? "—"}
                    </span>
                    <span className="cvp-fc-metric">
                      {fc.metric_name ?? fc.title ?? "—"}
                    </span>
                    {fc.delta_pct !== null && (
                      <span
                        className={`cvp-fc-delta ${
                          fc.delta_pct >= 0 ? "cvp-positive" : "cvp-negative"
                        }`}
                      >
                        {fc.delta_pct >= 0 ? "+" : ""}
                        {fc.delta_pct.toFixed(1)}%
                      </span>
                    )}
                  </div>
                ))}
                {forecasts.length > 3 && (
                  <div className="cvp-fc-more">他 {forecasts.length - 3}件</div>
                )}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
