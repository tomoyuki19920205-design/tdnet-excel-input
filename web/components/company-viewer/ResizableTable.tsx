"use client";

import React, { useState, useCallback, useEffect, useRef } from "react";

// ============================================================
// useColumnResize — 列幅ドラッグ変更フック
// ============================================================
export interface ColumnDef {
    key: string;
    label: string;
    initialWidth: number;
    className?: string;
}

interface UseColumnResizeOptions {
    storageKey: string;
    columns: ColumnDef[];
}

export function useColumnResize({ storageKey, columns }: UseColumnResizeOptions) {
    const [widths, setWidths] = useState<number[]>(() => {
        // localStorage から復元
        if (typeof window !== "undefined") {
            try {
                const saved = localStorage.getItem(`col-widths-${storageKey}`);
                if (saved) {
                    const parsed = JSON.parse(saved) as number[];
                    if (parsed.length === columns.length) return parsed;
                }
            } catch { /* ignore */ }
        }
        return columns.map((c) => c.initialWidth);
    });

    const dragState = useRef<{
        colIndex: number;
        startX: number;
        startWidth: number;
    } | null>(null);

    // 幅を永続化
    useEffect(() => {
        try {
            localStorage.setItem(`col-widths-${storageKey}`, JSON.stringify(widths));
        } catch { /* ignore */ }
    }, [widths, storageKey]);

    const handleMouseDown = useCallback(
        (colIndex: number, e: React.MouseEvent) => {
            e.preventDefault();
            e.stopPropagation();
            dragState.current = {
                colIndex,
                startX: e.clientX,
                startWidth: widths[colIndex],
            };

            const handleMouseMove = (moveEvent: MouseEvent) => {
                if (!dragState.current) return;
                const diff = moveEvent.clientX - dragState.current.startX;
                const newWidth = Math.max(24, dragState.current.startWidth + diff);
                setWidths((prev) => {
                    const next = [...prev];
                    next[dragState.current!.colIndex] = newWidth;
                    return next;
                });
            };

            const handleMouseUp = () => {
                dragState.current = null;
                document.removeEventListener("mousemove", handleMouseMove);
                document.removeEventListener("mouseup", handleMouseUp);
                document.body.style.cursor = "";
                document.body.style.userSelect = "";
            };

            document.addEventListener("mousemove", handleMouseMove);
            document.addEventListener("mouseup", handleMouseUp);
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
        },
        [widths]
    );

    return { widths, handleMouseDown };
}

// ============================================================
// ResizableTable — 列幅ドラッグ対応テーブル
// ============================================================
interface ResizableTableProps {
    columns: ColumnDef[];
    storageKey: string;
    children: (widths: number[]) => React.ReactNode;
    headerExtra?: React.ReactNode;
}

export default function ResizableTable({
    columns,
    storageKey,
    children,
    headerExtra,
}: ResizableTableProps) {
    const { widths, handleMouseDown } = useColumnResize({ storageKey, columns });

    return (
        <div className="table-wrapper">
            <table className="data-table resizable-table">
                <thead>
                    <tr>
                        {columns.map((col, idx) => (
                            <th
                                key={col.key}
                                className={col.className || ""}
                                style={{ width: widths[idx], minWidth: 40 }}
                            >
                                <div className="th-content">
                                    <span>{col.label}</span>
                                    <div
                                        className="resize-handle"
                                        onMouseDown={(e) => handleMouseDown(idx, e)}
                                    />
                                </div>
                            </th>
                        ))}
                        {headerExtra}
                    </tr>
                </thead>
                {children(widths)}
            </table>
        </div>
    );
}
