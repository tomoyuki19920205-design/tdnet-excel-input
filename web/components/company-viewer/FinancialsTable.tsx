"use client";

import React, { useMemo, useCallback, useRef, useState, useEffect } from "react";
import type { FinancialRecord } from "@/types/financial";
import type { SegmentRecord } from "@/types/segment";
import { formatMillions, displayValue } from "@/lib/company-viewer/format";
import { useColumnResize, type ColumnDef } from "@/components/company-viewer/ResizableTable";
import type { GridData } from "@/lib/company-viewer/memo-api";
import type { ManualTableType } from "@/lib/company-viewer/memo-api";
import { parseTsvClipboard } from "@/lib/company-viewer/tsv-parser";
import type { SegmentOverrideSaveRequest } from "@/types/segment-override";
import { normalizePeriod, normalizeQuarter } from "@/lib/company-viewer/normalize";
import { extractFiscalYear } from "@/lib/viewer-api";
import { normalizeSegmentDisplayKey, pickSegmentDisplayName, normalizeSegmentAliasKey, normalizeSegmentSemanticKey } from "@/lib/company-viewer/segment-normalize";
import type { KpiDefMap, KpiValueMap } from "@/lib/company-viewer/kpi-api";
import {
    filterLast5Years,
    buildCumulativeRows,
    buildQStandaloneRows,
    sortForDisplay,
    FORECAST_SOURCES,
} from "@/lib/company-viewer/quarter-math";

/** 何もしない安定参照関数。onSelect など毎レンダー生成を避けるために使用 */
const NOOP = () => {};

// ─── セグメント source 別タブ ─────────────────────────────
// 'tdnet' = backfill_v4_pdf / v4_pdf (XBRL partial fallback採用済み)
//           / backfill_xbrl / xbrl / attachment_xbrl
// 'edinet' = edinet_xbrl
// 'all'   = 上記すべて（whitelist source のみ。sourceなし・ゴみデータは除外）
const TDNET_SOURCES  = new Set([
    "backfill_v4_pdf",   // XBRL partial fallback 採用済み PDF V4 (priority=0)
    "v4_pdf",            // 同上 (短縮エイリアス)
    "backfill_xbrl",
    "xbrl",
    "attachment_xbrl",
]);
const EDINET_SOURCES = new Set(["edinet_xbrl"]);
type SegSourceTab = "tdnet" | "edinet" | "all" | "memo";

interface MemoMap {
    [key: string]: GridData;
}

/** アクティブセル座標 */
interface CellCoord {
    tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual";
    rowIdx: number;
    colKey: string; // "memo_a" | "memo_b" | "kpi_1" | "kpi_2" | "kpi_3" | "col_0"..."col_7"
}

/** セグメントセルのアクティブ座標 */
interface SegCellCoord {
    rowIdx: number;   // cumRows 上の行インデックス
    colIdx: number;   // セグメント列インデックス (scIdx * 2 + 0=sales/1=profit)
}

/** 範囲選択 */
interface SelectionRange {
    tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual";
    startRow: number;
    startColIdx: number;  // 絶対列インデックス (0-based)
    endRow: number;
    endColIdx: number;
}

// CUM: [period, quarter, sales, gp, gm_rate, sga, op, margin, memo_a, memo_b, ...kpis]
// Q:   [period, quarter, sales, gp, gm_rate, sga, op, margin, ...kpis]
const CUM_BASE_COL_COUNT = 8;
const Q_BASE_COL_COUNT = 8;

// 編集可能列の共通定数
const MEMO_COLS = ["memo_a", "memo_b"] as const;
const KPI_COLS = ["kpi_1", "kpi_2", "kpi_3"] as const;
const EDITABLE_COLS: string[] = [...MEMO_COLS, ...KPI_COLS];

interface FinancialsTableProps {
    data: FinancialRecord[];
    loading: boolean;
    selectedPeriod?: string;
    selectedQuarter?: string;
    onRowClick?: (period: string, quarter: string) => void;
    memoMap?: MemoMap;
    onMemoEdit?: (period: string, quarter: string, colIdx: number, value: string) => void;
    onMemoPaste?: (
        rows: { period: string; quarter: string; colIdx: number; value: string }[]
    ) => void;
    segments?: SegmentRecord[];
    kpiDefs?: KpiDefMap;
    kpiValues?: KpiValueMap;
    onKpiHeaderEdit?: (kpiSlot: number, newName: string) => void;
    onKpiValueEdit?: (period: string, quarter: string, kpiSlot: number, value: string, tableScope?: "cum" | "q") => void;
    /** Segment override: save a manual value for a segment cell */
    onSegmentOverrideSave?: (
        fiscalYear: number,
        quarter: string,
        segmentName: string,
        metric: string,
        value: number,
    ) => Promise<void>;
    /** Segment override: delete a manual value */
    onSegmentOverrideDelete?: (
        fiscalYear: number,
        quarter: string,
        segmentName: string,
        metric: string,
    ) => Promise<void>;
    /** Segment override: bulk save multiple cells */
    onBulkSaveOverrides?: (
        items: SegmentOverrideSaveRequest[],
    ) => Promise<{ saved: number; failed: number }>;
    /** Undo (Ctrl+Z) */
    onUndo?: () => void;
    /** 手入力メモ専用行データ */
    manualTableMemos?: {
        pl_cum: string[][];
        pl_q: string[][];
        segment_cum: string[][];
        segment_q: string[][];
        segment_manual: string[][];
    };
    /** 手入力メモ専用行 編集コールバック */
    onManualMemoEdit?: (
        tableType: ManualTableType,
        rowIdx: number,
        colIdx: number,
        value: string,
    ) => void;
    /** 手入力メモペースト用バッチ保存コールバック (グリッド全体を一度に渡す) */
    onManualMemoGridUpdate?: (
        tableType: ManualTableType,
        newGrid: string[][],
    ) => void;
    /** segment_manual の列ヘッダー文字列配列 (列 0-11 対応、デフォルト "1"-"12") */
    segmentManualHeaders?: string[];
    /** segment_manual ヘッダー編集コールバック (colIdx: 0-based) */
    onSegmentManualHeaderEdit?: (colIdx: number, value: string) => void;
    /** 独自横スクロールバー用: pl-scroll-area DOM元素を渡すコールバック */
    onPlScrollAreaReady?: (el: HTMLDivElement | null) => void;
}

const KPI_SLOTS = [1, 2, 3] as const;

// ============================================================
// 基本列定義
// ============================================================
const CUM_COLUMNS: ColumnDef[] = [
    { key: "period", label: "PERIOD", initialWidth: 100 },
    { key: "quarter", label: "Q", initialWidth: 45 },
    { key: "sales", label: "SALES", initialWidth: 90, className: "num-col" },
    { key: "gp", label: "GP", initialWidth: 85, className: "num-col" },
    { key: "gm_rate", label: "粗利率", initialWidth: 75, className: "num-col gp-margin-col" },
    { key: "sga", label: "管理費", initialWidth: 85, className: "num-col" },
    { key: "op", label: "OP", initialWidth: 85, className: "num-col" },
    { key: "op_margin", label: "営業利益率", initialWidth: 75, className: "num-col op-margin-col" },
];

/** メモ欄・KPI欄テーブルのベース列定義 */
const MEMO_KPI_BASE_COLUMNS: ColumnDef[] = [
    { key: "period", label: "PERIOD", initialWidth: 100 },
    { key: "quarter", label: "Q", initialWidth: 45 },
    { key: "memo_a", label: "Memo A", initialWidth: 130 },
    { key: "memo_b", label: "Memo B", initialWidth: 130 },
];

const Q_BASE_COLUMNS: ColumnDef[] = [
    { key: "period", label: "PERIOD", initialWidth: 100 },
    { key: "quarter", label: "Q", initialWidth: 45 },
    { key: "sales", label: "SALES", initialWidth: 90, className: "num-col" },
    { key: "gp", label: "GP", initialWidth: 85, className: "num-col" },
    { key: "gm_rate", label: "粗利率", initialWidth: 75, className: "num-col" },
    { key: "sga", label: "管理費", initialWidth: 85, className: "num-col" },
    { key: "op", label: "OP", initialWidth: 85, className: "num-col" },
    { key: "op_margin", label: "営業利益率", initialWidth: 75, className: "num-col" },
];

// ============================================================
// フォーマッタ
// ============================================================
/** 手入力メモ専用行の行数（PL・セグメント累計/Q共通） */
const MANUAL_MEMO_ROW_COUNT = 4 as const;
/** segment_manual の入力列数（PERIOD/Q を除く入力可能列） */
const SEGMENT_MANUAL_COL_COUNT = 12 as const;

/** tableType ごとの行数を返す（segment_manual は計算不導—呼び出し元で cumRows.length を渡す） */
function getManualRowCount(tableType: ManualTableType): number {
    return MANUAL_MEMO_ROW_COUNT; // pl_cum / pl_q / segment_cum / segment_q 用
}

function fmtMargin(val: number | null): string {
    if (val === null || val === undefined) return "–";
    return `${val.toFixed(1)}%`;
}

function extractMemoValue(gridData: GridData | undefined, colIdx: number): string {
    if (!gridData || !gridData[0]) return "";
    const val = gridData[0]?.[colIdx];
    return val ?? "";
}

/** カンマ区切り数値を許容する数値パーサ。不正値は null。 */
function parseNumericValue(text: string): number | null {
    const trimmed = text.trim();
    if (!trimmed) return null;
    // カンマ除去
    const cleaned = trimmed.replace(/,/g, "");
    const num = Number(cleaned);
    return isNaN(num) ? null : num;
}

// ============================================================
// セグメント結合情報
// ============================================================
interface SegmentColumnDef {
    display_key: string;  // 正規化キー (英日重複排除用)
    segmentName: string;  // 表示名 (日本語優先)
    salesKey: string;
    profitKey: string;
}

/** 前四半期の定義 (Q単体計算用) */
const SEG_PREV_QUARTER: Record<string, string | null> = {
    "1Q": null,
    "2Q": "1Q",
    "3Q": "2Q",
    "FY": "3Q",
};

function buildSegmentInfo(
    segments: SegmentRecord[],
    opts: { disableSemanticMerge?: boolean } = {}
) {
    // ダミーセグメント（「売上」「利益」等で sales=0, profit=0 のみ）を除外
    const DUMMY_NAMES = new Set(["売上", "利益", "#VALUE!", "0", "月次売上", "累計", "ＧＰ"]);
    
    // ダミー除外のみ。null値行も含め全期間の名前を収集することで
    // 言語が期をまたぐケース（FY=英語、2Q=日本語）でも日本語優先ラベルになる
    const allSegments = segments.filter(
        (seg) =>
            seg.segment_name &&
            !DUMMY_NAMES.has(seg.segment_name) &&
            !seg.segment_name.startsWith("UNKNOWN_"),
    );

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // segmentColumns の基準: source グループごとに最新 FY period を求め合算
    //
    // 変更理由:
    //   全体の最新 FY を基準にすると、Allタブ等で複数 source が混在する場合に
    //   他 source の最新 FY セグメントが列定義に入らなくなる。
    //   例) TDNET 最新FY=2026-03-31 / EDINET 最新FY=2025-03-31 のとき、
    //       全体最新FY=2026-03-31 を基準にすると 2025FY EDINET 日本語列が欠落する。
    //   → source 別に最新FY を求め、各 source の最新FY セグメントを合算する。
    //
    // 単一 source (TDNET/EDINET タブ) の場合は従来と同一の動作になる。
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    const referenceSegments = (() => {
        const uniqueSources = Array.from(
            new Set(allSegments.map((s) => s.source).filter((src): src is string => Boolean(src)))
        );
        if (uniqueSources.length === 0) {
            // source なし: 従来通り全体最新 FY period 基準
            const latestFY = allSegments
                .filter((s) => s.quarter === "FY")
                .reduce((max, s) => (s.period > max ? s.period : max), "");
            const refP = latestFY || allSegments.reduce((max, s) => (s.period > max ? s.period : max), "");
            return allSegments.filter((s) => s.period === refP && (latestFY ? s.quarter === "FY" : true));
        }
        // source 別に最新 FY period を求め、各 source の最新 FY セグメントを収集して重複排除
        const seenKeys = new Set<string>();
        const result: SegmentRecord[] = [];
        for (const src of uniqueSources) {
            const srcSegs = allSegments.filter((s) => s.source === src);
            const srcFY = srcSegs.filter((s) => s.quarter === "FY");
            const latestFYForSrc = srcFY.reduce((max, s) => (s.period > max ? s.period : max), "");
            const refPeriodForSrc = latestFYForSrc ||
                srcSegs.reduce((max, s) => (s.period > max ? s.period : max), "");
            for (const seg of srcSegs) {
                if (
                    seg.period === refPeriodForSrc &&
                    (latestFYForSrc ? seg.quarter === "FY" : true)
                ) {
                    const dedupeKey = `${seg.period}|${seg.quarter}|${seg.segment_name}`;
                    if (!seenKeys.has(dedupeKey)) {
                        seenKeys.add(dedupeKey);
                        result.push(seg);
                    }
                }
            }
        }
        return result;
    })();

    // 日本語アンカー Set は referenceSegments のみから構築
    // (他 period の EDINET 日本語名を列キーに混入させない)
    const _JP_CHK = /[\u3040-\u30ff\u4e00-\u9fff]/;

    // disableSemanticMerge=true (Allタブ) の場合: jpAnchorSet / jpSemanticAnchorMap を空にし
    // resolveDk が semantic 統合を行わないようにする。
    // これにより TDNET 英語名と EDINET 日本語名が同じ意味キーで1列に潰れるのを防ぐ。
    const jpAnchorSet = opts.disableSemanticMerge
        ? new Set<string>()
        : new Set<string>(
            referenceSegments
                .map((s) => s.segment_name)
                .filter((name) => name && _JP_CHK.test(name))
                .map((name) => normalizeSegmentDisplayKey(name))
                .filter(Boolean),
        );

    // semantic key → Japanese display_dk マップ
    // 日英共通の意味キーで照合し、英語名を日本語 display_dk へ解決する
    // disableSemanticMerge=true の場合は構築しない（空 Map のまま）
    const jpSemanticAnchorMap = new Map<string, string>();
    if (!opts.disableSemanticMerge) {
        for (const seg of referenceSegments) {
            const name = seg.segment_name;
            if (!name || !_JP_CHK.test(name)) continue;
            const displayDk = normalizeSegmentDisplayKey(name);
            const semanticDk = normalizeSegmentSemanticKey(name);
            if (displayDk && semanticDk && !jpSemanticAnchorMap.has(semanticDk)) {
                jpSemanticAnchorMap.set(semanticDk, displayDk);
            }
        }
    }

    // TDNET英語名を日本語アンカーdkへ解決するヘルパー
    // 0. semantic key → 1. alias完全一致 → 2. alias部分一致
    const resolveDk = (name: string): string => {
        const baseDk = normalizeSegmentDisplayKey(name) || name;

        // 0. semantic key 判定（日英共通意味キーで JP displayDk へ解決）
        const semanticKey = normalizeSegmentSemanticKey(name);
        const jpFromSemantic = jpSemanticAnchorMap.get(semanticKey);
        const alias = normalizeSegmentAliasKey(name);
        const aliasDk = alias ? (normalizeSegmentDisplayKey(alias) || "") : "";
        if (jpFromSemantic) return jpFromSemantic;

        // 1 & 2. alias判定（完全一致 → 部分一致）
        if (alias && aliasDk) {
            // 1. 完全一致
            if (jpAnchorSet.has(aliasDk)) return aliasDk;
            // 2. 部分一致フォールバック（短すぎるキーの誤統合を防ぐ）
            if (aliasDk.length >= 3) {
                for (const jpDk of jpAnchorSet) {
                    if (
                        jpDk.length >= 3 &&
                        (jpDk.includes(aliasDk) || aliasDk.includes(jpDk))
                    ) {
                        return jpDk;
                    }
                }
            }
        }
        return baseDk;
    };

    // nameMap: referenceSegments のみから列定義を構築
    // (全 period 横断にしないことで、過去期にしか存在しないセグメントを列化しない)
    const nameMap = new Map<string, string[]>();
    for (const seg of referenceSegments) {
        const dk = resolveDk(seg.segment_name);
        if (!nameMap.has(dk)) nameMap.set(dk, []);
        nameMap.get(dk)!.push(seg.segment_name);
    }

    const filtered = segments.filter((seg) => {
        if (!seg.segment_name) return false;
        if (DUMMY_NAMES.has(seg.segment_name)) return false;
        if (seg.segment_name.startsWith("UNKNOWN_")) return false;
        if ((seg.segment_sales === null || seg.segment_sales === 0) &&
            (seg.segment_profit === null || seg.segment_profit === 0)) return false;
        return true;
    });


    const segmentColumns: SegmentColumnDef[] = Array.from(nameMap.entries()).map(([dk, names]) => ({
        display_key: dk,
        segmentName: pickSegmentDisplayName(names),
        salesKey: `seg:${dk}:sales`,
        profitKey: `seg:${dk}:profit`,
    }));

    // 累計用 segmentMap: key = "period|quarter"
    const segmentMap = new Map<string, Record<string, number | null>>();
    for (const seg of filtered) {
        const key = `${seg.period}|${seg.quarter}`;
        if (!segmentMap.has(key)) segmentMap.set(key, {});
        const row = segmentMap.get(key)!;
        const dk = resolveDk(seg.segment_name);
        const col = segmentColumns.find((c) => c.salesKey === `seg:${dk}:sales`);
        if (col) {
            row[col.salesKey] = seg.segment_sales;
            row[col.profitKey] = seg.segment_profit;
        }
    }

    // Q単体用 segmentQMap: key = "period|quarter"
    // 1Q: 累計そのまま, 2Q: 2Q累計-1Q累計, 3Q: 3Q累計-2Q累計, FY: FY累計-3Q累計
    const segmentQMap = new Map<string, Record<string, number | null>>();
    // periodごとにグルーピングして差分計算
    const periodSet = new Set<string>();
    for (const seg of filtered) {
        periodSet.add(seg.period);
    }
    for (const period of periodSet) {
        for (const q of ["1Q", "2Q", "3Q", "FY"]) {
            const curKey = `${period}|${q}`;
            const curData = segmentMap.get(curKey);
            if (!curData) continue;

            const prevQ = SEG_PREV_QUARTER[q];
            const qRow: Record<string, number | null> = {};

            for (const col of segmentColumns) {
                const curSales = curData[col.salesKey] ?? null;
                const curProfit = curData[col.profitKey] ?? null;

                if (prevQ === null) {
                    // 1Q: 累計 = 単体
                    qRow[col.salesKey] = curSales;
                    qRow[col.profitKey] = curProfit;
                } else {
                    const prevKey = `${period}|${prevQ}`;
                    const prevData = segmentMap.get(prevKey);
                    const prevSales = prevData?.[col.salesKey] ?? null;
                    const prevProfit = prevData?.[col.profitKey] ?? null;

                    if (prevData === undefined) {
                        // 前四半期セグメントデータが存在しない（FYのみのケース等）:
                        // 差分計算不能のため累計値をそのまま使用（表示上は累計値として扱う）
                        qRow[col.salesKey] = curSales;
                        qRow[col.profitKey] = curProfit;
                    } else {
                        // 前四半期データあり: 差分計算
                        qRow[col.salesKey] = (curSales !== null && prevSales !== null) ? curSales - prevSales : null;
                        qRow[col.profitKey] = (curProfit !== null && prevProfit !== null) ? curProfit - prevProfit : null;
                    }
                }
            }
            segmentQMap.set(curKey, qRow);
        }
    }


    // Per-cell source tracking: build from SegmentRecord._salesSource / _profitSource
    // Key: "period|quarter|seg:name:sales" or "period|quarter|seg:name:profit"
    const sourceMap = new Map<string, string>();
    for (const seg of segments) {
        const key = `${seg.period}|${seg.quarter}`;
        const col2 = (() => {
            const dk = resolveDk(seg.segment_name);
            return segmentColumns.find((c) => c.salesKey === `seg:${dk}:sales`);
        })();
        if (col2) {
            if (seg._salesSource) sourceMap.set(`${key}|${col2.salesKey}`, seg._salesSource);
            if (seg._profitSource) sourceMap.set(`${key}|${col2.profitKey}`, seg._profitSource);
        }
    }

    return { segmentColumns, segmentMap, segmentQMap, sourceMap };
}

// ============================================================
// PLリサイズ高さ管理
// ============================================================
const PL_HEIGHT_KEY = "pl-scroll-height";
const DEFAULT_HEIGHT = 600;
const MIN_HEIGHT = 200;
const MAX_HEIGHT = 1200;

function loadSavedHeight(): number {
    if (typeof window === "undefined") return DEFAULT_HEIGHT;
    const saved = localStorage.getItem(PL_HEIGHT_KEY);
    if (saved) {
        const n = parseInt(saved, 10);
        if (!isNaN(n) && n >= MIN_HEIGHT && n <= MAX_HEIGHT) return n;
    }
    return DEFAULT_HEIGHT;
}

// ============================================================
// PLテーブルヘッダー
// ============================================================
function PLTableHeader({
    columns,
    widths,
    onResizeStart,
    segHeaders,
    segWidths,
    onSegResizeStart,
    kpiSlots,
    kpiDefs,
    kpiWidths,
    onKpiResizeStart,
    editingKpiHeader,
    editingKpiHeaderValue,
    kpiHeaderInputRef,
    onStartKpiHeaderEdit,
    onEditingKpiHeaderValueChange,
    onCommitKpiHeaderEdit,
    onCancelKpiHeaderEdit,
}: {
    columns: ColumnDef[];
    widths: number[];
    onResizeStart: (colIndex: number, e: React.MouseEvent) => void;
    segHeaders?: { label: string; fullName?: string; className?: string }[];
    segWidths?: number[];
    onSegResizeStart?: (colIndex: number, e: React.MouseEvent) => void;
    kpiSlots?: readonly number[];
    kpiDefs?: KpiDefMap;
    kpiWidths?: number[];
    onKpiResizeStart?: (colIndex: number, e: React.MouseEvent) => void;
    editingKpiHeader?: number | null;
    editingKpiHeaderValue?: string;
    kpiHeaderInputRef?: React.RefObject<HTMLInputElement | null>;
    onStartKpiHeaderEdit?: (slot: number) => void;
    onEditingKpiHeaderValueChange?: (v: string) => void;
    onCommitKpiHeaderEdit?: () => void;
    onCancelKpiHeaderEdit?: () => void;
}) {
    return (
        <thead>
            <tr>
                {columns.map((col, idx) => (
                    <th
                        key={col.key}
                        className={col.className || ""}
                        style={{ width: widths[idx], minWidth: widths[idx] }}
                    >
                        <div className="th-content">
                            <span>{col.label}</span>
                            <div
                                className="resize-handle"
                                onMouseDown={(e) => onResizeStart(idx, e)}
                            />
                        </div>
                    </th>
                ))}
                {segHeaders?.map((eh, idx) => {
                    const w = segWidths?.[idx] ?? 90;
                    return (
                        <th
                            key={`seg-${idx}`}
                            className={`seg-header-cell ${eh.className || "num-col"}`}
                            style={{ width: w, minWidth: 24 }}
                            title={eh.fullName ?? eh.label}
                            data-fullname={eh.fullName ?? eh.label}
                        >
                            <div className="th-content">
                                <span className="seg-header-label">{eh.label}</span>
                                {onSegResizeStart && (
                                    <div
                                        className="resize-handle"
                                        onMouseDown={(e) => onSegResizeStart(idx, e)}
                                    />
                                )}
                            </div>
                        </th>
                    );
                })}
                {kpiSlots?.map((slot, idx) => {
                    const w = kpiWidths?.[idx] ?? 90;
                    const isEditing = editingKpiHeader === slot;
                    return (
                        <th
                            key={`kpi-${slot}`}
                            className="kpi-header-cell"
                            style={{ width: w, minWidth: 24 }}
                            onDoubleClick={() => onStartKpiHeaderEdit?.(slot)}
                        >
                            <div className="th-content">
                                {isEditing ? (
                                    <input
                                        ref={kpiHeaderInputRef as React.RefObject<HTMLInputElement>}
                                        className="kpi-header-input"
                                        value={editingKpiHeaderValue ?? ""}
                                        onChange={(e) => onEditingKpiHeaderValueChange?.(e.target.value)}
                                        onKeyDown={(e) => {
                                            if (e.key === "Enter") { e.preventDefault(); onCommitKpiHeaderEdit?.(); }
                                            if (e.key === "Escape") { e.preventDefault(); onCancelKpiHeaderEdit?.(); }
                                        }}
                                        onBlur={() => onCommitKpiHeaderEdit?.()}
                                    />
                                ) : (
                                    <span className="kpi-header-label" title="ダブルクリックで編集">{kpiDefs?.[slot] ?? `KPI ${slot}`}</span>
                                )}
                                {onKpiResizeStart && (
                                    <div
                                        className="resize-handle"
                                        onMouseDown={(e) => onKpiResizeStart(idx, e)}
                                    />
                                )}
                            </div>
                        </th>
                    );
                })}
            </tr>
        </thead>
    );
}

