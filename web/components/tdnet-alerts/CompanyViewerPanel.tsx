"use client";

import React, { useEffect, useState, useRef, useMemo } from "react";
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
  companyViewerBaseUrl?: string;
}

// ============================================================
// 内部型
// ============================================================
interface PLRow {
  period: string;
  periodLabel: string;
  quarter: string;        // "1Q" | "2Q" | "3Q" | "4Q" | "FY"
  isFY: boolean;
  isFirstInPeriod: boolean;
  sales: number | null;
  opProfit: number | null;
  opMargin: number | null; // %
  yoySales: number | null; // %
  yoyOp: number | null;    // %
  qnqSales: number | null; // %（FY行は null）
  qnqOp: number | null;    // %（FY行は null）
}

// ============================================================
// フォーマットヘルパー
// ============================================================
function fmt億(v: number | null): string {
  if (v === null) return "—";
  const oku = v / 100; // 百万円 → 億円
  const abs = Math.abs(oku);
  if (abs >= 10000) return `${(oku / 10000).toFixed(1)}兆`;
  if (abs >= 1000)  return `${oku.toFixed(0)}億`;
  if (abs >= 10)   return `${oku.toFixed(1)}億`;
  return `${oku.toFixed(2)}億`;
}

function fmtPct(v: number | null): string {
  if (v === null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function fmtMargin(v: number | null): string {
  if (v === null) return "—";
  return `${v.toFixed(1)}%`;
}

function pctClass(v: number | null): string {
  if (v === null) return "";
  return v >= 0 ? "cvp-pos" : "cvp-neg";
}

function periodLabel(period: string): string {
  const m = period.match(/^(\d{4})-(\d{2})/);
  if (!m) return period;
  return `${m[1].slice(2)}/${parseInt(m[2])}期`;
}

function growthRate(curr: number | null, base: number | null): number | null {
  if (curr === null || base === null || base === 0) return null;
  return ((curr - base) / Math.abs(base)) * 100;
}

// ============================================================
// PL 計算ロジック
// ============================================================
function computePLRows(financials: FinancialRecord[]): PLRow[] {
  if (financials.length === 0) return [];

  // lookup: "period|quarter" -> record
  const lookup = new Map<string, FinancialRecord>();
  for (const r of financials) {
    lookup.set(`${r.period}|${r.quarter}`, r);
  }

  // ユニーク期 (DESC)
  const periods = [...new Set(financials.map(r => r.period))].sort().reverse();

  // 累計→前四半期のマッピング
  const PREV_CUM: Record<string, string> = { "2Q": "1Q", "3Q": "2Q", "FY": "3Q" };

  // 四半期単独値を計算
  function getSA(
    period: string, q: string
  ): { s: number | null; op: number | null } {
    const curr = lookup.get(`${period}|${q}`);
    if (!curr) return { s: null, op: null };
    const prevQ = PREV_CUM[q];
    if (!prevQ) return { s: curr.sales, op: curr.operating_profit }; // 1Q
    const prev = lookup.get(`${period}|${prevQ}`);
    if (!prev) return { s: curr.sales, op: curr.operating_profit }; // 前Qデータなし
    return {
      s:  curr.sales               !== null && prev.sales               !== null
            ? curr.sales - prev.sales : null,
      op: curr.operating_profit    !== null && prev.operating_profit    !== null
            ? curr.operating_profit - prev.operating_profit : null,
    };
  }

  // ============================================================
  // QnQ 計算用: 時系列順 (ASC) の単独四半期リスト
  // ============================================================
  type SR = { period: string; quarter: string; s: number | null; op: number | null };
  const standaloneAsc: SR[] = [];

  for (const period of [...periods].reverse()) { // 古い順
    for (const q of ["1Q", "2Q", "3Q", "4Q"] as const) {
      const srcQ = q === "4Q" ? "FY" : q;
      if (!lookup.has(`${period}|${srcQ}`)) continue;
      if (q === "4Q" && !lookup.has(`${period}|3Q`)) continue; // 3Q ないと 4Q SA 不定
      const { s, op } = getSA(period, srcQ);
      standaloneAsc.push({ period, quarter: q, s, op });
    }
  }

  // QnQ マップ: "period|quarter" -> 直前の SA 行
  const qnqPrevMap = new Map<string, SR>();
  for (let i = 1; i < standaloneAsc.length; i++) {
    const c = standaloneAsc[i];
    qnqPrevMap.set(`${c.period}|${c.quarter}`, standaloneAsc[i - 1]);
  }

  // 前年期 (DESC 配列の次インデックス)
  const pidx = new Map(periods.map((p, i) => [p, i]));
  const prevYr = (p: string) => periods[(pidx.get(p) ?? 0) + 1] ?? null;

  // ============================================================
  // 表示行 生成 (期 DESC, Q 内は 4Q→3Q→2Q→1Q→FY)
  // ============================================================
  const raw: Omit<PLRow, "isFirstInPeriod">[] = [];

  for (const period of periods) {
    const py = prevYr(period);

    // --- 単独四半期 (4Q, 3Q, 2Q, 1Q) ---
    for (const q of ["4Q", "3Q", "2Q", "1Q"] as const) {
      const srcQ = q === "4Q" ? "FY" : q;
      if (!lookup.has(`${period}|${srcQ}`)) continue;
      if (q === "4Q" && !lookup.has(`${period}|3Q`)) continue;
      const { s, op } = getSA(period, srcQ);

      // YoY: 前年同四半期の SA
      const pyRow = py
        ? standaloneAsc.find(r => r.period === py && r.quarter === q)
        : null;

      // QnQ: 直前の SA 行
      const qnqRow = qnqPrevMap.get(`${period}|${q}`);

      raw.push({
        period,
        periodLabel: periodLabel(period),
        quarter: q,
        isFY: false,
        sales:    s,
        opProfit: op,
        opMargin: op !== null && s !== null && s !== 0 ? (op / s) * 100 : null,
        yoySales: growthRate(s,  pyRow?.s  ?? null),
        yoyOp:    growthRate(op, pyRow?.op ?? null),
        qnqSales: growthRate(s,  qnqRow?.s  ?? null),
        qnqOp:    growthRate(op, qnqRow?.op ?? null),
      });
    }

    // --- FY 通期行 ---
    const fyRec = lookup.get(`${period}|FY`);
    if (fyRec) {
      const pyFy = py ? lookup.get(`${py}|FY`) : null;
      raw.push({
        period,
        periodLabel: periodLabel(period),
        quarter: "FY",
        isFY: true,
        sales:    fyRec.sales,
        opProfit: fyRec.operating_profit,
        opMargin: fyRec.operating_profit !== null && fyRec.sales !== null && fyRec.sales !== 0
          ? (fyRec.operating_profit / fyRec.sales) * 100 : null,
        yoySales: growthRate(fyRec.sales, pyFy?.sales ?? null),
        yoyOp:    growthRate(fyRec.operating_profit, pyFy?.operating_profit ?? null),
        qnqSales: null, // FY 行は QnQ なし
        qnqOp:    null,
      });
    }
  }

  // isFirstInPeriod フラグ付与
  let lastPeriod = "";
  return raw.map(row => {
    const isFirst = row.period !== lastPeriod;
    if (isFirst) lastPeriod = row.period;
    return { ...row, isFirstInPeriod: isFirst };
  });
}

// ============================================================
// Component
// ============================================================
export default function CompanyViewerPanel({
  ticker,
  supabase,
  companyViewerBaseUrl,
}: Props) {
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);
  const [info,    setInfo]    = useState<CompanyInfo | null>(null);
  const [financials, setFinancials] = useState<FinancialRecord[]>([]);
  const [forecasts,  setForecasts]  = useState<ForecastRevision[]>([]);
  const currentRef = useRef<string>("");

  useEffect(() => {
    if (!ticker) return;
    currentRef.current = ticker;
    setLoading(true);
    setError(null);
    setInfo(null);
    setFinancials([]);
    setForecasts([]);

    (async () => {
      const [infoR, plR, fcR] = await Promise.allSettled([
        loadCompanyInfo(supabase, ticker),
        loadFinancials(supabase, ticker),
        loadForecastRevision(supabase, ticker),
      ]);
      if (currentRef.current !== ticker) return;

      if (infoR.status === "fulfilled") setInfo(infoR.value);

      if (plR.status === "fulfilled") {
        const { data, error: plErr } = plR.value;
        if (plErr) setError(plErr);
        else setFinancials(data);
      } else {
        setError(plR.reason?.message ?? "PL取得に失敗しました");
      }

      if (fcR.status === "fulfilled") setForecasts(fcR.value);
      setLoading(false);
    })();
  }, [ticker, supabase]);

  const plRows = useMemo(() => computePLRows(financials), [financials]);

  // 直近12四半期 + FY行
  const displayRows = plRows.slice(0, 24);

  const cvUrl = companyViewerBaseUrl
    ? `${companyViewerBaseUrl}?ticker=${encodeURIComponent(ticker)}`
    : null;

  return (
    <div className="cvp-root">
      {/* ヘッダー */}
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
            RLSエラー (42501) またはテーブル未アクセス権限の可能性があります。
          </div>
        </div>
      )}

      {/* PL テーブル */}
      {!loading && !error && (
        <div className="cvp-table-wrap">
          {displayRows.length === 0 ? (
            <div className="cvp-empty">PLデータなし（ticker: {ticker}）</div>
          ) : (
            <table className="cvp-pl-table">
              <thead>
                <tr>
                  <th className="cvp-th-left">期</th>
                  <th className="cvp-th-left">Q</th>
                  <th>売上高</th>
                  <th>YoY売</th>
                  <th>QnQ売</th>
                  <th>営業利益</th>
                  <th>利益率</th>
                  <th>YoY営</th>
                  <th>QnQ営</th>
                </tr>
              </thead>
              <tbody>
                {displayRows.map((row, i) => (
                  <tr
                    key={i}
                    className={[
                      row.isFY ? "cvp-row-fy" : "cvp-row-q",
                      row.isFirstInPeriod && !row.isFY ? "cvp-first-in-period" : "",
                    ].join(" ")}
                  >
                    {/* 期ラベル: 期の最初の行（4Q か最上位Q）にのみ表示 */}
                    <td className="cvp-td-period">
                      {row.isFirstInPeriod ? row.periodLabel : ""}
                    </td>
                    <td className="cvp-td-q">
                      {row.isFY ? "通期" : row.quarter}
                    </td>
                    <td className="cvp-td-num">{fmt億(row.sales)}</td>
                    <td className={`cvp-td-pct ${pctClass(row.yoySales)}`}>
                      {fmtPct(row.yoySales)}
                    </td>
                    <td className={`cvp-td-pct ${pctClass(row.qnqSales)}`}>
                      {fmtPct(row.qnqSales)}
                    </td>
                    <td className="cvp-td-num">{fmt億(row.opProfit)}</td>
                    <td className="cvp-td-pct">{fmtMargin(row.opMargin)}</td>
                    <td className={`cvp-td-pct ${pctClass(row.yoyOp)}`}>
                      {fmtPct(row.yoyOp)}
                    </td>
                    <td className={`cvp-td-pct ${pctClass(row.qnqOp)}`}>
                      {fmtPct(row.qnqOp)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* 業績修正 */}
      {!loading && !error && forecasts.length > 0 && (
        <>
          <div className="cvp-section-title">📝 業績修正 ({forecasts.length}件)</div>
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
                      fc.delta_pct >= 0 ? "cvp-pos" : "cvp-neg"
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
    </div>
  );
}