// ============================================================
// メインコンポーネント
// ============================================================
function FinancialsTable({
    data,
    loading,
    selectedPeriod,
    selectedQuarter,
    onRowClick,
    memoMap,
    onMemoEdit,
    onMemoPaste,
    segments,
    kpiDefs,
    kpiValues,
    onKpiHeaderEdit,
    onKpiValueEdit,
    onSegmentOverrideSave,
    onSegmentOverrideDelete,
    onBulkSaveOverrides,
    onUndo,
    manualTableMemos,
    onManualMemoEdit,
    onManualMemoGridUpdate,
    segmentManualHeaders,
    onSegmentManualHeaderEdit,
    onPlScrollAreaReady,
}: FinancialsTableProps) {
    // 実績 + 予想 全行（FORECAST_SOURCES が値を锻定）
    const filteredAll    = useMemo(() => filterLast5Years(data), [data]);
    // メイン PL テーブル用: 実績行のみ
    const filteredActual = useMemo(
        () => filteredAll.filter(r => !FORECAST_SOURCES.has(r.source ?? "")),
        [filteredAll]
    );
    const sorted = useMemo(() => sortForDisplay(filteredActual), [filteredActual]);
    const cumRows = useMemo(() => buildCumulativeRows(sorted), [sorted]);
    const qRows = useMemo(() => buildQStandaloneRows(sorted), [sorted]);
    // 最新FY予想バー用: FY 予想行のみ（period 降順）
    const forecastFYRows = useMemo(
        () => filteredAll
            .filter(r => r.quarter === "FY" && FORECAST_SOURCES.has(r.source ?? ""))
            .sort((a, b) => b.period.localeCompare(a.period)),
        [filteredAll]
    );

    const [segSourceTab, setSegSourceTab] = useState<SegSourceTab>("tdnet");

    const filteredBySource = useMemo(() => {
        const segs = segments || [];
        if (segSourceTab === "tdnet")  return segs.filter(s => TDNET_SOURCES.has(s.source ?? ""));
        if (segSourceTab === "edinet") return segs.filter(s => EDINET_SOURCES.has(s.source ?? ""));
        if (segSourceTab === "memo")   return segs; // memoモードでは全て返す（テーブル自体は非表示）
        // "all": whitelist 全体（TDNET + EDINET）のみ。source なしは除外。
        return segs.filter(s => TDNET_SOURCES.has(s.source ?? "") || EDINET_SOURCES.has(s.source ?? ""));
    }, [segments, segSourceTab]);

    // ── source_priority 防御的フィルタ ──
    // period + quarter 単位で最小 source_priority の行だけを残す。
    // viewer-api.ts 側でも同様のフィルタを実施しているが、
    // buildSegmentInfo / referenceSegments での混入を防ぐ二重防衛として追加。
    // source_priority が null/undefined の場合は 99 扱い。
    const filteredForSegBuild = useMemo(() => {
        if (filteredBySource.length === 0) return filteredBySource;
        const minMap = new Map<string, number>();
        for (const s of filteredBySource) {
            const key = `${s.period ?? ""}|${s.quarter ?? ""}`;
            const pri = s.source_priority != null ? Number(s.source_priority) : 99;
            const cur = minMap.get(key);
            if (cur === undefined || pri < cur) minMap.set(key, pri);
        }
        return filteredBySource.filter((s) => {
            const key = `${s.period ?? ""}|${s.quarter ?? ""}`;
            const pri = s.source_priority != null ? Number(s.source_priority) : 99;
            return pri === (minMap.get(key) ?? 99);
        });
    }, [filteredBySource]);

    // セグメント列 (filteredForSegBuild を入力にすることでタブ切替え + priority フィルタを実現)
    const { segmentColumns, segmentMap, segmentQMap, sourceMap } = useMemo(
        () => buildSegmentInfo(filteredForSegBuild, { disableSemanticMerge: segSourceTab === "all" }),
        [filteredForSegBuild, segSourceTab]
    );
    // セグメント列ヘッダー（累計PL・Q単体PL共通）
    const segmentHeaders = useMemo(() => {
        const headers: { label: string; fullName?: string; className?: string }[] = [];
        for (const sc of segmentColumns) {
            // "管工機材売上(円)" → "管工機材" のように末尾の単位表記を除去
            const cleanName = sc.segmentName
                .replace(/[（(]円[)）]/g, "")
                .replace(/売上$/, "")
                .replace(/利益$/, "")
                .trim();
            // fullName: hover tooltip 用（省略前の元の segment 名）
            headers.push({
                label: `${cleanName} 売上`,
                fullName: `${sc.segmentName} — 売上（百万円）`,
                className: "num-col seg-sales-col",
            });
            headers.push({
                label: `${cleanName} 利益`,
                fullName: `${sc.segmentName} — 利益（百万円）`,
                className: "num-col seg-profit-col",
            });
        }
        return headers;
    }, [segmentColumns]);

    // 列幅管理
    const cumResize = useColumnResize({ storageKey: "pl-cum-v5", columns: CUM_COLUMNS });
    const qResize = useColumnResize({ storageKey: "pl-q-v5", columns: Q_BASE_COLUMNS });
    const memoKpiResize = useColumnResize({ storageKey: "pl-memo-kpi-v2", columns: MEMO_KPI_BASE_COLUMNS });

    // セグメント列幅管理 (動的列数に対応)
    const segColCount = segmentHeaders.length; // 各セグメント×(売上+利益)
    const [segWidths, setSegWidths] = useState<number[]>(() => {
        if (typeof window !== "undefined" && segColCount > 0) {
            try {
                const saved = localStorage.getItem("seg-col-widths");
                if (saved) {
                    const parsed = JSON.parse(saved) as number[];
                    if (parsed.length === segColCount) return parsed;
                }
            } catch { /* ignore */ }
        }
        return Array(segColCount).fill(90);
    });

    // セグメント列数が変わったら幅をリセット
    useEffect(() => {
        if (segColCount > 0 && segWidths.length !== segColCount) {
            setSegWidths(Array(segColCount).fill(90));
        }
    }, [segColCount, segWidths.length]);

    // セグメント列幅永続化
    useEffect(() => {
        if (segColCount > 0) {
            try {
                localStorage.setItem("seg-col-widths", JSON.stringify(segWidths));
            } catch { /* ignore */ }
        }
    }, [segWidths, segColCount]);

    const segDragState = useRef<{ colIndex: number; startX: number; startWidth: number } | null>(null);
    const handleSegResizeStart = useCallback((colIndex: number, e: React.MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        segDragState.current = { colIndex, startX: e.clientX, startWidth: segWidths[colIndex] ?? 90 };

        const onMove = (ev: MouseEvent) => {
            if (!segDragState.current) return;
            const diff = ev.clientX - segDragState.current.startX;
            const newW = Math.max(24, segDragState.current.startWidth + diff);
            setSegWidths((prev) => {
                const next = [...prev];
                next[segDragState.current!.colIndex] = newW;
                return next;
            });
        };
        const onUp = () => {
            segDragState.current = null;
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
    }, [segWidths]);

    // KPI列幅管理 (3列固定)
    const [kpiWidths, setKpiWidths] = useState<number[]>(() => {
        if (typeof window !== "undefined") {
            try {
                const saved = localStorage.getItem("kpi-col-widths");
                if (saved) {
                    const parsed = JSON.parse(saved) as number[];
                    if (parsed.length === 3) return parsed;
                }
            } catch { /* ignore */ }
        }
        return [90, 90, 90];
    });
    useEffect(() => {
        try { localStorage.setItem("kpi-col-widths", JSON.stringify(kpiWidths)); } catch { /* ignore */ }
    }, [kpiWidths]);

    const kpiDragState = useRef<{ colIndex: number; startX: number; startWidth: number } | null>(null);
    const handleKpiResizeStart = useCallback((colIndex: number, e: React.MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        kpiDragState.current = { colIndex, startX: e.clientX, startWidth: kpiWidths[colIndex] ?? 90 };
        const onMove = (ev: MouseEvent) => {
            if (!kpiDragState.current) return;
            const diff = ev.clientX - kpiDragState.current.startX;
            const newW = Math.max(24, kpiDragState.current.startWidth + diff);
            setKpiWidths((prev) => { const next = [...prev]; next[kpiDragState.current!.colIndex] = newW; return next; });
        };
        const onUp = () => {
            kpiDragState.current = null;
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
    }, [kpiWidths]);

    // KPIヘッダー編集state
    const [editingKpiHeader, setEditingKpiHeader] = useState<number | null>(null);
    const [editingKpiHeaderValue, setEditingKpiHeaderValue] = useState("");
    const kpiHeaderInputRef = useRef<HTMLInputElement>(null);

    const startKpiHeaderEdit = useCallback((slot: number) => {
        setEditingKpiHeader(slot);
        setEditingKpiHeaderValue(kpiDefs?.[slot] || `KPI ${slot}`);
        setTimeout(() => kpiHeaderInputRef.current?.focus(), 0);
    }, [kpiDefs]);

    const commitKpiHeaderEdit = useCallback(() => {
        if (editingKpiHeader === null) return;
        const name = editingKpiHeaderValue.trim() || `KPI ${editingKpiHeader}`;
        onKpiHeaderEdit?.(editingKpiHeader, name);
        setEditingKpiHeader(null);
    }, [editingKpiHeader, editingKpiHeaderValue, onKpiHeaderEdit]);

    const cancelKpiHeaderEdit = useCallback(() => {
        setEditingKpiHeader(null);
    }, []);

    // KPIセルは activeCell / editingCell に統合済み。別管理stateは不要。

    const kpiExtraWidth = kpiWidths.reduce((s, w) => s + w, 0);
    const segExtraWidth = segWidths.reduce((s, w) => s + w, 0);
    // PLテーブル幅（セグメント列なし）
    // 累計PL: 表示8列分のみ (period/q/sales/gp/gm_rate/sga/op/op_margin)
    const cumTableWidth = cumResize.widths.slice(0, 8).reduce((s, w) => s + w, 0);
    // Q/memo_kpi は period/q (先頭2列) を非表示にするため、幅計算から除外
    const qTableWidth = qResize.widths.slice(2).reduce((s, w) => s + w, 0);
    const memoKpiTableWidth = memoKpiResize.widths.slice(2).reduce((s, w) => s + w, 0) + kpiExtraWidth;
    // セグメントテーブル幅
    const segCumTableWidth = 100 + 45 + segExtraWidth;
    const segQTableWidth = 100 + 45 + segExtraWidth;

    // ============================================================
    // Excel-like セル操作
    // ============================================================
    const [activeCell, setActiveCell] = useState<CellCoord | null>(null);
    const [editingCell, setEditingCell] = useState<CellCoord | null>(null);
    const [editValue, setEditValue] = useState("");
    const editInputRef = useRef<HTMLElement>(null);
    const formulaBarRef = useRef<HTMLTextAreaElement>(null);
    const gridRef = useRef<HTMLDivElement>(null);

    // PL メモ / KPI 専用編集 state（セグメントメモの editingManualCell と完全同構造）
    // editingCell / commitEdit / gridRef.focus の既存経路とは独立
    const [editingPlMemoCell, setEditingPlMemoCell] = useState<CellCoord | null>(null);
    const [plMemoEditValue, setPlMemoEditValue] = useState("");
    const plMemoInputRef = useRef<HTMLElement>(null);
    const isCommittingPlMemoRef = useRef(false);

    // 範囲選択
    const [selectionRange, setSelectionRange] = useState<SelectionRange | null>(null);
    const isDragging = useRef(false);
    const dragDidMove = useRef(false);  // ドラッグ中に別セルへ移動したか
    // mousedown 時にセットし、mouseup で dragDidMove===false のときだけ実行する編集開始関数
    const pendingPlMemoClick = useRef<(() => void) | null>(null);

    // 手入力メモ専用行の編集 state
    const [activeManualCell, setActiveManualCell] = useState<{ tableType: ManualTableType; rowIdx: number; colIdx: number } | null>(null);
    const [editingManualCell, setEditingManualCell] = useState<{ tableType: ManualTableType; rowIdx: number; colIdx: number } | null>(null);
    const [manualEditValue, setManualEditValue] = useState("");
    const isCommittingManualRef = useRef(false);
    // segment_manual 範囲選択 state とドラッグ追跡 ref
    const [segManualSel, setSegManualSel] = useState<{
        startRow: number; startCol: number; endRow: number; endCol: number;
    } | null>(null);
    const segManualIsDragging = useRef(false);
    const segManualDragMoved = useRef(false);
    // ドラッグ開始セル（mouseup 時の単一クリック判定用）
    const segManualDragStartRef = useRef<{ rowIdx: number; colIdx: number } | null>(null);
    // startManualEditing の最新版を保持（useEffect 内で前方参照なく呼ぶため）
    const startManualEditingRef = useRef<((coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }, initialValue: string) => void) | null>(null);
    // onManualMemoEdit / onManualMemoGridUpdate の最新版を常に保持（ペースト用）
    const onManualMemoEditRef = useRef(onManualMemoEdit);
    useEffect(() => { onManualMemoEditRef.current = onManualMemoEdit; }, [onManualMemoEdit]);
    const onManualMemoGridUpdateRef = useRef(onManualMemoGridUpdate);
    useEffect(() => { onManualMemoGridUpdateRef.current = onManualMemoGridUpdate; }, [onManualMemoGridUpdate]);
    // manualTableMemos の最新版を常に保持（バッチペースト用）
    const manualTableMemosRef = useRef(manualTableMemos);
    useEffect(() => { manualTableMemosRef.current = manualTableMemos; }, [manualTableMemos]);
    // activeManualCell / segManualSel の最新版を copy イベント内から参照するための ref
    const activeManualCellRef = useRef(activeManualCell);
    useEffect(() => { activeManualCellRef.current = activeManualCell; }, [activeManualCell]);
    const segManualSelRef = useRef(segManualSel);
    useEffect(() => { segManualSelRef.current = segManualSel; }, [segManualSel]);

    // フォーミュラバー高さリサイズ
    const FB_HEIGHT_KEY = "formula-bar-height";
    const FB_DEFAULT_HEIGHT = 52;
    const FB_MIN_HEIGHT = 28;
    const FB_MAX_HEIGHT = 300;
    const [formulaBarHeight, setFormulaBarHeight] = useState(() => {
        if (typeof window === "undefined") return FB_DEFAULT_HEIGHT;
        const saved = localStorage.getItem(FB_HEIGHT_KEY);
        if (saved) { const n = parseInt(saved, 10); if (!isNaN(n) && n >= FB_MIN_HEIGHT && n <= FB_MAX_HEIGHT) return n; }
        return FB_DEFAULT_HEIGHT;
    });
    const fbResizing = useRef(false);
    const fbResizeStartY = useRef(0);
    const fbResizeStartH = useRef(0);

    useEffect(() => {
        localStorage.setItem(FB_HEIGHT_KEY, String(formulaBarHeight));
    }, [formulaBarHeight]);

    const handleFbResizeStart = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        fbResizing.current = true;
        fbResizeStartY.current = e.clientY;
        fbResizeStartH.current = formulaBarHeight;
        const onMove = (ev: MouseEvent) => {
            if (!fbResizing.current) return;
            const diff = ev.clientY - fbResizeStartY.current;
            const newH = Math.max(FB_MIN_HEIGHT, Math.min(FB_MAX_HEIGHT, fbResizeStartH.current + diff));
            setFormulaBarHeight(newH);
        };
        const onUp = () => {
            fbResizing.current = false;
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        document.body.style.cursor = "row-resize";
        document.body.style.userSelect = "none";
    }, [formulaBarHeight]);

    // 編集開始直後フラグ: startEditing/startManualEditing 後の requestAnimationFrame 内で
    // gridRef.focus() がtextareaのfocusを奔うのを防ぐ
    const justStartedEditingRef = useRef(false);
    // PL メモ編集中フラグ: editingPlMemoCell の最新値を ref で常に保持（useCallback deps に含めず参照できる）
    const editingPlMemoCellRef = useRef<CellCoord | null>(null);
    // PL メモ編集開始時刻: startPlMemoEditing 実行時に Date.now() を保存
    // textarea onBlur で編集開始直後の多照 blur を無視するために使用
    const plMemoEditStartedAtRef = useRef<number>(0);

    // セグメントセルのアクティブ管理
    const [activeSegCell, setActiveSegCell] = useState<SegCellCoord | null>(null);
    // セグメントセルの編集管理（親制御）
    const [editingSegCell, setEditingSegCell] = useState<SegCellCoord | null>(null);
    const [segEditValue, setSegEditValue] = useState("");
    // トースト
    const [toastMessage, setToastMessage] = useState<string | null>(null);
    const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    const showToast = useCallback((msg: string) => {
        setToastMessage(msg);
        if (toastTimer.current) clearTimeout(toastTimer.current);
        toastTimer.current = setTimeout(() => setToastMessage(null), 4000);
    }, []);

    // PLリサイズ高さ
    const [plHeight, setPlHeight] = useState(DEFAULT_HEIGHT);
    const resizing = useRef(false);
    const resizeStartY = useRef(0);
    const resizeStartHeight = useRef(0);

    useEffect(() => {
        setPlHeight(loadSavedHeight());
    }, []);

    // segment_manual ドラッグ選択の終了検出（document レベルで mouseup を捕捉）
    // mousedown で即編集開始済みのため、mouseup はドラッグフラグのリセットのみ担当
    useEffect(() => {
        const handleDocMouseUp = (_ev: MouseEvent) => {
            if (!segManualIsDragging.current) return;
            segManualIsDragging.current = false;
            segManualDragMoved.current = false;
            segManualDragStartRef.current = null;
        };
        document.addEventListener("mouseup", handleDocMouseUp);
        return () => document.removeEventListener("mouseup", handleDocMouseUp);
    }, []);

    // フォーミュラバー用: 現在のセル値を取得
    const getActiveCellValue = useCallback((): string => {
        if (!activeCell) return "";
        // pl_cum_manual / pl_q_manual は manualTableMemos から取得
        if (activeCell.tableId === "pl_cum_manual") {
            const colIdx = parseInt(activeCell.colKey.replace("col_", ""));
            return manualTableMemos?.pl_cum?.[activeCell.rowIdx]?.[colIdx] ?? "";
        }
        if (activeCell.tableId === "pl_q_manual") {
            const colIdx = parseInt(activeCell.colKey.replace("col_", "")) + 2;
            return manualTableMemos?.pl_q?.[activeCell.rowIdx]?.[colIdx] ?? "";
        }
        const rows = activeCell.tableId === "cum" ? cumRows : qRows;
        const row = rows[activeCell.rowIdx];
        if (!row) return "";
        const key = activeCell.colKey;
        if (key === "memo_a" || key === "memo_b") {
            if (!memoMap) return "";
            const memoKey = `${row.period}|${row.quarter}`;
            const colIdx = key === "memo_a" ? 0 : 1;
            return extractMemoValue(memoMap[memoKey], colIdx);
        }
        if (key.startsWith("kpi_")) {
            const slot = parseInt(key.split("_")[1]);
            const kpiKey = `${activeCell.tableId}|${row.period}|${row.quarter}`;
            return kpiValues?.[kpiKey]?.[slot] ?? "";
        }
        return "";
    }, [activeCell, memoMap, cumRows, qRows, kpiValues]);

    // フォーミュラバーの表示ラベル
    const activeCellLabel = useMemo((): string => {
        if (!activeCell) return "";
        if (activeCell.tableId === "pl_cum_manual") {
            const colIdx = parseInt(activeCell.colKey.replace("col_", ""));
            return `下メモ(累計) / 行${activeCell.rowIdx + 1} / 列${colIdx + 1}`;
        }
        if (activeCell.tableId === "pl_q_manual") {
            const colIdx = parseInt(activeCell.colKey.replace("col_", ""));
            return `下メモ(Q単体) / 行${activeCell.rowIdx + 1} / 列${colIdx + 1}`;
        }
        const rows = activeCell.tableId === "cum" ? cumRows : qRows;
        const row = rows[activeCell.rowIdx];
        if (!row) return "";
        const key = activeCell.colKey;
        let colLabel = "";
        if (key === "memo_a") colLabel = "Memo A";
        else if (key === "memo_b") colLabel = "Memo B";
        else if (key.startsWith("kpi_")) {
            const slot = parseInt(key.split("_")[1]);
            colLabel = kpiDefs?.[slot] ?? `KPI ${slot}`;
        }
        const tableLabel = activeCell.tableId === "cum" ? "累PL" : "Q単";
        return `${tableLabel} / ${row.period} / ${row.quarter} / ${colLabel}`;
    }, [activeCell, cumRows, qRows, kpiDefs]);


    // grid wrapperにfocusを戻すヘルパー（編集中は focus を奪わない）
    const focusGrid = useCallback(() => {
        requestAnimationFrame(() => {
            if (justStartedEditingRef.current) {
                return; // 編集中なら focus を奪わない
            }
            // PL メモ編集中は gridRef に focus を戻さない
            if (editingPlMemoCellRef.current) {
                return;
            }
            gridRef.current?.focus();
        });
    }, []);

    // セル選択
    const selectCell = useCallback((coord: CellCoord) => {
        // PL メモ編集中は selectCell を実行しない（gridRef.focus に被るのを防ぐ）
        if (editingPlMemoCellRef.current) return;
        setActiveCell(coord);
        setEditingCell(null);
        setSelectionRange(null);
        // セグメントセルの選択を解除
        setActiveSegCell(null);
        setEditingSegCell(null);
        // 手入力メモ行の選択を解除（排他制御）
        setActiveManualCell(null);
        setEditingManualCell(null);
        // DOM focus をグリッドルートへ移動 (Ctrl+V 等のキーイベント受け取りのため)
        focusGrid();
    }, [focusGrid]);

    // 範囲選択: mousedown (ドラッグ開始)
    const handleCellMouseDown = useCallback((tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual", rowIdx: number, colIdx: number, e: React.MouseEvent) => {
        // PL メモ編集中は PL データセルの mousedown を無視する
        if (editingPlMemoCellRef.current) return;
        // 左クリックのみ
        if (e.button !== 0) return;
        e.preventDefault(); // ブラウザのテキスト選択(灰色ハイライト)を防止
        isDragging.current = true;
        dragDidMove.current = false;
        setSelectionRange({ tableId, startRow: rowIdx, startColIdx: colIdx, endRow: rowIdx, endColIdx: colIdx });
        setEditingCell(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        // 手入力メモ行の選択を解除（排他制御）
        setActiveManualCell(null);
        setEditingManualCell(null);
    }, []);

    // 範囲選択: mouseenter (ドラッグ中の拡張)
    const handleCellMouseEnter = useCallback((tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual", rowIdx: number, colIdx: number) => {
        if (!isDragging.current || !selectionRange) return;
        if (selectionRange.tableId !== tableId) return;
        // 別セルに入ったらドラッグ移動とみなす
        if (rowIdx !== selectionRange.startRow || colIdx !== selectionRange.startColIdx) {
            dragDidMove.current = true;
        }
        setSelectionRange((prev) => prev ? { ...prev, endRow: rowIdx, endColIdx: colIdx } : prev);
    }, [selectionRange]);

    // 範囲選択: mouseup (ドラッグ終了) — グローバルリスナー
    useEffect(() => {
        const handleMouseUp = () => {
            if (isDragging.current) {
                isDragging.current = false;
                if (!dragDidMove.current) {
                    // 単一セルクリック: 範囲クリア
                    setSelectionRange(null);
                    // PLメモ/KPIセルのクリック編集開始（ドラッグしていない場合のみ）
                    if (pendingPlMemoClick.current) {
                        pendingPlMemoClick.current();
                    }
                }
                pendingPlMemoClick.current = null;
                // dragDidMove は click イベント後にリセット (少し遅延)
                setTimeout(() => { dragDidMove.current = false; }, 0);
            }
        };
        document.addEventListener("mouseup", handleMouseUp);
        return () => document.removeEventListener("mouseup", handleMouseUp);
    }, []);

    // セルが範囲内か判定
    const isCellInRange = useCallback((tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual", rowIdx: number, colIdx: number): boolean => {
        if (!selectionRange || selectionRange.tableId !== tableId) return false;
        const minRow = Math.min(selectionRange.startRow, selectionRange.endRow);
        const maxRow = Math.max(selectionRange.startRow, selectionRange.endRow);
        const minCol = Math.min(selectionRange.startColIdx, selectionRange.endColIdx);
        const maxCol = Math.max(selectionRange.startColIdx, selectionRange.endColIdx);
        return rowIdx >= minRow && rowIdx <= maxRow && colIdx >= minCol && colIdx <= maxCol;
    }, [selectionRange]);

    // セル表示値を取得 (全列対応)
    const getCellDisplayValue = useCallback((tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual", rowIdx: number, colIdx: number): string => {
        // pl_cum_manual / pl_q_manual: manualTableMemos から直接取得
        if (tableId === "pl_cum_manual") {
            return manualTableMemos?.pl_cum?.[rowIdx]?.[colIdx] ?? "";
        }
        if (tableId === "pl_q_manual") {
            // colOffset=2 なので実インデックスは colIdx + 2
            return manualTableMemos?.pl_q?.[rowIdx]?.[colIdx + 2] ?? "";
        }
        // memo_kpi テーブル: cumRows を使い独自列マッピング
        // 0=period, 1=quarter, 2=memo_a, 3=memo_b, 4=kpi_1, 5=kpi_2, 6=kpi_3
        if (tableId === "memo_kpi") {
            const row = cumRows[rowIdx];
            if (!row) return "";
            if (colIdx === 0) return displayValue(row.period);
            if (colIdx === 1) return displayValue(row.quarter);
            const memoKey = `${row.period}|${row.quarter}`;
            const memoGrid = memoMap?.[memoKey];
            if (colIdx === 2) return extractMemoValue(memoGrid, 0);
            if (colIdx === 3) return extractMemoValue(memoGrid, 1);
            // colIdx 4=kpi_1, 5=kpi_2, 6=kpi_3
            if (colIdx >= 4 && colIdx <= 6) {
                const slot = colIdx - 3;
                const kpiKey = `cum|${row.period}|${row.quarter}`;
                return kpiValues?.[kpiKey]?.[slot] ?? "";
            }
            return "";
        }
        const rows = tableId === "cum" ? cumRows : qRows;
        const row = rows[rowIdx];
        if (!row) return "";
        const baseCount = tableId === "cum" ? CUM_BASE_COL_COUNT : Q_BASE_COL_COUNT;
        const kpiStart = baseCount;

        // 基本列 (0=period,1=Q,2=sales,3=gp,4=gm_rate,5=sga,6=op,7=margin, cum:8=memoA,9=memoB)
        if (colIdx < baseCount) {
            if (colIdx === 0) return row.period || "";
            if (colIdx === 1) return row.quarter || "";
            if (colIdx === 2) return formatMillions(row.sales) ?? "";
            if (colIdx === 3) return formatMillions(row.grossProfit) ?? "";
            if (colIdx === 4) return fmtMargin(row.grossMarginRate);
            if (colIdx === 5) return formatMillions(row.sgAndA) ?? "";
            if (colIdx === 6) return formatMillions(row.operatingProfit) ?? "";
            if (colIdx === 7) return fmtMargin(row.opMargin);
            if (tableId === "cum" && colIdx === 8) {
                const mKey = `${row.period}|${row.quarter}`;
                return extractMemoValue(memoMap?.[mKey], 0);
            }
            if (tableId === "cum" && colIdx === 9) {
                const mKey = `${row.period}|${row.quarter}`;
                return extractMemoValue(memoMap?.[mKey], 1);
            }
        }
        // KPI列
        if (colIdx >= kpiStart && colIdx < kpiStart + 3) {
            const slot = colIdx - kpiStart + 1;
            const kpiKey = `${tableId}|${row.period}|${row.quarter}`;
            return kpiValues?.[kpiKey]?.[slot] ?? "";
        }
        return "";
    }, [cumRows, qRows, memoMap, kpiValues, manualTableMemos]);

    // TSVクォーティング (改行/タブを含むセルをダブルクォートで囲む)
    const quoteTsvCell = (val: string): string => {
        if (val.includes("\t") || val.includes("\n") || val.includes("\r") || val.includes('"')) {
            return '"' + val.replace(/"/g, '""') + '"';
        }
        return val;
    };

    // 範囲のセル値を取得 (TSVフォーマット)
    const getRangeAsTsv = useCallback((): string => {
        if (!selectionRange) return "";
        const minRow = Math.min(selectionRange.startRow, selectionRange.endRow);
        const maxRow = Math.max(selectionRange.startRow, selectionRange.endRow);
        const minCol = Math.min(selectionRange.startColIdx, selectionRange.endColIdx);
        const maxCol = Math.max(selectionRange.startColIdx, selectionRange.endColIdx);
        const lines: string[] = [];
        for (let r = minRow; r <= maxRow; r++) {
            const cells: string[] = [];
            for (let c = minCol; c <= maxCol; c++) {
                cells.push(quoteTsvCell(getCellDisplayValue(selectionRange.tableId, r, c)));
            }
            lines.push(cells.join("\t"));
        }
        return lines.join("\n");
    }, [selectionRange, getCellDisplayValue]);

    /**
     * 編集可能グリッド (memo_kpi / pl_cum_manual / pl_q_manual) の 1 セル保存を共通化。
     * clearRange / Delete 単一クリア / handleEditablePaste から呼ばれる。
     */
    const saveEditableCell = useCallback((
        tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual",
        rowIdx: number,
        colIdx: number,
        value: string,
    ) => {
        // pl_cum_manual: manualTableMemos.pl_cum[rowIdx][colIdx]
        if (tableId === "pl_cum_manual") {
            onManualMemoEdit?.("pl_cum", rowIdx, colIdx, value);
            return;
        }
        // pl_q_manual: 表示列 colIdx に +2 のオフセット
        if (tableId === "pl_q_manual") {
            onManualMemoEdit?.("pl_q", rowIdx, colIdx + 2, value);
            return;
        }
        // memo_kpi: colIdx 2,3 → memo_a/b、colIdx 4,5,6 → kpi_1/2/3
        if (tableId === "memo_kpi") {
            const row = cumRows[rowIdx];
            if (!row) return;
            if (colIdx === 2 || colIdx === 3) {
                onMemoEdit?.(row.period, row.quarter, colIdx - 2, value);
            } else if (colIdx >= 4 && colIdx <= 6) {
                onKpiValueEdit?.(row.period, row.quarter, colIdx - 3, value, "cum");
            }
            return;
        }
        // cum / q テーブルの KPI 列
        const rows = tableId === "q" ? qRows : cumRows;
        const row = rows[rowIdx];
        if (!row) return;
        const baseCount = tableId === "q" ? Q_BASE_COL_COUNT : CUM_BASE_COL_COUNT;
        if (colIdx >= baseCount && colIdx < baseCount + 3) {
            const slot = colIdx - baseCount + 1;
            const saveTableId: "cum" | "q" = tableId === "q" ? "q" : "cum";
            onKpiValueEdit?.(row.period, row.quarter, slot, value, saveTableId);
        }
        // memo_a / memo_b (cum テーブルのみ)
        if (tableId === "cum") {
            if (colIdx === 8) onMemoEdit?.(row.period, row.quarter, 0, value);
            if (colIdx === 9) onMemoEdit?.(row.period, row.quarter, 1, value);
        }
    }, [cumRows, qRows, onManualMemoEdit, onMemoEdit, onKpiValueEdit]);

    // 範囲クリア (Delete/Backspace) — saveEditableCell で統一
    const clearRange = useCallback(() => {
        if (!selectionRange) return;
        const minRow = Math.min(selectionRange.startRow, selectionRange.endRow);
        const maxRow = Math.max(selectionRange.startRow, selectionRange.endRow);
        const minCol = Math.min(selectionRange.startColIdx, selectionRange.endColIdx);
        const maxCol = Math.max(selectionRange.startColIdx, selectionRange.endColIdx);
        for (let r = minRow; r <= maxRow; r++)
            for (let c = minCol; c <= maxCol; c++)
                saveEditableCell(selectionRange.tableId, r, c, "");
    }, [selectionRange, saveEditableCell]);

    // 編集開始（startManualEditing と同方式）
    const startEditing = useCallback((coord: CellCoord, initialValue?: string) => {
        // justStartedEditingRef を立てる: focusGrid() が textarea の focus を奪うのを防ぐ
        justStartedEditingRef.current = true;
        setTimeout(() => { justStartedEditingRef.current = false; }, 100);
        setEditingCell(coord);
        setActiveCell(coord);
        const val = initialValue !== undefined ? initialValue : "";
        setEditValue(val);
        // 他テーブルの選択状態をクリア（startManualEditing と同方式）
        setActiveManualCell(null);
        setEditingManualCell(null);
        setSelectionRange(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        setTimeout(() => editInputRef.current?.focus(), 0);
    }, [editInputRef]);


    // commitEdit reentrancy guard（blur + keydownでの二重発火防止）
    const isCommittingRef = useRef(false);

    // 編集確定
    const commitEdit = useCallback(() => {
        if (isCommittingRef.current) return;
        if (!editingCell) return;
        // PL メモ編集中は旧 editingCell の commit を実行しない
        if (editingPlMemoCellRef.current) {
            return;
        }
        // 編集開始直後（justStartedEditingRef）なら commit しない
        // mousedown で startEditing → blur 発火 → commit の誤動作防止
        if (justStartedEditingRef.current) {
            return;
        }
        isCommittingRef.current = true;
        const rows = editingCell.tableId === "q" ? qRows : cumRows; // memo_kpi も cumRows
        const row = rows[editingCell.rowIdx];
        if (!row) { isCommittingRef.current = false; return; }

        const key = editingCell.colKey;
        if ((key === "memo_a" || key === "memo_b") && onMemoEdit) {
            const colIdx = key === "memo_a" ? 0 : 1;
            onMemoEdit(row.period, row.quarter, colIdx, editValue);
        } else if (key.startsWith("kpi_") && onKpiValueEdit) {
            const slot = parseInt(key.split("_")[1]);
            const saveTableId: "cum" | "q" = editingCell.tableId === "q" ? "q" : "cum";
            onKpiValueEdit(row.period, row.quarter, slot, editValue, saveTableId);
        }

        setEditingCell(null);
        // 同期的に grid へフォーカスを戻す（commitManualEdit と同方式）
        gridRef.current?.focus();
        isCommittingRef.current = false;
    }, [editingCell, editValue, cumRows, qRows, onMemoEdit, onKpiValueEdit]);

    // 編集キャンセル
    const cancelEdit = useCallback(() => {
        setEditingCell(null);
        focusGrid();
    }, [focusGrid]);


    // ============================================================
    // 手入力メモ専用行 — セル操作関数
    // ============================================================

    /** 手入力メモセルを選択（MEMO A/B の selectCell 相当） */
    const selectManualCell = useCallback((coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }) => {
        setActiveManualCell(coord);
        setActiveCell(null);
        setEditingCell(null);
        setSelectionRange(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        focusGrid();
    }, [focusGrid]);

    /** アクティブな手入力セルの現在値を取得 */
    const getActiveManualCellValue = useCallback((): string => {
        if (!activeManualCell) return "";
        const grid = manualTableMemos?.[activeManualCell.tableType];
        return grid?.[activeManualCell.rowIdx]?.[activeManualCell.colIdx] ?? "";
    }, [activeManualCell, manualTableMemos]);

    /** 手入力メモセル編集開始（startEditing 相当） */
    const startManualEditing = useCallback((
        coord: { tableType: ManualTableType; rowIdx: number; colIdx: number },
        initialValue: string,
    ) => {
        setEditingManualCell(coord);
        setActiveManualCell(coord);
        setManualEditValue(initialValue);
        // 他テーブルの選択状態をクリア（PLセルが残ったままセグメントMEMOに切り替わる問題の修正）
        setActiveCell(null);
        setEditingCell(null);
        setSelectionRange(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        // MEMO A/B と同じ明示的 focus（autoFocus との二重保険）
        setTimeout(() => editInputRef.current?.focus(), 0);
    }, [editInputRef]);
    // ref を最新の startManualEditing で更新（mouseup ハンドラから ref 経由で呼ぶため）
    useEffect(() => { startManualEditingRef.current = startManualEditing; }, [startManualEditing]);

    // ============================================================
    // PL メモ / KPI 専用編集関数
    // セグメントメモ（startManualEditing / commitManualEdit）と完全同構造
    // editingCell / commitEdit / gridRef.focus の既存経路を使わない
    // ============================================================

    /** PL メモ / KPI セル編集開始（startManualEditing と完全同構造） */
    const startPlMemoEditing = useCallback((coord: CellCoord, initialValue: string) => {
        // PL メモ編集中フラグを立てる: focusGrid() が gridRef に focus を奔わないようにする
        editingPlMemoCellRef.current = coord;
        justStartedEditingRef.current = true;
        // blurガード用に編集開始時刻を記録
        plMemoEditStartedAtRef.current = Date.now();
        setEditingPlMemoCell(coord);
        setPlMemoEditValue(initialValue);
        // 他テーブルの選択状態をクリア（startManualEditing と同様）
        setActiveCell(coord);
        setEditingCell(null);
        setSelectionRange(null);
        setActiveManualCell(null);
        setEditingManualCell(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        // textarea がフォーカスされた後にフラグをリセット（それまでは gridRef.focus() をブロック）
        setTimeout(() => {
            (plMemoInputRef.current as HTMLTextAreaElement | null)?.focus();
            justStartedEditingRef.current = false;
        }, 50);
    }, [plMemoInputRef]);

    /**
     * PLメモ・KPI・下メモ欄（pl_cum_manual / pl_q_manual）の編集対象かどうかを判定するヘルパー。
     * この関数が true を返すセルは startPlMemoEditing / commitPlMemoEdit で編集・保存される。
     */
    const isPlMemoEditableCell = useCallback((cell: CellCoord | null): boolean => {
        if (!cell) return false;
        // 下メモ欄テーブルは全列が編集対象
        if (cell.tableId === "pl_cum_manual" || cell.tableId === "pl_q_manual") return true;
        // memo_kpi テーブルは全列が編集対象
        if (cell.tableId === "memo_kpi") return true;
        // cum / q テーブルは memo_a / memo_b / kpi_ 列のみ
        return cell.colKey === "memo_a" || cell.colKey === "memo_b" || cell.colKey.startsWith("kpi_");
    }, []);

    /**
     * activeCell が PL メモ / KPI 列 / 下メモ欄へ移動した瞬間に自動で textarea 編集を開始する。
     * 方向キーで移動→ すぐ textarea 表示→ IME でそのまま入力可能（Excel 風）
     * editingPlMemoCell 編集中は再呼びしない。
     */
    useEffect(() => {
        if (!activeCell) return;
        if (!isPlMemoEditableCell(activeCell)) return;
        // すでに編集中なら再起動しない
        if (editingPlMemoCell) return;
        // 現在値を取得して編集開始
        // pl_cum_manual / pl_q_manual は getCellDisplayValue 経由で manualTableMemos から取得
        const colKey = activeCell.colKey;
        let currentValue = "";
        if (activeCell.tableId === "pl_cum_manual") {
            const colIdx = parseInt(colKey.replace("col_", ""));
            currentValue = manualTableMemos?.pl_cum?.[activeCell.rowIdx]?.[colIdx] ?? "";
        } else if (activeCell.tableId === "pl_q_manual") {
            const colIdx = parseInt(colKey.replace("col_", "")) + 2;
            currentValue = manualTableMemos?.pl_q?.[activeCell.rowIdx]?.[colIdx] ?? "";
        } else {
            const rows = activeCell.tableId === "cum" ? cumRows : qRows;
            const row = rows[activeCell.rowIdx];
            if (row) {
                if (colKey === "memo_a" || colKey === "memo_b") {
                    const memoKey = `${row.period}|${row.quarter}`;
                    const memoColIdx = colKey === "memo_a" ? 0 : 1;
                    currentValue = extractMemoValue(memoMap?.[memoKey], memoColIdx);
                } else if (colKey.startsWith("kpi_")) {
                    const slot = parseInt(colKey.split("_")[1]);
                    const kpiKey = `${activeCell.tableId}|${row.period}|${row.quarter}`;
                    currentValue = kpiValues?.[kpiKey]?.[slot] ?? "";
                }
            }
        }
        startPlMemoEditing(activeCell, currentValue);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeCell]);

    /** PL メモ / KPI セル編集確定 — saveEditableCell で統一 */
    const commitPlMemoEdit = useCallback(() => {
        if (isCommittingPlMemoRef.current) return;
        if (!editingPlMemoCell) return;
        isCommittingPlMemoRef.current = true;
        const key = editingPlMemoCell.colKey;
        const tid = editingPlMemoCell.tableId;
        // colKey → colIdx 変換
        if (tid === "pl_cum_manual" || tid === "pl_q_manual") {
            const colIdx = parseInt(key.replace("col_", ""));
            saveEditableCell(tid, editingPlMemoCell.rowIdx, colIdx, plMemoEditValue);
        } else if (tid === "memo_kpi") {
            // memo_kpi: colKey "memo_a"→2, "memo_b"→3, "kpi_N"→3+N
            let absCol = 0;
            if (key === "memo_a") absCol = 2;
            else if (key === "memo_b") absCol = 3;
            else if (key.startsWith("kpi_")) absCol = 3 + parseInt(key.split("_")[1]);
            saveEditableCell("memo_kpi", editingPlMemoCell.rowIdx, absCol, plMemoEditValue);
        } else {
            // cum / q テーブルの memo_a / memo_b / kpi_
            const rows = tid === "q" ? qRows : cumRows;
            const row = rows[editingPlMemoCell.rowIdx];
            if (row) {
                if ((key === "memo_a" || key === "memo_b") && onMemoEdit) {
                    const colIdx = key === "memo_a" ? 0 : 1;
                    onMemoEdit(row.period, row.quarter, colIdx, plMemoEditValue);
                } else if (key.startsWith("kpi_") && onKpiValueEdit) {
                    const slot = parseInt(key.split("_")[1]);
                    const saveTableId = tid === "q" ? "q" : "cum";
                    onKpiValueEdit(row.period, row.quarter, slot, plMemoEditValue, saveTableId);
                }
            }
        }
        editingPlMemoCellRef.current = null;
        setEditingPlMemoCell(null);
        gridRef.current?.focus();
        isCommittingPlMemoRef.current = false;
    }, [editingPlMemoCell, plMemoEditValue, cumRows, qRows, onMemoEdit, onKpiValueEdit, saveEditableCell]);

    /** PL メモ / KPI セル編集キャンセル（cancelManualEdit と同構造） */
    const cancelPlMemoEdit = useCallback(() => {
        editingPlMemoCellRef.current = null;  // PL メモ編集中フラグをリセット
        setEditingPlMemoCell(null);
        focusGrid();
    }, [focusGrid]);

    /**
     * PL メモ textarea の onBlur ガード。
     * 編集開始から 200ms 以内 かつ relatedTarget=null（フォーカス競合 or spurious blur）の場合は
     * commitPlMemoEdit を呼ばずに return する。
     * Enter / Escape / 別セルクリックによる commit は影響しない。
     */
    const plMemoBlurShouldSkip = useCallback(
        (e: React.FocusEvent<HTMLTextAreaElement | HTMLInputElement>) => {
            const elapsed = Date.now() - plMemoEditStartedAtRef.current;
            if (elapsed < 200 && e.relatedTarget == null) {
                return true;
            }
            return false;
        },
        []
    );

    /**
     * PL メモ / KPI セル mousedown ハンドラ（handleSegManualCellMouseDown と同構造）
     * handleCellMouseDown を通さないため e.preventDefault / setEditingCell(null) が走らない
     */
    const handlePlMemoCellMouseDown = useCallback((
        tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual",
        rowIdx: number,
        colKey: string,
        currentValue: string,
    ) => {
        const coord: CellCoord = { tableId, rowIdx, colKey };
        startPlMemoEditing(coord, currentValue);
    }, [startPlMemoEditing]);



    /** 手入力メモセル編集確定（commitEdit 相当） */
    const commitManualEdit = useCallback(() => {
        if (isCommittingManualRef.current) return;
        if (!editingManualCell) return;
        isCommittingManualRef.current = true;
        onManualMemoEdit?.(
            editingManualCell.tableType,
            editingManualCell.rowIdx,
            editingManualCell.colIdx,
            manualEditValue,
        );
        setEditingManualCell(null);
        // 同期的に grid へフォーカスを戻す（rAF 遅延を使わない）
        // → Enter 直後の Arrow キーが確実に handleTableKeyDown に届くようにする
        gridRef.current?.focus();
        isCommittingManualRef.current = false;
    }, [editingManualCell, manualEditValue, onManualMemoEdit]);

    /** 手入力メモセル編集キャンセル（cancelEdit 相当） */
    const cancelManualEdit = useCallback(() => {
        setEditingManualCell(null);
        focusGrid();
    }, [focusGrid]);

    /** 手入力メモセル Tab 移動 */
    const moveManualActiveCell = useCallback((delta: number) => {
        if (!activeManualCell) return;
        const colCounts: Record<ManualTableType, number> = {
            pl_cum:         CUM_BASE_COL_COUNT,
            pl_q:           Q_BASE_COL_COUNT - 2,
            segment_cum:    2 + segmentColumns.length * 2,
            segment_q:      2 + segmentColumns.length * 2,
            segment_manual: SEGMENT_MANUAL_COL_COUNT,  // 固定12列
        };
        const colCount = colCounts[activeManualCell.tableType];
        // segment_manual: 行数は cumRows に連動
        const rowCount = activeManualCell.tableType === "segment_manual"
            ? cumRows.length
            : getManualRowCount(activeManualCell.tableType);
        const totalCells = rowCount * colCount;
        const flat = activeManualCell.rowIdx * colCount + activeManualCell.colIdx + delta;
        const clampedFlat = Math.max(0, Math.min(totalCells - 1, flat));
        const newRowIdx = Math.floor(clampedFlat / colCount);
        const newColIdx = clampedFlat % colCount;
        const nextCoord = { tableType: activeManualCell.tableType, rowIdx: newRowIdx, colIdx: newColIdx };
        setActiveManualCell(nextCoord);
        // segment_manual: Tab移動後も即編集状態にする（IME対策含む）
        if (activeManualCell.tableType === "segment_manual") {
            const currentValue = manualTableMemosRef.current?.segment_manual?.[newRowIdx]?.[newColIdx] ?? "";
            requestAnimationFrame(() => {
                startManualEditingRef.current?.(nextCoord, currentValue);
            });
        } else {
            focusGrid();
        }
    }, [activeManualCell, segmentColumns.length, cumRows.length, focusGrid]);

    /** 手入力メモセル: Arrow Key 方向移動（素直クランプ、折り返しなし） */
    const moveManualActiveCellDir = useCallback((dRow: number, dCol: number) => {
        if (!activeManualCell) return;
        const colCounts: Record<ManualTableType, number> = {
            pl_cum:         CUM_BASE_COL_COUNT,
            pl_q:           Q_BASE_COL_COUNT - 2,
            segment_cum:    2 + segmentColumns.length * 2,
            segment_q:      2 + segmentColumns.length * 2,
            segment_manual: SEGMENT_MANUAL_COL_COUNT,  // 固定12列
        };
        const colCount = colCounts[activeManualCell.tableType];
        // segment_manual: 行数は cumRows に連動
        const rowCount = activeManualCell.tableType === "segment_manual"
            ? cumRows.length
            : getManualRowCount(activeManualCell.tableType);
        const newRowIdx = Math.max(0, Math.min(rowCount - 1, activeManualCell.rowIdx + dRow));
        const newColIdx = Math.max(0, Math.min(colCount - 1, activeManualCell.colIdx + dCol));
        const nextCoord = { tableType: activeManualCell.tableType, rowIdx: newRowIdx, colIdx: newColIdx };
        setActiveManualCell(nextCoord);
        // segment_manual: 移動先でも即編集状態にする（IME対策含む）
        if (activeManualCell.tableType === "segment_manual") {
            const currentValue = manualTableMemosRef.current?.segment_manual?.[newRowIdx]?.[newColIdx] ?? "";
            requestAnimationFrame(() => {
                startManualEditingRef.current?.(nextCoord, currentValue);
            });
        } else {
            focusGrid();
        }
    }, [activeManualCell, segmentColumns.length, cumRows.length, focusGrid]);

    // 隣セルへ移動 (memo + kpi 統合)
    const moveActiveCell = useCallback((dRow: number, dCol: number) => {
        if (!activeCell) return;
        // pl_cum_manual: 8列 × MANUAL_MEMO_ROW_COUNT行
        if (activeCell.tableId === "pl_cum_manual") {
            const curColIdx = parseInt(activeCell.colKey.replace("col_", ""));
            const newRow = Math.max(0, Math.min(MANUAL_MEMO_ROW_COUNT - 1, activeCell.rowIdx + dRow));
            const newCol = Math.max(0, Math.min(CUM_BASE_COL_COUNT - 1, curColIdx + dCol));
            setActiveCell({ tableId: "pl_cum_manual", rowIdx: newRow, colKey: `col_${newCol}` });
            setSelectionRange(null);
            return;
        }
        // pl_q_manual: 6列(colOffset=2で実際は2〜7) × MANUAL_MEMO_ROW_COUNT行
        if (activeCell.tableId === "pl_q_manual") {
            const curColIdx = parseInt(activeCell.colKey.replace("col_", ""));
            const newRow = Math.max(0, Math.min(MANUAL_MEMO_ROW_COUNT - 1, activeCell.rowIdx + dRow));
            const newCol = Math.max(0, Math.min((Q_BASE_COL_COUNT - 2) - 1, curColIdx + dCol));
            setActiveCell({ tableId: "pl_q_manual", rowIdx: newRow, colKey: `col_${newCol}` });
            setSelectionRange(null);
            return;
        }
        const rows = activeCell.tableId === "cum" ? cumRows : qRows;
        const editableCols = EDITABLE_COLS;
        const curColIdx = editableCols.indexOf(activeCell.colKey);
        if (curColIdx < 0) return;

        let newRow = activeCell.rowIdx + dRow;
        let newCol = curColIdx + dCol;

        // 行折り返し
        if (newCol < 0) { newRow--; newCol = editableCols.length - 1; }
        if (newCol >= editableCols.length) { newRow++; newCol = 0; }

        if (newRow < 0 || newRow >= rows.length) return;

        selectCell({
            tableId: activeCell.tableId,
            rowIdx: newRow,
            colKey: editableCols[newCol],
        });
    }, [activeCell, cumRows, qRows, selectCell]);

    /** PL メモ / KPI 編集中 Arrow キー（handleSegManualArrowKey と同構造、moveActiveCell の後に定義） */
    const handlePlMemoArrowKey = useCallback((dir: "up" | "down" | "left" | "right") => {
        // 編集開始直後 120ms 以内 かつ activeElement が textarea/input の場合は誤発火とみなし skip
        // 120ms 以降の Arrow は「Enter 確定して隔セル移動」の正常処理として通す
        const elapsed = Date.now() - plMemoEditStartedAtRef.current;
        if (elapsed < 120 && editingPlMemoCellRef.current) {
            const activeEl = document.activeElement as HTMLElement | null;
            const isTextEditingTarget =
                activeEl != null && (
                    activeEl.tagName === 'TEXTAREA' ||
                    activeEl.tagName === 'INPUT' ||
                    activeEl.isContentEditable
                );
            if (isTextEditingTarget) {
                return;
            }
        }
        commitPlMemoEdit();
        const dr = dir === "up" ? -1 : dir === "down" ? 1 : 0;
        const dc = dir === "left" ? -1 : dir === "right" ? 1 : 0;
        requestAnimationFrame(() => moveActiveCell(dr, dc));
    }, [commitPlMemoEdit, moveActiveCell]);

    // セグメントセル移動
    const moveActiveSegCell = useCallback((dRow: number, dCol: number) => {
        if (!activeSegCell) return;
        const totalSegCols = segmentColumns.length * 2;
        let newRow = activeSegCell.rowIdx + dRow;
        let newCol = activeSegCell.colIdx + dCol;

        // 列折り返し
        if (newCol < 0) { newRow--; newCol = totalSegCols - 1; }
        if (newCol >= totalSegCols) { newRow++; newCol = 0; }

        if (newRow < 0 || newRow >= cumRows.length) return;

        setActiveSegCell({ rowIdx: newRow, colIdx: newCol });
    }, [activeSegCell, segmentColumns.length, cumRows.length]);

    // セグメントセル: 親から編集開始
    const startSegEditing = useCallback((coord: SegCellCoord, initialValue: string) => {
        setEditingSegCell(coord);
        setSegEditValue(initialValue);
    }, []);

    // セグメントセル: 編集完了（保存は SegOverrideCell 内で実行、親は状態リセットのみ）
    const finishSegEditing = useCallback(() => {
        setEditingSegCell(null);
        setSegEditValue("");
        requestAnimationFrame(() => gridRef.current?.focus());
    }, []);

    // フォーミュラバーからの編集
    const handleFormulaBarChange = useCallback((value: string) => {
        if (!activeCell) return;
        const rows = activeCell.tableId === "q" ? qRows : cumRows; // memo_kpi も cumRows
        const row = rows[activeCell.rowIdx];
        if (!row) return;
        const key = activeCell.colKey;
        if ((key === "memo_a" || key === "memo_b") && onMemoEdit) {
            const colIdx = key === "memo_a" ? 0 : 1;
            onMemoEdit(row.period, row.quarter, colIdx, value);
        } else if (key.startsWith("kpi_") && onKpiValueEdit) {
            const slot = parseInt(key.split("_")[1]);
            onKpiValueEdit(row.period, row.quarter, slot, value);
        }
    }, [activeCell, cumRows, qRows, onMemoEdit, onKpiValueEdit]);

    // segment_manual 範囲 or 単一セルを TSV 文字列に変換してコピー用に返す。
    // 該当セルがなければ null を返す。
    const getSegManualTsv = useCallback((): string | null => {
        const grid = manualTableMemos?.segment_manual ?? [];
        if (segManualSel) {
            const minRow = Math.min(segManualSel.startRow, segManualSel.endRow);
            const maxRow = Math.max(segManualSel.startRow, segManualSel.endRow);
            const minCol = Math.min(segManualSel.startCol, segManualSel.endCol);
            const maxCol = Math.max(segManualSel.startCol, segManualSel.endCol);
            const lines: string[] = [];
            for (let r = minRow; r <= maxRow; r++) {
                const cells: string[] = [];
                for (let c = minCol; c <= maxCol; c++) {
                    cells.push(quoteTsvCell(grid[r]?.[c] ?? ""));
                }
                lines.push(cells.join("\t"));
            }
            return lines.join("\n");
        }
        if (activeManualCell?.tableType === "segment_manual") {
            const val = grid[activeManualCell.rowIdx]?.[activeManualCell.colIdx] ?? "";
            return quoteTsvCell(val);
        }
        return null;
    }, [segManualSel, activeManualCell, manualTableMemos]);

    // キーボードイベント（テーブル全体）
    const handleTableKeyDown = useCallback((e: React.KeyboardEvent) => {
        // --- 上部テキストバー（formula-bar）編集中は Arrow キーをセル移動に渡さない ---
        const target = e.target as HTMLElement | null;
        const isFormulaBarEditing =
            target &&
            (
                target.classList.contains("formula-bar-input") ||
                !!target.closest?.(".formula-bar")
            );
        if (isFormulaBarEditing) return;

        // IME変換中（文字確定中）は Arrow / Enter / Escape / Tab をグリッド操作に使わない
        const isComposing = e.nativeEvent.isComposing || e.key === "Process" || e.keyCode === 229;

        // IME変換中（候補選択中）: グリッド操作キーを全て無視
        if (isComposing) return;

        // --- セグメントセルがアクティブの場合 ---
        if (activeSegCell && !editingSegCell) {
            if (e.key === "ArrowUp") { e.preventDefault(); moveActiveSegCell(-1, 0); }
            else if (e.key === "ArrowDown") { e.preventDefault(); moveActiveSegCell(1, 0); }
            else if (e.key === "ArrowLeft") { e.preventDefault(); moveActiveSegCell(0, -1); }
            else if (e.key === "ArrowRight") { e.preventDefault(); moveActiveSegCell(0, 1); }
            else if (e.key === "Tab") { e.preventDefault(); moveActiveSegCell(0, e.shiftKey ? -1 : 1); }
            else if (e.key === "Enter" || e.key === "F2") {
                e.preventDefault();
                // 現在のセルの値を取得して編集開始
                startSegEditing(activeSegCell, "");
            }
            // 数字・マイナス・ドット → 直接入力で編集開始
            else if (/^[0-9.\-]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
                e.preventDefault();
                startSegEditing(activeSegCell, e.key);
            }
            else if (e.key === "Escape") {
                e.preventDefault();
                setActiveSegCell(null);
            }
            return;
        }

        // セグメント編集中はスキップ（input 側で処理）
        if (editingSegCell) return;

        // --- 手入力メモ専用行がアクティブの場合（activeCell / editingCell がない場合のみ） ---
        if (activeManualCell && !activeCell && !editingCell && !editingManualCell) {
            // Ctrl+Z: Undo
            if ((e.ctrlKey || e.metaKey) && e.key === "z" && !e.shiftKey) {
                e.preventDefault(); onUndo?.(); return;
            }
            // Ctrl+C: segment_manual 範囲コピー
            if ((e.ctrlKey || e.metaKey) && e.key === "c" && !e.shiftKey
                && activeManualCell.tableType === "segment_manual") {
                e.preventDefault();
                const tsv = getSegManualTsv();
                if (tsv !== null) navigator.clipboard.writeText(tsv).catch(console.error);
                return;
            }
            // Delete/Backspace: クリア（範囲選択中は全セルクリア）
            if (e.key === "Delete" || e.key === "Backspace") {
                e.preventDefault();
                if (segManualSel && activeManualCell.tableType === "segment_manual") {
                    const minRow = Math.min(segManualSel.startRow, segManualSel.endRow);
                    const maxRow = Math.max(segManualSel.startRow, segManualSel.endRow);
                    const minCol = Math.min(segManualSel.startCol, segManualSel.endCol);
                    const maxCol = Math.max(segManualSel.startCol, segManualSel.endCol);
                    for (let r = minRow; r <= maxRow; r++)
                        for (let c = minCol; c <= maxCol; c++)
                            onManualMemoEdit?.("segment_manual", r, c, "");
                } else {
                    onManualMemoEdit?.(activeManualCell.tableType, activeManualCell.rowIdx, activeManualCell.colIdx, "");
                }
                return;
            }
            // Tab: 次セルへ移動（選択解除）
            if (e.key === "Tab") {
                e.preventDefault();
                setSegManualSel(null);
                moveManualActiveCell(e.shiftKey ? -1 : 1);
                return;
            }
            // Enter / F2: 既存値を維持して編集開始（選択解除）
            if (e.key === "Enter" || e.key === "F2") {
                e.preventDefault();
                setSegManualSel(null);
                startManualEditing(activeManualCell, getActiveManualCellValue());
                return;
            }
            // Escape: 選択解除 + 範囲解除
            if (e.key === "Escape") {
                e.preventDefault();
                setSegManualSel(null);
                setActiveManualCell(null);
                focusGrid();
                return;
            }
            // Shift+Arrow: segment_manual 範囲拡張
            if (e.shiftKey && activeManualCell.tableType === "segment_manual" &&
                (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowDown")) {
                e.preventDefault();
                const dr = e.key === "ArrowUp" ? -1 : e.key === "ArrowDown" ? 1 : 0;
                const dc = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
                setSegManualSel(prev => {
                    const base = prev ?? {
                        startRow: activeManualCell.rowIdx, startCol: activeManualCell.colIdx,
                        endRow: activeManualCell.rowIdx, endCol: activeManualCell.colIdx,
                    };
                    return {
                        ...base,
                        endRow: Math.max(0, Math.min(cumRows.length - 1, base.endRow + dr)),
                        endCol: Math.max(0, Math.min(SEGMENT_MANUAL_COL_COUNT - 1, base.endCol + dc)),
                    };
                });
                return;
            }
            // ArrowUp/Down/Left/Right: 方向移動（範囲解除）
            if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowDown") {
                e.preventDefault();
                setSegManualSel(null);
                if (e.key === "ArrowLeft")  moveManualActiveCellDir(0, -1);
                if (e.key === "ArrowRight") moveManualActiveCellDir(0,  1);
                if (e.key === "ArrowUp")    moveManualActiveCellDir(-1, 0);
                if (e.key === "ArrowDown")  moveManualActiveCellDir(1,  0);
                return;
            }
            return;
        }

        // 手入力メモ編集中はスキップ（input 側で処理）
        if (editingManualCell) return;

        // --- メモ / KPI セルがアクティブの場合 ---
        // selectionRange がある場合は activeCell なしでも Ctrl+C / Delete を処理
        if (!activeCell) {
            if (selectionRange) {
                // Ctrl+C: 範囲コピー
                if ((e.ctrlKey || e.metaKey) && e.key === "c" && !e.shiftKey) {
                    e.preventDefault();
                    const tsv = getRangeAsTsv();
                    if (tsv != null) navigator.clipboard.writeText(tsv).catch(console.error);
                    return;
                }
                // Delete/Backspace: 範囲クリア
                if (e.key === "Delete" || e.key === "Backspace") {
                    e.preventDefault();
                    clearRange();
                    return;
                }
            }
            return;
        }

        // 編集中の処理は input 側の onKeyDown で処理するため、ここではスキップ
        if (editingCell) return;

        // Ctrl+Z: Undo
        if ((e.ctrlKey || e.metaKey) && e.key === "z" && !e.shiftKey) {
            e.preventDefault();
            onUndo?.();
            return;
        }

        // Ctrl+C: 範囲コピー
        if ((e.ctrlKey || e.metaKey) && e.key === "c" && !e.shiftKey) {
            if (selectionRange) {
                e.preventDefault();
                const tsv = getRangeAsTsv();
                if (tsv != null) navigator.clipboard.writeText(tsv).catch(console.error);
                return;
            }
            // 単一セルの場合も value をコピー
            if (activeCell) {
                e.preventDefault();
                const val = getActiveCellValue();
                if (val != null) navigator.clipboard.writeText(quoteTsvCell(val)).catch(console.error);
                return;
            }
        }

        // Delete/Backspace: 範囲クリア or 単一セルクリア（saveEditableCell で統一）
        if (e.key === "Delete" || e.key === "Backspace") {
            e.preventDefault();
            if (selectionRange) {
                clearRange();
                return;
            }
            // 単一セルクリア: colKey → colIdx に変換して saveEditableCell に委譲
            if (activeCell.tableId === "pl_cum_manual" || activeCell.tableId === "pl_q_manual") {
                const colIdx = parseInt(activeCell.colKey.replace("col_", ""));
                saveEditableCell(activeCell.tableId, activeCell.rowIdx, colIdx, "");
            } else if (activeCell.tableId === "memo_kpi") {
                // memo_kpi の colKey は "memo_a" / "memo_b" / "kpi_N" — 絶対列番号に変換
                const key = activeCell.colKey;
                let absCol = 0;
                if (key === "memo_a") absCol = 2;
                else if (key === "memo_b") absCol = 3;
                else if (key.startsWith("kpi_")) absCol = 3 + parseInt(key.split("_")[1]);
                saveEditableCell("memo_kpi", activeCell.rowIdx, absCol, "");
            } else {
                // cum / q テーブル
                const key = activeCell.colKey;
                if (key === "memo_a" || key === "memo_b") {
                    const rows = activeCell.tableId === "cum" ? cumRows : qRows;
                    const row = rows[activeCell.rowIdx];
                    if (row && onMemoEdit) {
                        const colIdx = key === "memo_a" ? 0 : 1;
                        onMemoEdit(row.period, row.quarter, colIdx, "");
                    }
                } else if (key.startsWith("kpi_") && onKpiValueEdit) {
                    const rows = activeCell.tableId === "cum" ? cumRows : qRows;
                    const row = rows[activeCell.rowIdx];
                    if (row) {
                        const slot = parseInt(key.split("_")[1]);
                        onKpiValueEdit(row.period, row.quarter, slot, "");
                    }
                }
            }
            return;
        }

        // 非編集中: 矢印キーで移動
        if (e.key === "ArrowUp") { e.preventDefault(); setSelectionRange(null); moveActiveCell(-1, 0); }
        else if (e.key === "ArrowDown") { e.preventDefault(); setSelectionRange(null); moveActiveCell(1, 0); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); setSelectionRange(null); moveActiveCell(0, -1); }
        else if (e.key === "ArrowRight") { e.preventDefault(); setSelectionRange(null); moveActiveCell(0, 1); }
        else if (e.key === "Tab") { e.preventDefault(); setSelectionRange(null); moveActiveCell(0, e.shiftKey ? -1 : 1); }
        else if (e.key === "Enter") {
            e.preventDefault();
            setSelectionRange(null);
            const val = getActiveCellValue();
            // pl_cum_manual / pl_q_manual / memo_kpi / memo_a / memo_b / kpi_ は startPlMemoEditing
            if (isPlMemoEditableCell(activeCell)) {
                startPlMemoEditing(activeCell, val);
            } else {
                startEditing(activeCell, val);
            }
        }
        else if (e.key === "F2") {
            e.preventDefault();
            setSelectionRange(null);
            const val = getActiveCellValue();
            if (isPlMemoEditableCell(activeCell)) {
                startPlMemoEditing(activeCell, val);
            } else {
                startEditing(activeCell, val);
            }
        }
        // Escape: 範囲解除
        else if (e.key === "Escape") {
            e.preventDefault();
            setSelectionRange(null);
        }
        // 印字可能文字: PL メモ / KPI / 下メモ欄セルは方向キー移動の瞬間に useEffect で自動編集開始するため、ここでは何もしない
    }, [activeCell, activeManualCell, activeSegCell, editingCell, editingManualCell, editingSegCell, isPlMemoEditableCell, moveActiveCell, moveActiveSegCell, moveManualActiveCell, moveManualActiveCellDir, startEditing, startPlMemoEditing, startManualEditing, startSegEditing, getActiveCellValue, getActiveManualCellValue, cumRows, qRows, onMemoEdit, onKpiValueEdit, onManualMemoEdit, saveEditableCell, selectionRange, getRangeAsTsv, clearRange, focusGrid, onUndo, segManualSel, getSegManualTsv]);

    // 編集可能グリッド統合ペースト (memo_kpi / pl_cum_manual / pl_q_manual / cum / q 全対応)
    const handleEditablePaste = useCallback(
        (tableId: "cum" | "q" | "memo_kpi" | "pl_cum_manual" | "pl_q_manual", startColIdx: number, startRowIdx: number, e: React.ClipboardEvent) => {
            e.preventDefault();
            e.stopPropagation();
            const text = e.clipboardData.getData("text/plain");
            if (text == null) return;

            let parsed = parseTsvClipboard(text);

            // Excel等の空白セル1個コピー対策:
            // text が "" または改行のみの場合も空白1セルとして扱う
            if (parsed.length === 0) {
                if (text === "" || /^[\r\n]+$/.test(text)) {
                    parsed = [[""]];
                } else {
                    return;
                }
            }

            // --- pl_cum_manual / pl_q_manual: saveEditableCell で保存 ---
            if (tableId === "pl_cum_manual" || tableId === "pl_q_manual") {
                const maxCols = tableId === "pl_cum_manual" ? CUM_BASE_COL_COUNT : (Q_BASE_COL_COUNT - 2);
                for (let r = 0; r < parsed.length; r++) {
                    const targetRow = startRowIdx + r;
                    if (targetRow >= MANUAL_MEMO_ROW_COUNT) break;
                    for (let c = 0; c < parsed[r].length; c++) {
                        const displayCol = startColIdx + c;
                        if (displayCol >= maxCols) break;
                        saveEditableCell(tableId, targetRow, displayCol, parsed[r][c]);
                    }
                }
                return;
            }

            // --- memo_kpi / cum / q: 既存の memo + kpi 列ベースのペースト ---
            const rows = tableId === "q" ? qRows : cumRows;
            const availableCols = tableId === "q" ? [...KPI_COLS] : EDITABLE_COLS;
            const kpiSaveTableId: "cum" | "q" = tableId === "q" ? "q" : "cum";

            const memoEdits: { period: string; quarter: string; colIdx: number; value: string }[] = [];
            const kpiEdits: { period: string; quarter: string; slot: number; value: string; tableId: "cum" | "q" }[] = [];

            for (let r = 0; r < parsed.length; r++) {
                const targetRowIdx = startRowIdx + r;
                if (targetRowIdx >= rows.length) break;
                const row = rows[targetRowIdx];
                for (let c = 0; c < parsed[r].length; c++) {
                    const colPos = startColIdx + c;
                    if (colPos >= availableCols.length) break;
                    const colKey = availableCols[colPos];
                    const value = parsed[r][c];

                    if (colKey === "memo_a" || colKey === "memo_b") {
                        memoEdits.push({
                            period: row.period,
                            quarter: row.quarter,
                            colIdx: colKey === "memo_a" ? 0 : 1,
                            value,
                        });
                    } else if (colKey.startsWith("kpi_")) {
                        const slot = parseInt(colKey.split("_")[1]);
                        kpiEdits.push({ period: row.period, quarter: row.quarter, slot, value, tableId: kpiSaveTableId });
                    }
                }
            }

            // memo列の一括保存
            if (memoEdits.length > 0 && onMemoPaste) {
                onMemoPaste(memoEdits);
            }
            // kpi列の保存 (1セルずつ)
            if (kpiEdits.length > 0 && onKpiValueEdit) {
                for (const edit of kpiEdits) {
                    onKpiValueEdit(edit.period, edit.quarter, edit.slot, edit.value, edit.tableId as "cum" | "q");
                }
            }
        },
        [cumRows, qRows, onMemoPaste, onKpiValueEdit, saveEditableCell]
    );

    /**
     * pl_cum_manual ペーストハンドラを事前生成。
     * [rowIdx][colIdx] → (e: ClipboardEvent) => void のマップ。
     * handleEditablePaste が安定参照のため、マウント時に1回だけ生成される。
     */
    const plCumManualPasteHandlers = useMemo<((e: React.ClipboardEvent) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: CUM_BASE_COL_COUNT }, (_, colIdx) =>
                    (e: React.ClipboardEvent) => handleEditablePaste("pl_cum_manual", colIdx, rowIdx, e)
                )
            ),
        [handleEditablePaste]
    );

    /**
     * pl_q_manual ペーストハンドラを事前生成。
     * [rowIdx][displayCol] → (e: ClipboardEvent) => void のマップ。
     */
    const plQManualPasteHandlers = useMemo<((e: React.ClipboardEvent) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: Q_BASE_COL_COUNT - 2 }, (_, displayCol) =>
                    (e: React.ClipboardEvent) => handleEditablePaste("pl_q_manual", displayCol, rowIdx, e)
                )
            ),
        [handleEditablePaste]
    );

    /**
     * pl_cum_manual onMouseEnter / onMouseEnterRange ハンドラを事前生成。
     * [rowIdx][colIdx] → () => void のマップ。両 props が同じ関数を参照する。
     */
    const plCumManualMouseEnterHandlers = useMemo<(() => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: CUM_BASE_COL_COUNT }, (_, colIdx) =>
                    () => handleCellMouseEnter("pl_cum_manual", rowIdx, colIdx)
                )
            ),
        [handleCellMouseEnter]
    );

    /**
     * pl_q_manual onMouseEnter / onMouseEnterRange ハンドラを事前生成。
     * [rowIdx][displayCol] → () => void のマップ。
     */
    const plQManualMouseEnterHandlers = useMemo<(() => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: Q_BASE_COL_COUNT - 2 }, (_, displayCol) =>
                    () => handleCellMouseEnter("pl_q_manual", rowIdx, displayCol)
                )
            ),
        [handleCellMouseEnter]
    );

    /**
     * pl_cum_manual onStartEdit ハンドラを事前生成。
     * [rowIdx][colIdx] → (val: string) => void のマップ。
     * colKey = `col_${colIdx}` で固定のため、マップ内で計算済み。
     */
    const plCumManualStartEditHandlers = useMemo<((val: string) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: CUM_BASE_COL_COUNT }, (_, colIdx) => {
                    const colKey = `col_${colIdx}`;
                    return (val: string) => handlePlMemoCellMouseDown("pl_cum_manual", rowIdx, colKey, val);
                })
            ),
        [handlePlMemoCellMouseDown]
    );

    /**
     * pl_q_manual onStartEdit ハンドラを事前生成。
     * [rowIdx][displayCol] → (val: string) => void のマップ。
     */
    const plQManualStartEditHandlers = useMemo<((val: string) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: Q_BASE_COL_COUNT - 2 }, (_, displayCol) => {
                    const colKey = `col_${displayCol}`;
                    return (val: string) => handlePlMemoCellMouseDown("pl_q_manual", rowIdx, colKey, val);
                })
            ),
        [handlePlMemoCellMouseDown]
    );

    /**
     * pl_cum_manual onMouseDownCaptureEdit ハンドラを事前生成。
     * [rowIdx][colIdx] → (e: MouseEvent, cellValue: string) => void のマップ。
     * cellValue はレンダー時に確定するため JSX 側でバインドする。
     */
    const plCumManualCaptureHandlers = useMemo<((e: React.MouseEvent, cellValue: string) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: CUM_BASE_COL_COUNT }, (_, colIdx) => {
                    const colKey = `col_${colIdx}`;
                    return (e: React.MouseEvent, cellValue: string) => {
                        handleCellMouseDown("pl_cum_manual", rowIdx, colIdx, e);
                        pendingPlMemoClick.current = () =>
                            handlePlMemoCellMouseDown("pl_cum_manual", rowIdx, colKey, cellValue);
                    };
                })
            ),
        [handleCellMouseDown, handlePlMemoCellMouseDown]
    );

    /**
     * pl_q_manual onMouseDownCaptureEdit ハンドラを事前生成。
     * [rowIdx][displayCol] → (e: MouseEvent, cellValue: string) => void のマップ。
     */
    const plQManualCaptureHandlers = useMemo<((e: React.MouseEvent, cellValue: string) => void)[][]>(
        () =>
            Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) =>
                Array.from({ length: Q_BASE_COL_COUNT - 2 }, (_, displayCol) => {
                    const colKey = `col_${displayCol}`;
                    return (e: React.MouseEvent, cellValue: string) => {
                        handleCellMouseDown("pl_q_manual", rowIdx, displayCol, e);
                        pendingPlMemoClick.current = () =>
                            handlePlMemoCellMouseDown("pl_q_manual", rowIdx, colKey, cellValue);
                    };
                })
            ),
        [handleCellMouseDown, handlePlMemoCellMouseDown]
    );

    // ── memo_kpi テーブル (memo_a / memo_b / kpi_x) ──────────────────────────
    // cumRows は銘柄切替で長さが変わるため deps に含める

    /** memo_a onMouseEnter / onMouseEnterRange [idx] → () => void */
    const memoAMouseEnterHandlers = useMemo<(() => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                () => handleCellMouseEnter("memo_kpi", idx, 2)
            ),
        [cumRows.length, handleCellMouseEnter]
    );

    /** memo_b onMouseEnter / onMouseEnterRange [idx] → () => void */
    const memoBMouseEnterHandlers = useMemo<(() => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                () => handleCellMouseEnter("memo_kpi", idx, 3)
            ),
        [cumRows.length, handleCellMouseEnter]
    );

    /** kpi_x onMouseEnter / onMouseEnterRange [idx][slot] → () => void (slot=1/2/3) */
    const kpiMouseEnterHandlers = useMemo<(() => void)[][]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                KPI_SLOTS.map((slot) =>
                    () => handleCellMouseEnter("memo_kpi", idx, 3 + slot)
                )
            ),
        [cumRows.length, handleCellMouseEnter]
    );

    /** memo_a onStartEdit [idx] → (val: string) => void */
    const memoAStartEditHandlers = useMemo<((val: string) => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                (val: string) => handlePlMemoCellMouseDown("memo_kpi", idx, "memo_a", val)
            ),
        [cumRows.length, handlePlMemoCellMouseDown]
    );

    /** memo_b onStartEdit [idx] → (val: string) => void */
    const memoBStartEditHandlers = useMemo<((val: string) => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                (val: string) => handlePlMemoCellMouseDown("memo_kpi", idx, "memo_b", val)
            ),
        [cumRows.length, handlePlMemoCellMouseDown]
    );

    /** kpi_x onStartEdit [idx][slotIndex(0-2)] → (val: string) => void */
    const kpiStartEditHandlers = useMemo<((val: string) => void)[][]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                KPI_SLOTS.map((slot) => {
                    const colKey = `kpi_${slot}`;
                    return (val: string) => handlePlMemoCellMouseDown("memo_kpi", idx, colKey, val);
                })
            ),
        [cumRows.length, handlePlMemoCellMouseDown]
    );

    /**
     * memo_a onMouseDownCaptureEdit [idx] → (e, cellValue) => void。
     * cellValue = memoA はレンダー時確定のため JSX 側でバインド。
     */
    const memoACaptureHandlers = useMemo<((e: React.MouseEvent, cellValue: string) => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                (e: React.MouseEvent, cellValue: string) => {
                    handleCellMouseDown("memo_kpi", idx, 2, e);
                    pendingPlMemoClick.current = () =>
                        handlePlMemoCellMouseDown("memo_kpi", idx, "memo_a", cellValue);
                }
            ),
        [cumRows.length, handleCellMouseDown, handlePlMemoCellMouseDown]
    );

    /**
     * memo_b onMouseDownCaptureEdit [idx] → (e, cellValue) => void。
     */
    const memoBCaptureHandlers = useMemo<((e: React.MouseEvent, cellValue: string) => void)[]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                (e: React.MouseEvent, cellValue: string) => {
                    handleCellMouseDown("memo_kpi", idx, 3, e);
                    pendingPlMemoClick.current = () =>
                        handlePlMemoCellMouseDown("memo_kpi", idx, "memo_b", cellValue);
                }
            ),
        [cumRows.length, handleCellMouseDown, handlePlMemoCellMouseDown]
    );

    /**
     * kpi_x onMouseDownCaptureEdit [idx][slotIndex(0-2)] → (e, cellValue) => void。
     */
    const kpiCaptureHandlers = useMemo<((e: React.MouseEvent, cellValue: string) => void)[][]>(
        () =>
            Array.from({ length: cumRows.length }, (_, idx) =>
                KPI_SLOTS.map((slot) => {
                    const colKey = `kpi_${slot}`;
                    const kpiAbsCol = 3 + slot;
                    return (e: React.MouseEvent, cellValue: string) => {
                        handleCellMouseDown("memo_kpi", idx, kpiAbsCol, e);
                        pendingPlMemoClick.current = () =>
                            handlePlMemoCellMouseDown("memo_kpi", idx, colKey, cellValue);
                    };
                })
            ),
        [cumRows.length, handleCellMouseDown, handlePlMemoCellMouseDown]
    );

    // セグメント値取得
    const getSegValue = useCallback(
        (period: string, quarter: string, key: string): number | null => {
            const normP = normalizePeriod(period);
            const normQ = normalizeQuarter(quarter);
            const mapKey = `${normP}|${normQ}`;
            const row = segmentMap.get(mapKey);
            if (!row) return null;
            return row[key] ?? null;
        },
        [segmentMap]
    );

    // Q単体セグメント値取得
    const getSegQValue = useCallback(
        (period: string, quarter: string, key: string): number | null => {
            const normP = normalizePeriod(period);
            const normQ = normalizeQuarter(quarter);
            const mapKey = `${normP}|${normQ}`;
            const row = segmentQMap.get(mapKey);
            if (!row) return null;
            return row[key] ?? null;
        },
        [segmentQMap]
    );

    // PLリサイズハンドラ
    const handleResizeMouseDown = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        resizing.current = true;
        resizeStartY.current = e.clientY;
        resizeStartHeight.current = plHeight;

        const onMouseMove = (ev: MouseEvent) => {
            if (!resizing.current) return;
            const dy = ev.clientY - resizeStartY.current;
            const newH = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, resizeStartHeight.current + dy));
            setPlHeight(newH);
        };

        const onMouseUp = () => {
            resizing.current = false;
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
            // 保存
            setPlHeight((h) => {
                localStorage.setItem(PL_HEIGHT_KEY, String(h));
                return h;
            });
        };

        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
    }, [plHeight]);

    // --- Ctrl+V on active cell ---

    /** segment_manual セルのマウスダウン — ドラッグ範囲選択の開始 */
    const handleSegManualCellMouseDown = useCallback((rowIdx: number, colIdx: number) => {
        segManualIsDragging.current = true;
        segManualDragMoved.current = false;
        segManualDragStartRef.current = { rowIdx, colIdx };
        const coord = { tableType: "segment_manual" as ManualTableType, rowIdx, colIdx };
        // アクティブセルとして登録（キーボードイベント受け取りのため）
        setActiveManualCell(coord);
        setActiveCell(null);
        setSelectionRange(null);
        setActiveSegCell(null);
        setEditingSegCell(null);
        // 範囲選択の初期化（drag 開始点）
        setSegManualSel({ startRow: rowIdx, startCol: colIdx, endRow: rowIdx, endCol: colIdx });
        // mousedown 時点で即編集開始（mouseup 連動なしで確実に編集導入）
        // ドラッグ選択になった場合は handleSegManualCellMouseEnter で編集をキャンセルする
        const currentGrid = manualTableMemosRef.current?.segment_manual ?? [];
        const currentValue = currentGrid[rowIdx]?.[colIdx] ?? "";
        startManualEditingRef.current?.(coord, currentValue);
    }, []);

    /** segment_manual セルのマウスエンター — ドラッグ中に範囲拡張 */
    const handleSegManualCellMouseEnter = useCallback((rowIdx: number, colIdx: number) => {
        if (!segManualIsDragging.current) return;
        segManualDragMoved.current = true;
        setSegManualSel(prev => {
            const next = prev
                ? { ...prev, endRow: rowIdx, endCol: colIdx }
                : { startRow: rowIdx, startCol: colIdx, endRow: rowIdx, endCol: colIdx };
            return next;
        });
        // ドラッグで別セルへ移動した場合は editing をキャンセル（範囲選択モードへ）
        if (isCommittingManualRef.current) return;
        isCommittingManualRef.current = true;
        setEditingManualCell(null);
        setManualEditValue("");
        requestAnimationFrame(() => { isCommittingManualRef.current = false; });
    }, []);

    /**
     * segment_manual 範囲コピー — copy イベント経由で clipboardData.setData を使用。
     * navigator.clipboard.writeText では Excel の「貼り付け先に合わせる」形式にならないため、
     * onCopy ハンドラで直接 clipboardData に TSV を書き込む。
     * 空白セルも "" として TSV に含め、Excel 貼り付け時に行列位置が維持される。
     */
    const handleSegmentManualCopy = useCallback((e: React.ClipboardEvent) => {
        // segment_manual がアクティブでない場合は通常コピーを許可
        if (activeManualCellRef.current?.tableType !== "segment_manual") return;

        const sel = segManualSelRef.current;
        const grid = manualTableMemosRef.current?.segment_manual ?? [];

        let text: string;
        if (sel) {
            // 範囲選択コピー
            const r1 = Math.min(sel.startRow, sel.endRow);
            const r2 = Math.max(sel.startRow, sel.endRow);
            const c1 = Math.min(sel.startCol, sel.endCol);
            const c2 = Math.max(sel.startCol, sel.endCol);
            text = Array.from({ length: r2 - r1 + 1 }, (_, ri) => {
                const r = r1 + ri;
                return Array.from({ length: c2 - c1 + 1 }, (_, ci) => {
                    const c = c1 + ci;
                    // 空白セルも "" のまま（filter/trim 禁止）
                    const val = grid[r]?.[c] ?? "";
                    // タブ・改行・ダブルクォートを含むセルはクォート
                    if (val.includes("\t") || val.includes("\n") || val.includes("\r") || val.includes('"')) {
                        return '"' + val.replace(/"/g, '""') + '"';
                    }
                    return val;
                }).join("\t");
            }).join("\n");
        } else {
            // 単一セルコピー
            const cell = activeManualCellRef.current;
            if (!cell) return;
            const val = grid[cell.rowIdx]?.[cell.colIdx] ?? "";
            text = val.includes("\t") || val.includes("\n") || val.includes("\r") || val.includes('"')
                ? '"' + val.replace(/"/g, '""') + '"'
                : val;
        }

        e.preventDefault();
        e.stopPropagation();
        e.clipboardData.setData("text/plain", text);
        e.clipboardData.setData("text", text);
    }, []);

    /**
     * segment_manual 専用ペースト — グローバル activeManualCell に依存しない。
     * MemoCellExcel の onPaste から coord 付きで直接呼ばれる。
     * PL 側の activeManualCell が残っていても影響しない。
     */
    const handleSegmentManualPaste = useCallback((
        e: React.ClipboardEvent,
        coord: { tableType: ManualTableType; rowIdx: number; colIdx: number },
    ) => {
        e.preventDefault();
        e.stopPropagation();  // handleTablePaste への伝播を防ぐ

        const rawText = e.clipboardData?.getData("text/plain") ?? "";
        // クリップボード全体が空なら何もしない（末尾空白のみの場合は空白として貼り付けたい）
        if (rawText === "") return;

        // TSV parse: parseTsvClipboard を使用（末尾空行だけ除去、途中空セルは "" として正確に扱う）
        const parsedRows = parseTsvClipboard(rawText);
        if (parsedRows.length === 0) return;

        const rowCount = cumRows.length;
        const colCount = SEGMENT_MANUAL_COL_COUNT;
        const startRow = coord.rowIdx;
        const startCol = coord.colIdx;

        // 現在のグリッドを深コピーしてベースにする
        const currentGrid = manualTableMemosRef.current?.segment_manual ?? [];
        const newGrid: string[][] = Array.from({ length: rowCount }, (_, r) =>
            Array.from({ length: colCount }, (_, c) => currentGrid[r]?.[c] ?? "")
        );

        // ペーストデータを newGrid に書き込む（空白セル "" も必ず書き込む）
        for (let rOff = 0; rOff < parsedRows.length; rOff++) {
            const r = startRow + rOff;
            if (r >= rowCount) break;
            for (let cOff = 0; cOff < parsedRows[rOff].length; cOff++) {
                const c = startCol + cOff;
                if (c >= colCount) break;
                // undefined/null は "" に。空白文字列 "" はそのまま "" を代入して既存値を消す。
                newGrid[r][c] = parsedRows[rOff][cOff] ?? "";
            }
        }


        // onBlur → commitManualEdit が paste 中に発火するのを抑制
        isCommittingManualRef.current = true;
        setEditingManualCell(null);
        setActiveManualCell(coord);
        setActiveCell(null);  // PL 側の activeCell を確実にクリア
        setManualEditValue("");

        // グリッド全体を1回で保存（rAF ループ不使用 → レース条件なし）
        onManualMemoGridUpdateRef.current?.("segment_manual", newGrid);
        requestAnimationFrame(() => {
            isCommittingManualRef.current = false;
            gridRef.current?.focus();
        });
    }, [cumRows.length]);

    /**
     * segment_manual 編集中の Arrow キー処理 — MemoCellExcel から onArrowKey 経由で呼ばれる。
     * 現在セルを確定してから方向移動する。
     * IME 判定は MemoCellExcel 側の onKeyDown で行っているためここでは不要。
     */
    const handleSegManualArrowKey = useCallback((dir: "up" | "down" | "left" | "right") => {
        // 確定（onBlur と二重にならないよう isCommittingManualRef で保護）
        commitManualEdit();
        // 確定後に方向移動（moveManualActiveCellDir は activeManualCell を参照するため同期で OK）
        const dRow = dir === "up" ? -1 : dir === "down" ? 1 : 0;
        const dCol = dir === "left" ? -1 : dir === "right" ? 1 : 0;
        moveManualActiveCellDir(dRow, dCol);
    }, [commitManualEdit, moveManualActiveCellDir]);

    const handleTablePaste = useCallback((e: React.ClipboardEvent) => {
        // セグメントセルがアクティブの場合 → セグメントペースト処理
        if (activeSegCell && onBulkSaveOverrides) {
            e.preventDefault();
            e.stopPropagation();
            const rawText = e.clipboardData.getData("text/plain");
            if (!rawText) return;

            const parsed = parseTsvClipboard(rawText);

            if (parsed.length === 0) return;

            const startRow = activeSegCell.rowIdx;
            const startCol = activeSegCell.colIdx;
            const totalSegCols = segmentColumns.length * 2; // 各セグメント × (sales + profit)

            const items: SegmentOverrideSaveRequest[] = [];
            let skipped = 0;
            let invalid = 0;

            for (let r = 0; r < parsed.length; r++) {
                const targetRowIdx = startRow + r;
                if (targetRowIdx >= cumRows.length) { skipped += parsed[r].length; continue; }
                const row = cumRows[targetRowIdx];
                const isEditableQ = row.quarter === "1Q" || row.quarter === "3Q";
                if (!isEditableQ) { skipped += parsed[r].length; continue; }

                const fy = extractFiscalYear(row.period);

                for (let c = 0; c < parsed[r].length; c++) {
                    const targetColIdx = startCol + c;
                    if (targetColIdx >= totalSegCols) { skipped++; continue; }

                    // セグメント列インデックスから segmentName と metric を逆算
                    const scIdx = Math.floor(targetColIdx / 2);
                    const metricIdx = targetColIdx % 2; // 0=sales, 1=profit
                    if (scIdx >= segmentColumns.length) { skipped++; continue; }

                    const sc = segmentColumns[scIdx];
                    const segmentName = sc.segmentName;
                    const metric = metricIdx === 0 ? "sales" : "operating_profit";

                    // base 値チェック: base がある (非null かつ source !== "manual") セルはスキップ
                    const mapKey = `${normalizePeriod(row.period)}|${normalizeQuarter(row.quarter)}`;
                    const segKey = metricIdx === 0 ? sc.salesKey : sc.profitKey;
                    const currentVal = segmentMap.get(mapKey)?.[segKey] ?? null;
                    const cellSource = sourceMap.get(`${mapKey}|${segKey}`);
                    const isManualCell = cellSource === "manual";
                    const hasBaseValue = currentVal !== null && !isManualCell;
                    if (hasBaseValue) { skipped++; continue; }

                    // 数値パース（セル単位）
                    const cellText = parsed[r][c];
                    const numVal = parseNumericValue(cellText);
                    if (numVal === null) { invalid++; continue; }

                    items.push({
                        ticker: row.period.split("-").length > 0 ? "" : "", // ticker は親から渡される
                        fiscal_year: fy,
                        quarter: row.quarter,
                        segment_name: segmentName,
                        metric,
                        value: numVal,
                    });
                }
            }


            if (items.length === 0) {
                showToast(`0件保存 / ${skipped}件スキップ / ${invalid}件不正値`);
                return;
            }

            // 非同期で bulk save
            onBulkSaveOverrides(items).then(({ saved, failed }) => {
                const msg = `${saved}件保存 / ${skipped}件スキップ${invalid > 0 ? ` / ${invalid}件不正値` : ""}${failed > 0 ? ` / ${failed}件失敗` : ""}`;
                showToast(msg);
            }).catch((err) => {
                console.error("[SEG-PASTE] bulk save error:", err);
                showToast(`保存エラー: ${err instanceof Error ? err.message : String(err)}`);
            });

            return;
        }

        // ── segment_manual 専用ペースト ──
        // click = edit（IME fix）により editingManualCell 中でも多セル貼り付けを優先する。
        // PERIOD/Q は保存 grid に含まず。colIdx は保存 grid 座標 0..11。
        const segManualTarget =
            activeManualCell?.tableType === "segment_manual" ? activeManualCell
            : editingManualCell?.tableType === "segment_manual" ? editingManualCell
            : null;
        if (segManualTarget && !activeCell && !activeSegCell) {
            e.preventDefault();
            e.stopPropagation();
            const rawText = e.clipboardData?.getData("text/plain") ?? "";
            if (rawText === "") return;
            const parsedRows = parseTsvClipboard(rawText);
            if (parsedRows.length === 0) return;

            const rowCount = cumRows.length;
            const colCount = SEGMENT_MANUAL_COL_COUNT;
            const startRow = segManualTarget.rowIdx;
            const startCol = segManualTarget.colIdx;

            // 現在のグリッドを深コピーしてベースにする
            const currentGrid = manualTableMemosRef.current?.segment_manual ?? [];
            const newGrid: string[][] = Array.from({ length: rowCount }, (_, r) =>
                Array.from({ length: colCount }, (_, c) => currentGrid[r]?.[c] ?? "")
            );

            // ペーストデータを newGrid に書き込む（空白セル "" も必ず書き込む）
            for (let rOff = 0; rOff < parsedRows.length; rOff++) {
                const r = startRow + rOff;
                if (r >= rowCount) break;
                for (let cOff = 0; cOff < parsedRows[rOff].length; cOff++) {
                    const c = startCol + cOff;
                    if (c >= colCount) break;
                    // 空白セル "" も必ず代入 → old value fallback 禁止
                    newGrid[r][c] = parsedRows[rOff][cOff] ?? "";
                }
            }


            // onBlur → commitManualEdit が paste 中に発火するのを抑制
            isCommittingManualRef.current = true;
            setEditingManualCell(null);
            setActiveManualCell(segManualTarget);
            setManualEditValue("");

            // グリッド全体を1回で保存（rAF ループ不使用 → レース条件なし）
            onManualMemoGridUpdateRef.current?.("segment_manual", newGrid);
            requestAnimationFrame(() => {
                isCommittingManualRef.current = false;
                gridRef.current?.focus();
            });
            return;
        }

        // 手入力メモ専用行がアクティブの場合 → 手入力メモペースト（activeCell がない場合のみ）
        // segment_manual は上の segManualTarget ブロックで処理済みのため、ここには到達しない
        if (activeManualCell && !activeCell && !editingManualCell) {
            e.preventDefault();
            e.stopPropagation();
            const manualText = e.clipboardData.getData("text/plain");
            if (!manualText) return;

            // segment_manual はここで再度処理（segManualTarget が null だった場合の安全弁）
            // ── 一括保存でレース条件を排除 ──
            if (activeManualCell.tableType === "segment_manual") {
                const parsedRows = parseTsvClipboard(manualText);
                if (parsedRows.length === 0) return;

                const rowCount = cumRows.length;
                const colCount = SEGMENT_MANUAL_COL_COUNT;
                const currentGrid = manualTableMemosRef.current?.segment_manual ?? [];
                const newGrid: string[][] = Array.from({ length: rowCount }, (_, r) =>
                    Array.from({ length: colCount }, (_, c) => currentGrid[r]?.[c] ?? "")
                );
                for (let rOff = 0; rOff < parsedRows.length; rOff++) {
                    const r = activeManualCell.rowIdx + rOff;
                    if (r >= rowCount) break;
                    for (let cOff = 0; cOff < parsedRows[rOff].length; cOff++) {
                        const c = activeManualCell.colIdx + cOff;
                        if (c >= colCount) break;
                        newGrid[r][c] = parsedRows[rOff][cOff] ?? "";
                    }
                }
                isCommittingManualRef.current = true;
                setEditingManualCell(null);
                setManualEditValue("");
                onManualMemoGridUpdateRef.current?.("segment_manual", newGrid);
                requestAnimationFrame(() => {
                    isCommittingManualRef.current = false;
                    gridRef.current?.focus();
                });
                return;
            }

            // pl_cum / pl_q / segment_cum / segment_q: 逐次 rAF（ステートが小さいため許容）
            const manualParsed = parseTsvClipboard(manualText);
            if (manualParsed.length > 0) {
                const manualColCounts: Record<ManualTableType, number> = {
                    pl_cum:         CUM_BASE_COL_COUNT,
                    pl_q:           Q_BASE_COL_COUNT - 2,
                    segment_cum:    2 + segmentColumns.length * 2,
                    segment_q:      2 + segmentColumns.length * 2,
                    segment_manual: SEGMENT_MANUAL_COL_COUNT,
                };
                const manualColCount = manualColCounts[activeManualCell.tableType];
                const maxRows = getManualRowCount(activeManualCell.tableType) - activeManualCell.rowIdx;
                const pasteItems: {
                    tableType: ManualTableType;
                    rowIdx: number;
                    colIdx: number;
                    value: string;
                }[] = [];
                for (let r = 0; r < Math.min(manualParsed.length, maxRows); r++) {
                    for (let c = 0; c < manualParsed[r].length; c++) {
                        const col = activeManualCell.colIdx + c;
                        if (col >= manualColCount) break;
                        pasteItems.push({
                            tableType: activeManualCell.tableType,
                            rowIdx: activeManualCell.rowIdx + r,
                            colIdx: col,
                            value: manualParsed[r][c],
                        });
                    }
                }
                if (pasteItems.length > 0) {
                    let pasteIdx = 0;
                    const applyNextPasteItem = () => {
                        if (pasteIdx >= pasteItems.length) return;
                        const item = pasteItems[pasteIdx++];
                        onManualMemoEditRef.current?.(
                            item.tableType,
                            item.rowIdx,
                            item.colIdx,
                            item.value,
                        );
                        if (pasteIdx < pasteItems.length) {
                            requestAnimationFrame(applyNextPasteItem);
                        }
                    };
                    applyNextPasteItem();
                }
            }
            return;
        }

        // メモ / KPI セルがアクティブの場合 → 統合 handleEditablePaste にルーティング
        if (!activeCell) return;
        // pl_*_manual: colKey ("col_N") → colIdx
        if (activeCell.tableId === "pl_cum_manual" || activeCell.tableId === "pl_q_manual") {
            const colIdx = parseInt(activeCell.colKey.replace("col_", ""));
            handleEditablePaste(activeCell.tableId, colIdx, activeCell.rowIdx, e);
            return;
        }
        // memo_kpi / cum / q: 対象テーブルの編集可能列を判定
        const availableCols = activeCell.tableId === "q" ? [...KPI_COLS] as string[] : EDITABLE_COLS;
        if (!availableCols.includes(activeCell.colKey)) return;
        const startEditableIdx = availableCols.indexOf(activeCell.colKey);
        handleEditablePaste(activeCell.tableId, startEditableIdx, activeCell.rowIdx, e);
    }, [activeCell, activeManualCell, editingManualCell, activeSegCell, handleEditablePaste, onBulkSaveOverrides, cumRows, segmentColumns, segmentMap, sourceMap, showToast, manualTableMemos]);

    if (loading) {
        return (
            <div className="data-section">
                <h2 className="section-title">📊 PL（四半期業績推移）</h2>
                <div className="loading-message">読込中...</div>
            </div>
        );
    }

    return (
        <div
            ref={gridRef}
            className="data-section pl-section"
            tabIndex={0}
            onKeyDown={handleTableKeyDown}
            onPaste={handleTablePaste}
        >
            <h2 className="section-title">📊 PL（四半期業績推移） — 過去5年</h2>

            {/* ============ フォーミュラバー ============ */}
            <div className="formula-bar">
                <div className="formula-bar-label">
                    {activeCellLabel ? (
                        <span className="cell-ref">{activeCellLabel}</span>
                    ) : (
                        <span className="cell-ref cell-ref-empty">セル未選択</span>
                    )}
                </div>
                <textarea
                    ref={formulaBarRef}
                    className="formula-bar-input"
                    placeholder="セルを選択してください"
                    value={activeCell ? getActiveCellValue() : ""}
                    onChange={(e) => handleFormulaBarChange(e.target.value)}
                    onKeyDown={(e) => {
                        // フォーミュラバーでEscapeを押したらgridに戻る
                        if (e.key === "Escape") {
                            e.preventDefault();
                            focusGrid();
                        }
                    }}
                    onBlur={() => {
                        // フォーミュラバーからフォーカスが外れたらgridにfocusを戻す
                        // ただしgrid内の他要素へ移動する場合はgrid側で処理される
                    }}
                    style={{ height: formulaBarHeight }}
                    disabled={!activeCell}
                />
            </div>
            {/* フォーミュラバー リサイズハンドル */}
            <div
                className="formula-bar-resize-handle"
                onMouseDown={handleFbResizeStart}
            >
                <span className="formula-bar-resize-grip">⋯</span>
            </div>

            {data.length === 0 ? (
                <div className="no-data-message">該当なし</div>
            ) : (
                <>
                    <div
                        className="pl-scroll-area"
                        ref={(el) => { onPlScrollAreaReady?.(el); }}
                    >
                        <div className="pl-dual-tables">
                            {/* === 累計PL === */}
                            <div className="pl-table-block">
                                <div className="pl-table-label">累計PL（百万円）</div>
                                <table className="pl-table" style={{ minWidth: cumTableWidth }}>
                                    <PLTableHeader columns={CUM_COLUMNS} widths={cumResize.widths.slice(0, 8)} onResizeStart={cumResize.handleMouseDown}
                                        kpiSlots={[]} kpiDefs={[]} kpiWidths={[]} onKpiResizeStart={() => {}}
                                        editingKpiHeader={null} editingKpiHeaderValue="" kpiHeaderInputRef={{ current: null }}
                                        onStartKpiHeaderEdit={() => {}} onEditingKpiHeaderValueChange={() => {}}
                                        onCommitKpiHeaderEdit={() => {}} onCancelKpiHeaderEdit={() => {}}
                                    />
                                    <tbody>
                                        {cumRows.map((row, idx) => {
                                            const isSelected = selectedPeriod === row.period && selectedQuarter === row.quarter;
                                            return (
                                                <tr key={`cum-${row.period}-${row.quarter}-${idx}`} className={[
                                                    "pl-row",
                                                    isSelected ? "pl-row-selected" : "",
                                                    row.quarter === "FY" ? "pl-row-fy" : "",
                                                    "year-group-row",
                                                    cumRows[idx - 1]?.period !== row.period ? "year-group-start" : "",
                                                    cumRows[idx + 1]?.period !== row.period ? "year-group-end" : "",
                                                ].filter(Boolean).join(" ")} onClick={() => onRowClick?.(row.period, row.quarter)}>
                                                    <td style={{ width: cumResize.widths[0], minWidth: cumResize.widths[0] }} className={isCellInRange("cum", idx, 0) ? "cell-in-range" : ""} onMouseDown={(e) => handleCellMouseDown("cum", idx, 0, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 0)}>{displayValue(row.period)}</td>
                                                    <td style={{ width: cumResize.widths[1], minWidth: cumResize.widths[1] }} className={isCellInRange("cum", idx, 1) ? "cell-in-range" : ""} onMouseDown={(e) => handleCellMouseDown("cum", idx, 1, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 1)}>{displayValue(row.quarter)}</td>
                                                    <td style={{ width: cumResize.widths[2], minWidth: cumResize.widths[2] }} className={`num-col ${isCellInRange("cum", idx, 2) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 2, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 2)}>{formatMillions(row.sales)}</td>
                                                    <td style={{ width: cumResize.widths[3], minWidth: cumResize.widths[3] }} className={`num-col ${isCellInRange("cum", idx, 3) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 3, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 3)}>{formatMillions(row.grossProfit)}</td>
                                                    <td style={{ width: cumResize.widths[4], minWidth: cumResize.widths[4] }} className={`num-col gp-margin-col ${isCellInRange("cum", idx, 4) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 4, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 4)}>{fmtMargin(row.grossMarginRate)}</td>
                                                    <td style={{ width: cumResize.widths[5], minWidth: cumResize.widths[5] }} className={`num-col ${isCellInRange("cum", idx, 5) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 5, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 5)}>{formatMillions(row.sgAndA)}</td>
                                                    <td style={{ width: cumResize.widths[6], minWidth: cumResize.widths[6] }} className={`num-col ${isCellInRange("cum", idx, 6) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 6, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 6)}>{formatMillions(row.operatingProfit)}</td>
                                                    <td style={{ width: cumResize.widths[7], minWidth: cumResize.widths[7] }} className={`num-col op-margin-col ${isCellInRange("cum", idx, 7) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("cum", idx, 7, e)} onMouseEnter={() => handleCellMouseEnter("cum", idx, 7)}>{fmtMargin(row.opMargin)}</td>
                                                </tr>
                                            );
                                        })}
                                        {/* 手入力メモ専用行 (PL累計) — memo_kpi と同じロジック */}
                                        {Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) => (
                                            <tr key={`manual-cum-${rowIdx}`} className={`manual-memo-row manual-memo-row-${rowIdx + 1}`}>
                                                {Array.from({ length: CUM_BASE_COL_COUNT }, (_, colIdx) => {
                                                    const colKey = `col_${colIdx}`;
                                                    const cellValue = manualTableMemos?.pl_cum?.[rowIdx]?.[colIdx] ?? "";
                                                    const isActive = activeCell?.tableId === "pl_cum_manual" && activeCell?.rowIdx === rowIdx && activeCell?.colKey === colKey;
                                                    const isEditing = editingPlMemoCell?.tableId === "pl_cum_manual" && editingPlMemoCell?.rowIdx === rowIdx && editingPlMemoCell?.colKey === colKey;
                                                    const isInRange = isCellInRange("pl_cum_manual", rowIdx, colIdx);
                                                    const isPLGpMargin = colIdx === 4;
                                                    const isPLOpMargin = colIdx === 7;
                                                    const extraClass = `manual-memo-cell${isPLGpMargin ? " gp-margin-col" : ""}${isPLOpMargin ? " op-margin-col" : ""}`;
                                                    return (
                                                        <MemoCellExcel
                                                            key={colIdx}
                                                            value={cellValue}
                                                            isActive={isActive}
                                                            isInRange={isInRange}
                                                            isEditing={isEditing}
                                                            editValue={plMemoEditValue}
                                                            onSelect={NOOP}
                                                            onStartEdit={plCumManualStartEditHandlers[rowIdx]?.[colIdx]}
                                                            onMouseDownCaptureEdit={(e) => plCumManualCaptureHandlers[rowIdx]?.[colIdx]?.(e, cellValue)}
                                                            onBlurShouldSkip={plMemoBlurShouldSkip}
                                                            onEditChange={setPlMemoEditValue}
                                                            onCommit={commitPlMemoEdit}
                                                            onCancel={cancelPlMemoEdit}
                                                            inputRef={isEditing ? plMemoInputRef : undefined}
                                                            onMouseEnter={plCumManualMouseEnterHandlers[rowIdx]?.[colIdx]}
                                                            onMouseEnterRange={plCumManualMouseEnterHandlers[rowIdx]?.[colIdx]}
                                                            onArrowKey={handlePlMemoArrowKey}
                                                            onPaste={plCumManualPasteHandlers[rowIdx]?.[colIdx]}
                                                            className={extraClass}
                                                            width={cumResize.widths[colIdx]}
                                                        />
                                                    );
                                                })}
                                            </tr>
                                        ))}

                                    </tbody>
                                </table>
                            </div>
                            {/* === Q単体PL === */}
                            <div className="pl-table-block">
                                <div className="pl-table-label">Q単体PL（百万円）</div>
                                <table className="pl-table" style={{ minWidth: qTableWidth }}>
                                    <PLTableHeader
                                        columns={Q_BASE_COLUMNS.slice(2)}
                                        widths={qResize.widths.slice(2)}
                                        onResizeStart={(idx, e) => qResize.handleMouseDown(idx + 2, e)}
                                        kpiSlots={[]} kpiDefs={[]} kpiWidths={[]} onKpiResizeStart={() => {}}
                                        editingKpiHeader={null} editingKpiHeaderValue="" kpiHeaderInputRef={{ current: null }}
                                        onStartKpiHeaderEdit={() => {}} onEditingKpiHeaderValueChange={() => {}}
                                        onCommitKpiHeaderEdit={() => {}} onCancelKpiHeaderEdit={() => {}}
                                    />
                                    <tbody>
                                        {qRows.map((row, idx) => (
                                            <tr key={`q-${row.period}-${row.quarter}-${idx}`} className={[
                                                "pl-row",
                                                selectedPeriod === row.period && selectedQuarter === row.quarter ? "pl-row-selected" : "",
                                                row.quarter === "FY" ? "pl-row-fy" : "",
                                                "year-group-row",
                                                qRows[idx - 1]?.period !== row.period ? "year-group-start" : "",
                                                qRows[idx + 1]?.period !== row.period ? "year-group-end" : "",
                                            ].filter(Boolean).join(" ")} onClick={() => onRowClick?.(row.period, row.quarter)}>
                                                <td style={{ width: qResize.widths[2], minWidth: qResize.widths[2] }} className={`num-col ${isCellInRange("q", idx, 2) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 2, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 2)}>{formatMillions(row.sales)}</td>
                                                <td style={{ width: qResize.widths[3], minWidth: qResize.widths[3] }} className={`num-col ${isCellInRange("q", idx, 3) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 3, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 3)}>{formatMillions(row.grossProfit)}</td>
                                                <td style={{ width: qResize.widths[4], minWidth: qResize.widths[4] }} className={`num-col gp-margin-col ${isCellInRange("q", idx, 4) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 4, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 4)}>{fmtMargin(row.grossMarginRate)}</td>
                                                <td style={{ width: qResize.widths[5], minWidth: qResize.widths[5] }} className={`num-col ${isCellInRange("q", idx, 5) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 5, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 5)}>{formatMillions(row.sgAndA)}</td>
                                                <td style={{ width: qResize.widths[6], minWidth: qResize.widths[6] }} className={`num-col ${isCellInRange("q", idx, 6) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 6, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 6)}>{formatMillions(row.operatingProfit)}</td>
                                                <td style={{ width: qResize.widths[7], minWidth: qResize.widths[7] }} className={`num-col op-margin-col ${isCellInRange("q", idx, 7) ? "cell-in-range" : ""}`} onMouseDown={(e) => handleCellMouseDown("q", idx, 7, e)} onMouseEnter={() => handleCellMouseEnter("q", idx, 7)}>{fmtMargin(row.opMargin)}</td>
                                            </tr>
                                        ))}
                                        {/* 手入力メモ専用行 (PL Q単体) — memo_kpi と同じロジック */}
                                        {Array.from({ length: MANUAL_MEMO_ROW_COUNT }, (_, rowIdx) => (
                                            <tr key={`manual-q-${rowIdx}`} className={`manual-memo-row manual-memo-row-${rowIdx + 1}`}>
                                                {Array.from({ length: Q_BASE_COL_COUNT - 2 }, (_, displayCol) => {
                                                    const saveColIdx = displayCol + 2; // colOffset=2
                                                    const colKey = `col_${displayCol}`; // 表示列キー
                                                    const cellValue = manualTableMemos?.pl_q?.[rowIdx]?.[saveColIdx] ?? "";
                                                    const isActive = activeCell?.tableId === "pl_q_manual" && activeCell?.rowIdx === rowIdx && activeCell?.colKey === colKey;
                                                    const isEditing = editingPlMemoCell?.tableId === "pl_q_manual" && editingPlMemoCell?.rowIdx === rowIdx && editingPlMemoCell?.colKey === colKey;
                                                    const isInRange = isCellInRange("pl_q_manual", rowIdx, displayCol);
                                                    const isPLGpMargin = saveColIdx === 4;
                                                    const isPLOpMargin = saveColIdx === 7;
                                                    const extraClass = `manual-memo-cell${isPLGpMargin ? " gp-margin-col" : ""}${isPLOpMargin ? " op-margin-col" : ""}`;
                                                    return (
                                                        <MemoCellExcel
                                                            key={displayCol}
                                                            value={cellValue}
                                                            isActive={isActive}
                                                            isInRange={isInRange}
                                                            isEditing={isEditing}
                                                            editValue={plMemoEditValue}
                                                            onSelect={NOOP}
                                                            onStartEdit={plQManualStartEditHandlers[rowIdx]?.[displayCol]}
                                                            onMouseDownCaptureEdit={(e) => plQManualCaptureHandlers[rowIdx]?.[displayCol]?.(e, cellValue)}
                                                            onBlurShouldSkip={plMemoBlurShouldSkip}
                                                            onEditChange={setPlMemoEditValue}
                                                            onCommit={commitPlMemoEdit}
                                                            onCancel={cancelPlMemoEdit}
                                                            inputRef={isEditing ? plMemoInputRef : undefined}
                                                            onMouseEnter={plQManualMouseEnterHandlers[rowIdx]?.[displayCol]}
                                                            onMouseEnterRange={plQManualMouseEnterHandlers[rowIdx]?.[displayCol]}
                                                            onArrowKey={handlePlMemoArrowKey}
                                                            onPaste={plQManualPasteHandlers[rowIdx]?.[displayCol]}
                                                            className={extraClass}
                                                            width={qResize.widths[saveColIdx]}
                                                        />
                                                    );
                                                })}
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            {/* === メモ欄・KPI欄 (右側) === */}
                            <div className="pl-table-block">
                                <div className="pl-table-label">メモ欄・KPI欄</div>
                                <table className="pl-table" style={{ minWidth: memoKpiTableWidth }}>
                                    <PLTableHeader
                                        columns={MEMO_KPI_BASE_COLUMNS.slice(2)}
                                        widths={memoKpiResize.widths.slice(2)}
                                        onResizeStart={(idx, e) => memoKpiResize.handleMouseDown(idx + 2, e)}
                                        kpiSlots={KPI_SLOTS} kpiDefs={kpiDefs} kpiWidths={kpiWidths} onKpiResizeStart={handleKpiResizeStart}
                                        editingKpiHeader={editingKpiHeader} editingKpiHeaderValue={editingKpiHeaderValue} kpiHeaderInputRef={kpiHeaderInputRef}
                                        onStartKpiHeaderEdit={startKpiHeaderEdit} onEditingKpiHeaderValueChange={setEditingKpiHeaderValue}
                                        onCommitKpiHeaderEdit={commitKpiHeaderEdit} onCancelKpiHeaderEdit={cancelKpiHeaderEdit}
                                    />
                                    <tbody>
                                        {cumRows.map((row, idx) => {
                                            const memoKey = `${row.period}|${row.quarter}`;
                                            const memoGrid = memoMap?.[memoKey];
                                            const memoA = extractMemoValue(memoGrid, 0);
                                            const memoB = extractMemoValue(memoGrid, 1);
                                            return (
                                                <tr key={`mk-${row.period}-${row.quarter}-${idx}`} className={[
                                                    "pl-row",
                                                    selectedPeriod === row.period && selectedQuarter === row.quarter ? "pl-row-selected" : "",
                                                    row.quarter === "FY" ? "pl-row-fy" : "",
                                                    "year-group-row",
                                                    cumRows[idx - 1]?.period !== row.period ? "year-group-start" : "",
                                                    cumRows[idx + 1]?.period !== row.period ? "year-group-end" : "",
                                                ].filter(Boolean).join(" ")}>
                                                    {/* memo_a (colIdx=2) */}
                                                    <MemoCellExcel value={memoA} width={memoKpiResize.widths[2]}
                                                        isActive={activeCell?.tableId === "memo_kpi" && activeCell?.rowIdx === idx && activeCell?.colKey === "memo_a"}
                                                        isInRange={isCellInRange("memo_kpi", idx, 2)}
                                                        isEditing={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === "memo_a"}
                                                        editValue={plMemoEditValue}
                                                        onSelect={NOOP}
                                                        onStartEdit={memoAStartEditHandlers[idx]}
                                                        onMouseDownCaptureEdit={(e) => memoACaptureHandlers[idx]?.(e, memoA)}
                                                        onBlurShouldSkip={plMemoBlurShouldSkip}
                                                        onEditChange={setPlMemoEditValue}
                                                        onCommit={commitPlMemoEdit}
                                                        onCancel={cancelPlMemoEdit}
                                                        inputRef={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === "memo_a" ? plMemoInputRef : undefined}
                                                        onMouseEnter={memoAMouseEnterHandlers[idx]}
                                                        onMouseEnterRange={memoAMouseEnterHandlers[idx]}
                                                        onArrowKey={handlePlMemoArrowKey}
                                                    />
                                                    {/* memo_b (colIdx=3) */}
                                                    <MemoCellExcel value={memoB} width={memoKpiResize.widths[3]}
                                                        isActive={activeCell?.tableId === "memo_kpi" && activeCell?.rowIdx === idx && activeCell?.colKey === "memo_b"}
                                                        isInRange={isCellInRange("memo_kpi", idx, 3)}
                                                        isEditing={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === "memo_b"}
                                                        editValue={plMemoEditValue}
                                                        onSelect={NOOP}
                                                        onStartEdit={memoBStartEditHandlers[idx]}
                                                        onMouseDownCaptureEdit={(e) => memoBCaptureHandlers[idx]?.(e, memoB)}
                                                        onBlurShouldSkip={plMemoBlurShouldSkip}
                                                        onEditChange={setPlMemoEditValue}
                                                        onCommit={commitPlMemoEdit}
                                                        onCancel={cancelPlMemoEdit}
                                                        inputRef={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === "memo_b" ? plMemoInputRef : undefined}
                                                        onMouseEnter={memoBMouseEnterHandlers[idx]}
                                                        onMouseEnterRange={memoBMouseEnterHandlers[idx]}
                                                        onArrowKey={handlePlMemoArrowKey}
                                                    />
                                                    {/* kpi_1/2/3 (colIdx=4/5/6) */}
                                                    {KPI_SLOTS.map((slot) => {
                                                        const colKey = `kpi_${slot}`;
                                                        const kpiKey = `cum|${row.period}|${row.quarter}`;
                                                        const cellVal = kpiValues?.[kpiKey]?.[slot] ?? "";
                                                        const kpiAbsCol = 3 + slot; // 4=kpi_1, 5=kpi_2, 6=kpi_3
                                                        return (
                                                            <MemoCellExcel key={colKey} value={cellVal} width={kpiWidths[slot - 1]}
                                                                isActive={activeCell?.tableId === "memo_kpi" && activeCell?.rowIdx === idx && activeCell?.colKey === colKey}
                                                                isInRange={isCellInRange("memo_kpi", idx, kpiAbsCol)}
                                                                isEditing={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === colKey}
                                                                editValue={plMemoEditValue}
                                                                onSelect={NOOP}
                                                                onStartEdit={kpiStartEditHandlers[idx]?.[slot - 1]}
                                                                onMouseDownCaptureEdit={(e) => kpiCaptureHandlers[idx]?.[slot - 1]?.(e, cellVal)}
                                                                onBlurShouldSkip={plMemoBlurShouldSkip}
                                                                onEditChange={setPlMemoEditValue}
                                                                onCommit={commitPlMemoEdit}
                                                                onCancel={cancelPlMemoEdit}
                                                                inputRef={editingPlMemoCell?.tableId === "memo_kpi" && editingPlMemoCell?.rowIdx === idx && editingPlMemoCell?.colKey === colKey ? plMemoInputRef : undefined}
                                                                className="kpi-cell"
                                                                onMouseEnter={kpiMouseEnterHandlers[idx]?.[slot - 1]}
                                                                onMouseEnterRange={kpiMouseEnterHandlers[idx]?.[slot - 1]}
                                                                onArrowKey={handlePlMemoArrowKey}
                                                            />
                                                        );
                                                    })}
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        </div>{/* pl-dual-tables */}
                    </div>{/* pl-scroll-area */}
                    {/* リサイズハンドル（縦スクロール廃止のため非表示） */}
                    <div className="pl-resize-handle" style={{ display: "none" }} onMouseDown={handleResizeMouseDown}>
                        <div className="pl-resize-grip">⋯</div>
                    </div>
                    {/* PL最下部サマリ: 最新FY予想の売上・営業利益 */}
                    {(() => {
                        // 実績 FY で最新の period
                        const latestActualFYPeriod =
                            [...cumRows].reverse().find(r => r.quarter === "FY")?.period ?? "";

                        // 候補選定:
                        // 1. latestActualFY より未来の予想（翌期予想）を優先
                        // 2. 同 period の当期予想
                        // 3. なければ null
                        const latestForecast: typeof forecastFYRows[0] | undefined =
                            forecastFYRows.find(r => r.period > latestActualFYPeriod) ??
                            forecastFYRows.find(r => r.period === latestActualFYPeriod) ??
                            (latestActualFYPeriod === "" ? forecastFYRows[0] : undefined);

                        if (!latestForecast) return null;
                        if (latestForecast.sales === null && latestForecast.operating_profit === null) return null;

                        const sales = latestForecast.sales;
                        const op    = latestForecast.operating_profit;
                        const opMargin =
                            op !== null && sales !== null && sales !== 0
                                ? (op / sales) * 100
                                : null;

                        return (
                            <div className="pl-summary-bar">
                                <span className="pl-summary-period">
                                    📌 最新FY予想: {latestForecast.period} {latestForecast.quarter}
                                </span>
                                {sales !== null && sales !== 0 && (
                                    <span className="pl-summary-item">
                                        <span className="pl-summary-label">売上</span>
                                        <span className="pl-summary-value">{formatMillions(sales)}</span>
                                    </span>
                                )}
                                {op !== null && (
                                    <span className="pl-summary-item">
                                        <span className="pl-summary-label">営業利益</span>
                                        <span className="pl-summary-value">{formatMillions(op)}</span>
                                    </span>
                                )}
                                {opMargin !== null && (
                                    <span className="pl-summary-item">
                                        <span className="pl-summary-label">営利率</span>
                                        <span className="pl-summary-value">{fmtMargin(opMargin)}</span>
                                    </span>
                                )}
                            </div>
                        );
                    })()}
                    {/* セグメント群テーブル */}
                    {cumRows.length > 0 && (
                        <div className="data-section seg-section" style={{ marginTop: 12 }}>
                            {/* ─ source別タブ + タイトル行 ─ */}
                            <div className="segment-header-row">
                                <h3 className="section-title" style={{ fontSize: 14, margin: 0 }}>
                                    {"📊"} セグメント業績
                                    {segmentColumns.length > 0 && (
                                        <span style={{ fontSize: 12, fontWeight: 400, color: "var(--text-muted)", marginLeft: 8 }}>
                                            — {segmentColumns.length}件
                                        </span>
                                    )}
                                </h3>
                                <div style={{ display: "flex", gap: 4, padding: "4px 12px", alignItems: "center" }}>
                                    {([
                                        { key: "tdnet",  label: "TDNET/XBRL" },
                                        { key: "edinet", label: "EDINET" },
                                        { key: "all",    label: "ALL" },
                                        { key: "memo",   label: "MEMO" },
                                    ] as const).map(({ key, label }) => (
                                        <button
                                            key={key}
                                            className={`segment-edit-toggle${segSourceTab === key ? " active" : ""}`}
                                            style={{ padding: "3px 10px", fontSize: "0.75rem" }}
                                            onClick={() => setSegSourceTab(key)}
                                        >
                                            {label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                            {segSourceTab === "memo" ? (
                                <SegmentManualMemoTable
                                    rows={cumRows}
                                    gridData={manualTableMemos?.segment_manual ?? []}
                                    activeManualCell={activeManualCell}
                                    editingManualCell={editingManualCell}
                                    editValue={manualEditValue}
                                    onStartEdit={startManualEditing}
                                    onEditChange={setManualEditValue}
                                    onCommit={commitManualEdit}
                                    onCancel={cancelManualEdit}
                                    editInputRef={editInputRef}
                                    onSegmentPaste={handleSegmentManualPaste}
                                    segManualSel={segManualSel}
                                    onCellMouseDown={handleSegManualCellMouseDown}
                                    onCellMouseEnter={handleSegManualCellMouseEnter}
                                    onCopy={handleSegmentManualCopy}
                                    onArrowKey={handleSegManualArrowKey}
                                    columnHeaders={segmentManualHeaders}
                                    onHeaderEdit={onSegmentManualHeaderEdit}
                                />
                            ) : (
                                <div className="pl-scroll-area">
                                    <div className="pl-dual-tables">
                                    <div className="pl-table-block">
                                        <div className="pl-table-label">累計セグメント（百万円）</div>
                                        <table className="pl-table" style={{ minWidth: segCumTableWidth }}>
                                            <thead><tr>
                                                <th style={{ width: 100, minWidth: 100 }}><div className="th-content"><span>PERIOD</span></div></th>
                                                <th style={{ width: 45, minWidth: 45 }}><div className="th-content"><span>Q</span></div></th>
                                                {segmentHeaders.map((eh, si) => <th key={`seg-cum-h-${si}`} className={`seg-header-cell ${eh.className || "num-col"} ${si % 2 === 1 ? "segment-group-end" : ""}`} style={{ width: segWidths[si] ?? 90, minWidth: 24 }}><div className="th-content"><span>{eh.label}</span><div className="resize-handle" onMouseDown={(e) => handleSegResizeStart(si, e)} /></div></th>)}
                                            </tr></thead>
                                            <tbody>
                                                {cumRows.map((row, idx) => (
                                                    <tr key={`seg-cum-${row.period}-${row.quarter}-${idx}`} className={[
                                                        "pl-row",
                                                        selectedPeriod === row.period && selectedQuarter === row.quarter ? "pl-row-selected" : "",
                                                        row.quarter === "FY" ? "pl-row-fy" : "",
                                                        "year-group-row",
                                                        cumRows[idx - 1]?.period !== row.period ? "year-group-start" : "",
                                                        cumRows[idx + 1]?.period !== row.period ? "year-group-end" : "",
                                                    ].filter(Boolean).join(" ")}>
                                                        <td style={{ width: 100, minWidth: 100 }}>{displayValue(row.period)}</td>
                                                        <td style={{ width: 45, minWidth: 45 }}>{displayValue(row.quarter)}</td>
                                                        {segmentColumns.map((sc, scIdx) => {
                                                            const salesVal = getSegValue(row.period, row.quarter, sc.salesKey);
                                                            const profitVal = getSegValue(row.period, row.quarter, sc.profitKey);
                                                            const sIdx = scIdx * 2;
                                                            const pIdx = scIdx * 2 + 1;
                                                            const mapKey = `${normalizePeriod(row.period)}|${normalizeQuarter(row.quarter)}`;
                                                            const salesSource = sourceMap.get(`${mapKey}|${sc.salesKey}`);
                                                            const profitSource = sourceMap.get(`${mapKey}|${sc.profitKey}`);
                                                            const isEditableQ = row.quarter === "1Q" || row.quarter === "3Q";
                                                            const fy = extractFiscalYear(row.period);
                                                            return (
                                                                <React.Fragment key={sc.segmentName}>
                                                                    <SegOverrideCell value={salesVal} source={salesSource} width={segWidths[sIdx]}
                                                                        editable={isEditableQ && (salesVal === null || salesSource === "manual") && !!onSegmentOverrideSave}
                                                                        isManual={salesSource === "manual"} fiscalYear={fy} quarter={row.quarter} segmentName={sc.segmentName} metric="sales"
                                                                        onSave={onSegmentOverrideSave} onDelete={onSegmentOverrideDelete}
                                                                        isSegActive={activeSegCell?.rowIdx === idx && activeSegCell?.colIdx === sIdx}
                                                                        onActivate={() => { setActiveSegCell({ rowIdx: idx, colIdx: sIdx }); setActiveCell(null); setEditingSegCell(null); }}
                                                                        isSegEditing={editingSegCell?.rowIdx === idx && editingSegCell?.colIdx === sIdx}
                                                                        segEditInitValue={editingSegCell?.rowIdx === idx && editingSegCell?.colIdx === sIdx ? segEditValue : undefined}
                                                                        onSegEditDone={finishSegEditing}
                                                                    />
                                                                    <SegOverrideCell value={profitVal} source={profitSource} width={segWidths[pIdx]}
                                                                        editable={isEditableQ && (profitVal === null || profitSource === "manual") && !!onSegmentOverrideSave}
                                                                        isManual={profitSource === "manual"} fiscalYear={fy} quarter={row.quarter} segmentName={sc.segmentName} metric="operating_profit"
                                                                        onSave={onSegmentOverrideSave} onDelete={onSegmentOverrideDelete}
                                                                        isSegActive={activeSegCell?.rowIdx === idx && activeSegCell?.colIdx === pIdx}
                                                                        onActivate={() => { setActiveSegCell({ rowIdx: idx, colIdx: pIdx }); setActiveCell(null); setEditingSegCell(null); }}
                                                                        isSegEditing={editingSegCell?.rowIdx === idx && editingSegCell?.colIdx === pIdx}
                                                                        segEditInitValue={editingSegCell?.rowIdx === idx && editingSegCell?.colIdx === pIdx ? segEditValue : undefined}
                                                                        onSegEditDone={finishSegEditing}
                                                                        extraClassName="segment-group-end"
                                                                    />
                                                                </React.Fragment>
                                                            );
                                                        })}
                                                    </tr>
                                                ))}
                                                {/* 手入力メモ専用行 (累計セグメント) */}
                                                <ManualMemoRows
                                                    tableType="segment_cum"
                                                    colCount={2 + segmentColumns.length * 2}
                                                    segmentGroupEndIndices={segmentColumns.map((_, i) => 2 + i * 2 + 1)}
                                                    gridData={manualTableMemos?.segment_cum ?? Array.from({ length: MANUAL_MEMO_ROW_COUNT }, () => [])}
                                                    activeManualCell={activeManualCell}
                                                    editingManualCell={editingManualCell}
                                                    editValue={manualEditValue}
                                                    onActivate={selectManualCell}
                                                    onStartEdit={startManualEditing}
                                                    onEditChange={setManualEditValue}
                                                    onCommit={commitManualEdit}
                                                    onCancel={cancelManualEdit}
                                                    editInputRef={editInputRef}
                                                />
                                            </tbody>
                                        </table>
                                    </div>
                                    <div className="pl-table-block">
                                        <div className="pl-table-label">Q単体セグメント（百万円）</div>
                                        <table className="pl-table" style={{ minWidth: segQTableWidth }}>
                                            <thead><tr>
                                                <th style={{ width: 100, minWidth: 100 }}><div className="th-content"><span>PERIOD</span></div></th>
                                                <th style={{ width: 45, minWidth: 45 }}><div className="th-content"><span>Q</span></div></th>
                                                {segmentHeaders.map((eh, si) => <th key={`seg-q-h-${si}`} className={`seg-header-cell ${eh.className || "num-col"} ${si % 2 === 1 ? "segment-group-end" : ""}`} style={{ width: segWidths[si] ?? 90, minWidth: 24 }}><div className="th-content"><span>{eh.label}</span><div className="resize-handle" onMouseDown={(e) => handleSegResizeStart(si, e)} /></div></th>)}
                                            </tr></thead>
                                            <tbody>
                                                {qRows.map((row, idx) => (
                                                    <tr key={`seg-q-${row.period}-${row.quarter}-${idx}`} className={[
                                                        "pl-row",
                                                        selectedPeriod === row.period && selectedQuarter === row.quarter ? "pl-row-selected" : "",
                                                        row.quarter === "FY" ? "pl-row-fy" : "",
                                                        "year-group-row",
                                                        qRows[idx - 1]?.period !== row.period ? "year-group-start" : "",
                                                        qRows[idx + 1]?.period !== row.period ? "year-group-end" : "",
                                                    ].filter(Boolean).join(" ")}>
                                                        <td style={{ width: 100, minWidth: 100 }}>{displayValue(row.period)}</td>
                                                        <td style={{ width: 45, minWidth: 45 }}>{displayValue(row.quarter)}</td>
                                                        {segmentColumns.map((sc, scIdx) => {
                                                            const salesVal = getSegQValue(row.period, row.quarter, sc.salesKey);
                                                            const profitVal = getSegQValue(row.period, row.quarter, sc.profitKey);
                                                            const sIdx = scIdx * 2;
                                                            const pIdx = scIdx * 2 + 1;
                                                            return (
                                                                <React.Fragment key={sc.segmentName}>
                                                                    <td className="num-col seg-data-cell" style={{ width: segWidths[sIdx], minWidth: segWidths[sIdx] }}>{salesVal !== null ? formatMillions(salesVal) : ""}</td>
                                                                    <td className="num-col seg-data-cell segment-group-end" style={{ width: segWidths[pIdx], minWidth: segWidths[pIdx] }}>{profitVal !== null ? formatMillions(profitVal) : ""}</td>
                                                                </React.Fragment>
                                                            );
                                                        })}
                                                    </tr>
                                                ))}
                                                {/* 手入力メモ専用行 (Q単体セグメント) */}
                                                <ManualMemoRows
                                                    tableType="segment_q"
                                                    colCount={2 + segmentColumns.length * 2}
                                                    segmentGroupEndIndices={segmentColumns.map((_, i) => 2 + i * 2 + 1)}
                                                    gridData={manualTableMemos?.segment_q ?? Array.from({ length: MANUAL_MEMO_ROW_COUNT }, () => [])}
                                                    activeManualCell={activeManualCell}
                                                    editingManualCell={editingManualCell}
                                                    editValue={manualEditValue}
                                                    onActivate={selectManualCell}
                                                    onStartEdit={startManualEditing}
                                                    onEditChange={setManualEditValue}
                                                    onCommit={commitManualEdit}
                                                    onCancel={cancelManualEdit}
                                                    editInputRef={editInputRef}
                                                />
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            </div>
                            )}
                        </div>
                    )}
                </>
            )}
            {/* トースト通知 */}
            {toastMessage && (
                <div className="seg-paste-toast">{toastMessage}</div>
            )}
        </div>
    );
}

// ============================================================
// Excel-like メモセル
// ============================================================
function MemoCellExcelBase({
    value,
    width,
    isActive,
    isInRange,
    isEditing,
    editValue,
    onSelect,
    onStartEdit,
    onEditChange,
    onCommit,
    onCancel,
    inputRef,
    className,
    onMouseDown,
    onMouseEnter,
    onMouseEnterRange,
    onPaste,
    onArrowKey,
    useTextarea,
    onMouseDownCaptureEdit,
    onBlurShouldSkip,
}: {
    value: string;
    width?: number;
    isActive: boolean;
    isInRange?: boolean;
    isEditing: boolean;
    editValue: string;
    onSelect: () => void;
    onStartEdit: (val: string) => void;
    onEditChange: (val: string) => void;
    onCommit: () => void;
    onCancel: () => void;
    inputRef?: React.RefObject<HTMLElement | null>;
    className?: string;
    onMouseDown?: (e: React.MouseEvent) => void;
    onMouseEnter?: () => void;
    /** ドラッグ範囲選択用: handleCellMouseEnter を専用に担当するコールバック */
    onMouseEnterRange?: () => void;
    onPaste?: (e: React.ClipboardEvent) => void;
    /** editing 中の Arrow キーでセル移動を行うコールバック（segment_manual 専用） */
    onArrowKey?: (dir: "up" | "down" | "left" | "right") => void;
    /** true の場合 textarea を使用（未指定時は className=="memo-cell" の場合のみ） */
    useTextarea?: boolean;
    /**
     * キャプチャフェーズの mousedown コールバック。
     * 親の onMouseDown / focusGrid / selectCell より先に発火されるため、
     * PL メモセルでブラウザ native focus を完全遺断して編集開始するために使用。
     */
    onMouseDownCaptureEdit?: (e: React.MouseEvent) => void;
    /**
     * onBlur ガード: この関数が true を返した場合、onCommit を呼ばずに return する。
     * PL メモ編集開始直後の spurious blur（relatedTarget=null）による即編集終了を防ぐために使用。
     */
    onBlurShouldSkip?: (e: React.FocusEvent<HTMLTextAreaElement | HTMLInputElement>) => boolean;
}) {
    const preview = value ? value.replace(/[\r\n]+/g, " ").trim() : "";
    const extraClass = className || "memo-cell";
    // useTextarea prop で明示指定、または className=="memo-cell" の場合に textarea を使用
    const renderAsTextarea = useTextarea === true || extraClass === "memo-cell";

    // isActive（選択中）または isEditing（編集中）のとき textarea/input を常に描画する。
    // クリック直後から IME を使えるよう、textarea を autoFocus で確実にフォーカスする。
    if (isEditing) {
        // renderAsTextarea=true: textarea (ALt+Enterで改行)
        // false: input (従来通り)
        return (
            <td
                style={{ width, minWidth: width, maxWidth: width, overflow: "hidden" }}
                className={`${extraClass} memo-cell-editing`}
            >
                {renderAsTextarea ? (
                    <textarea
                        ref={(el) => {
                            // external inputRef に attach
                            if (inputRef) (inputRef as React.MutableRefObject<HTMLElement | null>).current = el;
                            if (el) {
                                // マウント直後に強制 focus（autoFocus だけでは focus が奪われる場合の対策）
                                el.focus();
                                setTimeout(() => {
                                }, 0);
                            }
                        }}
                        className="memo-inline-input memo-inline-textarea"
                        value={editValue}
                        onChange={(e) => onEditChange(e.target.value)}
                        onCompositionEnd={(e) => onEditChange(e.currentTarget.value)}
                        onBlur={(e) => {
                            // 編集開始直後の spurious blur は無視（PL メモ専用ガード）
                            if (onBlurShouldSkip?.(e)) {
                                return;
                            }
                            onCommit();
                        }}
                        onPaste={onPaste}
                        onKeyDown={(e) => {
                            // IME変換中はセル移動キーを無視
                            if (e.nativeEvent.isComposing || e.key === "Process" || e.keyCode === 229) return;
                            if (onArrowKey && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
                                e.preventDefault(); e.stopPropagation();
                                const dir = e.key === "ArrowUp" ? "up" : e.key === "ArrowDown" ? "down" : e.key === "ArrowLeft" ? "left" : "right";
                                onArrowKey(dir); return;
                            }
                            if (e.key === "Tab") {
                                e.preventDefault();
                                if (onArrowKey) { onArrowKey(e.shiftKey ? "left" : "right"); }
                                else { onCommit(); }
                                return;
                            }
                            if (e.key === "Enter" && e.altKey) {
                                e.preventDefault();
                                const ta = e.currentTarget;
                                const start = ta.selectionStart ?? 0;
                                const end = ta.selectionEnd ?? 0;
                                const newVal = editValue.substring(0, start) + "\n" + editValue.substring(end);
                                onEditChange(newVal);
                                requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = start + 1; });
                                return;
                            }
                            if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); e.currentTarget.blur(); return; }
                            if (e.key === "Escape") { e.preventDefault(); onCancel(); return; }
                            e.stopPropagation();
                        }}
                        autoFocus
                    />
                ) : (
                    <input
                        ref={inputRef as React.RefObject<HTMLInputElement>}
                        className="memo-inline-input"
                        value={editValue}
                        onChange={(e) => onEditChange(e.target.value)}
                        onCompositionEnd={(e) => onEditChange(e.currentTarget.value)}
                        onBlur={(e) => {
                            if (onBlurShouldSkip?.(e)) return;
                            onCommit();
                        }}
                        onPaste={onPaste}
                        onKeyDown={(e) => {
                            if (e.nativeEvent.isComposing || e.key === "Process" || e.keyCode === 229) return;
                            if (onArrowKey && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
                                e.preventDefault(); e.stopPropagation();
                                const dir = e.key === "ArrowUp" ? "up" : e.key === "ArrowDown" ? "down" : e.key === "ArrowLeft" ? "left" : "right";
                                onArrowKey(dir); return;
                            }
                            if (e.key === "Tab") {
                                e.preventDefault();
                                if (onArrowKey) { onArrowKey(e.shiftKey ? "left" : "right"); }
                                else { onCommit(); }
                                return;
                            }
                            if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); e.currentTarget.blur(); return; }
                            if (e.key === "Escape") { e.preventDefault(); onCancel(); return; }
                            e.stopPropagation();
                        }}
                        autoFocus
                    />
                )}
            </td>
        );
    }

    return (
        <td
            style={{ width, minWidth: width, maxWidth: width, overflow: "hidden" }}
            className={`${extraClass} memo-cell-selectable ${isActive ? "memo-cell-active" : ""} ${isInRange ? "memo-cell-in-range" : ""}`}
            onClick={(e) => { e.stopPropagation(); }}
            onDoubleClick={(e) => { e.stopPropagation(); onStartEdit(value); }}
            onMouseDown={(e) => {
                e.preventDefault(); // ブラウザ native focus（tabindex=0 の祖先 div へのフォーカス）を防止
                e.stopPropagation();
                // セグメントメモ（handleSegManualCellMouseDown）と同じ方式:
                // onSelect() が startEditing/startManualEditing に繋がるよう呼び出し元で設定する
                onSelect();
                onMouseDown?.(e);
            }}
            onMouseDownCapture={(e) => {
                // キャプチャフェーズで親の全ハンドラを遺断して編集開始（PL メモ専用）
                if (onMouseDownCaptureEdit) {
                    e.preventDefault(); // ブラウザ native focus 完全遺断
                    e.stopPropagation(); // 親の onMouseDown / focusGrid / selectCell 遺断
                    onMouseDownCaptureEdit(e);
                }
            }}
            onMouseEnter={() => { onMouseEnter?.(); onMouseEnterRange?.(); }}
            title={preview}
        >
            {preview
                ? preview
                : extraClass === "manual-memo-cell"
                    ? ""  // segment_manual セル: 空欄は空文字（placeholder 廃止）
                    : <span className="memo-empty">–</span>
            }
        </td>
    );
}

// ============================================================
// Segment Override Cell — PL テーブル内のセグメント編集セル
// ============================================================
function SegOverrideCell({
    value,
    source,
    width,
    editable,
    isManual,
    fiscalYear,
    quarter,
    segmentName,
    metric,
    onSave,
    onDelete,
    isSegActive,
    onActivate,
    isSegEditing,
    segEditInitValue,
    onSegEditDone,
    isInRange,
    onRangeMouseDown,
    onRangeMouseEnter,
    extraClassName = "",
}: {
    value: number | null;
    source?: string;
    width: number;
    editable: boolean;
    isManual: boolean;
    fiscalYear: number;
    quarter: string;
    segmentName: string;
    metric: string;
    onSave?: FinancialsTableProps["onSegmentOverrideSave"];
    onDelete?: FinancialsTableProps["onSegmentOverrideDelete"];
    isSegActive?: boolean;
    onActivate?: () => void;
    /** 親から編集開始を制御 */
    isSegEditing?: boolean;
    segEditInitValue?: string;
    onSegEditDone?: () => void;
    isInRange?: boolean;
    onRangeMouseDown?: (e: React.MouseEvent) => void;
    onRangeMouseEnter?: () => void;
    /** 追加 className (segment-group-end 等) */
    extraClassName?: string;
}) {
    const [editing, setEditing] = useState(false);
    const [inputVal, setInputVal] = useState("");
    const [saving, setSaving] = useState(false);
    const inputRef = useRef<HTMLInputElement>(null);

    // 親から編集開始のシグナルを受け取る
    useEffect(() => {
        if (isSegEditing && !editing) {
            const canEdit = editable || isManual;
            if (canEdit) {
                if (segEditInitValue !== undefined && segEditInitValue !== "") {
                    // 直接入力: キー入力値をセット
                    setInputVal(segEditInitValue);
                } else if (isManual) {
                    setInputVal(value !== null ? String(value) : "");
                } else {
                    setInputVal("");
                }
                setEditing(true);
                setTimeout(() => inputRef.current?.focus(), 0);
            }
        }
    }, [isSegEditing]); // eslint-disable-line react-hooks/exhaustive-deps

    const handleSave = useCallback(async () => {
        setEditing(false);
        onSegEditDone?.(); // 親に編集完了を通知
        const trimmed = inputVal.trim();
        if (!trimmed || !onSave) return;
        const numVal = Number(trimmed);
        if (isNaN(numVal)) return;
        setSaving(true);
        try {
            await onSave(fiscalYear, quarter, segmentName, metric, numVal);
        } catch (err) {
            console.error("Seg override save failed:", err);
        } finally {
            setSaving(false);
        }
    }, [inputVal, onSave, onSegEditDone, fiscalYear, quarter, segmentName, metric]);

    const handleCancel = useCallback(() => {
        setEditing(false);
        onSegEditDone?.();
    }, [onSegEditDone]);

    const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
        if (e.key === "Enter") { e.preventDefault(); handleSave(); }
        else if (e.key === "Escape") { e.preventDefault(); handleCancel(); }
        e.stopPropagation();
    }, [handleSave, handleCancel]);

    const handleDeleteOverride = useCallback(async (e: React.MouseEvent) => {
        e.stopPropagation();
        if (!onDelete) return;
        if (!confirm("この手入力値を削除しますか？")) return;
        setSaving(true);
        try {
            await onDelete(fiscalYear, quarter, segmentName, metric);
        } catch (err) {
            console.error("Seg override delete failed:", err);
        } finally {
            setSaving(false);
        }
    }, [onDelete, fiscalYear, quarter, segmentName, metric]);

    // 編集中の input で paste イベントを横取りし、テーブルレベルのハンドラへ伝搬させる
    const handleInputPaste = useCallback((e: React.ClipboardEvent<HTMLInputElement>) => {
        const text = e.clipboardData.getData("text/plain");
        if (text && (text.includes("\t") || text.includes("\n"))) {
            e.preventDefault();
            setEditing(false);
            onSegEditDone?.();
            const tableDiv = (e.target as HTMLElement).closest(".pl-section");
            if (tableDiv) {
                const newEvent = new ClipboardEvent("paste", {
                    clipboardData: e.clipboardData as unknown as DataTransfer,
                    bubbles: true,
                    cancelable: true,
                });
                tableDiv.dispatchEvent(newEvent);
            }
        }
    }, [onSegEditDone]);

    const displayVal = value !== null ? formatMillions(value) : "–";
    const canEdit = editable || isManual;

    // クリック: アクティブ化 + 編集可能なら編集開始
    const handleCellClick = useCallback((e: React.MouseEvent) => {
        e.stopPropagation();
        onActivate?.();
        if (canEdit) {
            if (isManual) {
                setInputVal(value !== null ? String(value) : "");
            } else {
                setInputVal("");
            }
            setEditing(true);
            setTimeout(() => inputRef.current?.focus(), 0);
        }
    }, [canEdit, isManual, value, onActivate]);

    if (editing) {
        return (
            <td className="num-col seg-data-cell seg-cell-active" style={{ width, minWidth: width, maxWidth: width, overflow: "hidden" }}>
                <div className="segment-cell-edit">
                    <input
                        ref={inputRef}
                        type="number"
                        className="segment-cell-input"
                        placeholder="百万円"
                        value={inputVal}
                        onChange={(e) => setInputVal(e.target.value)}
                        onBlur={handleSave}
                        onKeyDown={handleKeyDown}
                        onPaste={handleInputPaste}
                        disabled={saving}
                        autoFocus
                    />
                </div>
            </td>
        );
    }

    return (
        <td
            className={`num-col seg-data-cell ${editable && !isManual ? "seg-editable" : ""} ${isManual ? "seg-manual-editable" : ""} ${saving ? "seg-saving" : ""} ${isSegActive ? "seg-cell-active" : ""} ${isInRange ? "cell-in-range" : ""} ${extraClassName}`}
            style={{ width, minWidth: width, maxWidth: width, overflow: "hidden" }}
            onClick={handleCellClick}
            onMouseDown={(e) => { onRangeMouseDown?.(e); }}
            onMouseEnter={() => { onRangeMouseEnter?.(); }}
            title={canEdit ? (isManual ? "クリックで再編集" : "クリックして入力") : "クリックして選択（ペースト用）"}
        >
            <div className="segment-cell-display">
                {editable && value === null ? (
                    <span className="segment-cell-placeholder">入力</span>
                ) : (
                    <span>{displayVal}</span>
                )}
                {isManual && (
                    <span
                        className="segment-manual-badge"
                        onClick={handleDeleteOverride}
                        title="手入力値 — クリックで削除"
                    >
                        M
                    </span>
                )}
            </div>
        </td>
    );
}

// ============================================================
// SegmentManualMemoTable — MEMO モード用テーブル
// PERIOD/Q は cumRows から取得（読み取り専用）、右12列が自由入力
// ============================================================
function SegmentManualMemoTable({
    rows,
    gridData,
    activeManualCell,
    editingManualCell,
    editValue,
    onStartEdit,
    onEditChange,
    onCommit,
    onCancel,
    editInputRef,
    onSegmentPaste,
    segManualSel,
    onCellMouseDown,
    onCellMouseEnter,
    onCopy,
    onArrowKey,
    columnHeaders,
    onHeaderEdit,
}: {
    rows: { period: string; quarter: string }[];
    gridData: string[][];
    activeManualCell: { tableType: ManualTableType; rowIdx: number; colIdx: number } | null;
    editingManualCell: { tableType: ManualTableType; rowIdx: number; colIdx: number } | null;
    editValue: string;
    onStartEdit: (coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }, initVal: string) => void;
    onEditChange: (val: string) => void;
    onCommit: () => void;
    onCancel: () => void;
    editInputRef?: React.RefObject<HTMLElement | null>;
    /** PL側のグローバル状態に依存しない専用ペーストハンドラ */
    onSegmentPaste?: (e: React.ClipboardEvent, coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }) => void;
    /** 現在の範囲選択 (drag or Shift+Arrow) */
    segManualSel?: { startRow: number; startCol: number; endRow: number; endCol: number } | null;
    /** ドラッグ開始コールバック */
    onCellMouseDown?: (rowIdx: number, colIdx: number) => void;
    /** ドラッグ中の範囲拡張コールバック */
    onCellMouseEnter?: (rowIdx: number, colIdx: number) => void;
    /** copy イベントハンドラ（範囲選択 → TSV → Excel 貼り付け） */
    onCopy?: (e: React.ClipboardEvent) => void;
    /** editing 中の Arrow キーでセル確定 + 移動を行うコールバック */
    onArrowKey?: (dir: "up" | "down" | "left" | "right") => void;
    /** 列ヘッダー文字列配列 (列 0-11、デフォルト "1"-"12") */
    columnHeaders?: string[];
    /** ヘッダー編集コールバック (colIdx: 0-based) */
    onHeaderEdit?: (colIdx: number, value: string) => void;
}) {
    const COL = SEGMENT_MANUAL_COL_COUNT; // 12

    // 範囲選択の正規化（start ≤ end を保証）
    const selMin = segManualSel ? {
        r: Math.min(segManualSel.startRow, segManualSel.endRow),
        c: Math.min(segManualSel.startCol, segManualSel.endCol),
    } : null;
    const selMax = segManualSel ? {
        r: Math.max(segManualSel.startRow, segManualSel.endRow),
        c: Math.max(segManualSel.startCol, segManualSel.endCol),
    } : null;
    return (
        <div className="pl-scroll-area" style={{ maxHeight: 480 }} onCopy={onCopy}>
            <div className="pl-dual-tables">
                <div className="pl-table-block">
                    <div className="pl-table-label">セグメント手入力メモ — {rows.length}行 × {COL}列</div>
                    <table className="pl-table" style={{ minWidth: 100 + 45 + COL * 72 }}>
                        <thead>
                            <tr>
                                <th style={{ width: 100, minWidth: 100 }}><div className="th-content"><span>PERIOD</span></div></th>
                                <th style={{ width: 45, minWidth: 45 }}><div className="th-content"><span>Q</span></div></th>
                                {Array.from({ length: COL }, (_, i) => {
                                    const headerLabel = columnHeaders?.[i] ?? String(i + 1);
                                    return (
                                        <th key={i} style={{ width: 72, minWidth: 50, padding: 0 }}>
                                            <div className="th-content" style={{ padding: 0 }}>
                                                <input
                                                    className="seg-manual-header-input"
                                                    value={headerLabel}
                                                    placeholder={String(i + 1)}
                                                    onChange={(e) => {
                                                        onHeaderEdit?.(i, e.target.value);
                                                    }}
                                                    onBlur={(e) => {
                                                        // blur 時は確定済みなので何もしない
                                                    }}
                                                    onKeyDown={(e) => {
                                                        if (e.key === "Enter" || e.key === "Escape") {
                                                            e.preventDefault();
                                                            (e.currentTarget as HTMLInputElement).blur();
                                                        }
                                                        e.stopPropagation();
                                                    }}
                                                    onMouseDown={(e) => e.stopPropagation()}
                                                    onClick={(e) => e.stopPropagation()}
                                                />
                                            </div>
                                        </th>
                                    );
                                })}
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map((row, rowIdx) => (
                                <tr
                                    key={`seg-manual-${rowIdx}`}
                                    className={[
                                        "manual-memo-row",
                                        row.quarter === "FY" ? "pl-row-fy" : "",
                                    ].filter(Boolean).join(" ")}
                                >
                                    {/* PERIOD 列 — 読み取り専用 */}
                                    <td style={{ width: 100, minWidth: 100, color: "var(--text-secondary)", userSelect: "none" }}>
                                        {displayValue(row.period)}
                                    </td>
                                    {/* Q 列 — 読み取り専用 */}
                                    <td style={{ width: 45, minWidth: 45, color: "var(--text-secondary)", userSelect: "none" }}>
                                        {displayValue(row.quarter)}
                                    </td>
                                    {/* 自由入力列 1〜12 */}
                                    {Array.from({ length: COL }, (_, colIdx) => {
                                        const isActive = activeManualCell?.tableType === "segment_manual"
                                            && activeManualCell?.rowIdx === rowIdx
                                            && activeManualCell?.colIdx === colIdx;
                                        const isEditing = editingManualCell?.tableType === "segment_manual"
                                            && editingManualCell?.rowIdx === rowIdx
                                            && editingManualCell?.colIdx === colIdx;
                                        const isInRange = (() => {
                                            if (!segManualSel) return false;
                                            const r1 = Math.min(segManualSel.startRow, segManualSel.endRow);
                                            const r2 = Math.max(segManualSel.startRow, segManualSel.endRow);
                                            const c1 = Math.min(segManualSel.startCol, segManualSel.endCol);
                                            const c2 = Math.max(segManualSel.startCol, segManualSel.endCol);
                                            return rowIdx >= r1 && rowIdx <= r2 && colIdx >= c1 && colIdx <= c2;
                                        })();
                                        const cellValue = gridData[rowIdx]?.[colIdx] ?? "";
                                        const coord = { tableType: "segment_manual" as ManualTableType, rowIdx, colIdx };
                                        return (
                                            <MemoCellExcel
                                                key={colIdx}
                                                value={cellValue}
                                                isActive={isActive}
                                                isInRange={isInRange}
                                                isEditing={isEditing}
                                                editValue={editValue}
                                                useTextarea={true}
                                                onSelect={() => {
                                                    // handleSegManualCellMouseDown 内で setActiveManualCell 済
                                                }}
                                                onStartEdit={(val) => onStartEdit(coord, val)}
                                                onEditChange={onEditChange}
                                                onCommit={onCommit}
                                                onCancel={onCancel}
                                                inputRef={isEditing ? editInputRef : undefined}
                                                className="manual-memo-cell"
                                                onPaste={onSegmentPaste
                                                    ? (e) => onSegmentPaste(e, coord)
                                                    : undefined}
                                                onMouseDown={() => onCellMouseDown?.(rowIdx, colIdx)}
                                                onMouseEnter={() => onCellMouseEnter?.(rowIdx, colIdx)}
                                                onArrowKey={onArrowKey ? (dir) => onArrowKey(dir) : undefined}
                                            />
                                        );
                                    })}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}

/**
 * React.memo ラッパー。
 * onMouseDownCaptureEdit は「(e) => stableHandler(e, cellValue)」という
 * 薄いラッパーが毎レンダーで生成されるため比較対象から除外する。
 * 実体（stableHandler）は安定参照なので、除外しても挙動に差異はない。
 */
const MemoCellExcel = React.memo(
    MemoCellExcelBase,
    (prev, next) =>
        prev.value === next.value &&
        prev.width === next.width &&
        prev.isActive === next.isActive &&
        prev.isInRange === next.isInRange &&
        prev.isEditing === next.isEditing &&
        prev.editValue === next.editValue &&
        prev.className === next.className &&
        prev.useTextarea === next.useTextarea &&
        prev.inputRef === next.inputRef &&
        prev.onSelect === next.onSelect &&
        prev.onStartEdit === next.onStartEdit &&
        prev.onEditChange === next.onEditChange &&
        prev.onCommit === next.onCommit &&
        prev.onCancel === next.onCancel &&
        prev.onMouseDown === next.onMouseDown &&
        prev.onMouseEnter === next.onMouseEnter &&
        prev.onMouseEnterRange === next.onMouseEnterRange &&
        prev.onPaste === next.onPaste &&
        prev.onArrowKey === next.onArrowKey &&
        prev.onBlurShouldSkip === next.onBlurShouldSkip
        // onMouseDownCaptureEdit は比較しない
);

// ============================================================
// ManualMemoRows — 手入力メモ専用行 (2行)
// MEMO A/B と同じ MemoCellExcel コンポーネントを使用
// ============================================================
function ManualMemoRows({
    tableType,
    colCount,
    colOffset = 0,
    widths,
    rowCount = MANUAL_MEMO_ROW_COUNT,
    segmentGroupEndIndices,
    gridData,
    activeManualCell,
    editingManualCell,
    editValue,
    onActivate,
    onStartEdit,
    onEditChange,
    onCommit,
    onCancel,
    editInputRef,
}: {
    tableType: ManualTableType;
    colCount: number;
    colOffset?: number; // 先頭何列をスキップするか (0=スキップなし, 2=period/q を除外)
    widths?: number[]; // 各表示列の幅 (colOffset 適用後の表示列順)
    rowCount?: number;
    segmentGroupEndIndices?: number[];
    gridData: string[][];
    activeManualCell: { tableType: ManualTableType; rowIdx: number; colIdx: number } | null;
    editingManualCell: { tableType: ManualTableType; rowIdx: number; colIdx: number } | null;
    editValue: string;
    onActivate: (coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }) => void;
    onStartEdit: (coord: { tableType: ManualTableType; rowIdx: number; colIdx: number }, initVal: string) => void;
    onEditChange: (val: string) => void;
    onCommit: () => void;
    onCancel: () => void;
    /** MEMO A/B と同じ editInputRef を共有して明示的フォーカスを実現 */
    editInputRef?: React.RefObject<HTMLElement | null>;
}) {
    return (
        <>
            {Array.from({ length: rowCount }, (_, rowIdx) => rowIdx).map((rowIdx) => (
                <tr key={`manual-${tableType}-${rowIdx}`} className={`manual-memo-row manual-memo-row-${rowIdx + 1}`}>
                    {Array.from({ length: colCount }, (_, i) => {
                        const colIdx = i + colOffset; // gridData / activeCell の実インデックス
                        const isActive = activeManualCell?.tableType === tableType
                            && activeManualCell?.rowIdx === rowIdx
                            && activeManualCell?.colIdx === colIdx;
                        const isEditing = editingManualCell?.tableType === tableType
                            && editingManualCell?.rowIdx === rowIdx
                            && editingManualCell?.colIdx === colIdx;
                        const cellValue = gridData[rowIdx]?.[colIdx] ?? "";
                        const isGroupEnd = segmentGroupEndIndices?.includes(colIdx) ?? false;
                        const isPLGpMargin = (tableType === "pl_cum" || tableType === "pl_q") && colIdx === 4;
                        const isPLOpMargin = (tableType === "pl_cum" || tableType === "pl_q") && colIdx === 7;
                        const extraClass = `manual-memo-cell${isPLGpMargin ? " gp-margin-col" : ""}${isPLOpMargin ? " op-margin-col" : ""}${isGroupEnd ? " segment-group-end" : ""}`;

                        return (
                            // MemoCellExcel を流用: MEMO A/B と完全同一の操作感
                            <MemoCellExcel
                                key={i}
                                value={cellValue}
                                isActive={isActive}
                                isEditing={isEditing}
                                editValue={editValue}
                                onSelect={() => onStartEdit({ tableType, rowIdx, colIdx }, cellValue)}
                                onStartEdit={(val) => onStartEdit({ tableType, rowIdx, colIdx }, val)}
                                onEditChange={onEditChange}
                                onCommit={onCommit}
                                onCancel={onCancel}
                                inputRef={isEditing ? editInputRef : undefined}
                                className={extraClass}
                                width={widths?.[i]}
                            />
                        );
                    })}
                </tr>
            ))}
        </>
    );
}

export default React.memo(FinancialsTable);

