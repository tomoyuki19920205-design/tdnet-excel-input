#!/usr/bin/env python3
# ============================================================
# build_viewer_pq.py
# ============================================================
# Power Query 方式の viewer_pq_v1.xlsx を生成する。
#
# Phase 1: ブック作成 + PQ定義 + .xlsx保存
# Phase 2: 再オープン + ListObjects.Add で PQ テーブルロード
# Phase 3: 数式配置 + 保存
#
# 実行:
#   .\.venv\Scripts\python.exe build_viewer_pq.py
# ============================================================
from __future__ import annotations

import argparse
import io as _io
import os
import sys
import time
from pathlib import Path

# Windows コンソール UTF-8 対応
if sys.stdout and hasattr(sys.stdout, "encoding"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = _io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
if sys.stderr and hasattr(sys.stderr, "encoding"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
        sys.stderr = _io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )


def main():
    parser = argparse.ArgumentParser(description="Power Query viewer 生成")
    parser.add_argument("--data-path", default=r"C:\Users\takuy\OneDrive\data.xlsx")
    parser.add_argument("--output", "-o", default=r"C:\Users\takuy\OneDrive\viewer_pq_v1.xlsx")
    args = parser.parse_args()

    data_path = Path(args.data_path).resolve()
    output_path = Path(args.output).resolve()

    if not data_path.exists():
        print(f"ERROR: data.xlsx not found: {data_path}")
        sys.exit(1)

    print()
    print("=" * 50)
    print("  viewer_pq_v1.xlsx 生成 (Power Query)")
    print("=" * 50)
    print(f"  data: {data_path}")
    print(f"  out:  {output_path}")
    print()

    import xlwings as xw

    import shutil
    import tempfile

    # OneDrive への直接SaveAsが失敗するため、tempフォルダに作成後に移動
    tmp_dir = tempfile.mkdtemp(prefix="viewer_pq_")
    tmp_path = Path(tmp_dir) / "viewer_pq_v1.xlsx"
    print(f"  tmp:  {tmp_path}")
    print()

    app = xw.App(visible=False)
    app.display_alerts = False
    app.screen_updating = False

    try:
        print("[Phase 1] PQ定義...")
        _phase1(app, data_path, tmp_path)
        print("  done")

        print("[Phase 2] テーブルロード...")
        _phase2(app, tmp_path)
        print("  done")

        print("[Phase 3] 数式配置...")
        _phase3(app, tmp_path)
        print("  done")
    finally:
        try:
            app.screen_updating = True
            app.display_alerts = True
            app.quit()
        except Exception:
            pass

    # OneDrive に移動
    print()
    print(f"[Move] {tmp_path} -> {output_path}")
    shutil.move(str(tmp_path), str(output_path))
    # temp dir cleanup
    try:
        os.rmdir(tmp_dir)
    except Exception:
        pass

    print()
    print("=" * 50)
    print("  DONE!")
    print("=" * 50)
    print(f"  output: {output_path}")
    print()


def _phase1(app, data_path, output_path):
    """ブック作成 + Power Query 定義 + 保存"""
    wb = app.books.add()

    config_ws = wb.sheets[0]
    config_ws.name = "CONFIG"
    config_ws.range("A1").value = "data_path"
    config_ws.range("B1").value = str(data_path)

    wb.sheets.add("PQ_DATA", after=config_ws)
    wb.sheets.add("PQ_SEGMENT", after=wb.sheets["PQ_DATA"])
    wb.sheets.add("PL_VIEW", before=config_ws)

    dp = str(data_path).replace("\\", "\\\\")

    m_data = (
        'let\n'
        f'    Source = Excel.Workbook(File.Contents("{dp}"), null, true),\n'
        '    Sheet = Source{[Item="DATA",Kind="Sheet"]}[Data],\n'
        '    Skip = Table.Skip(Sheet, 1),\n'
        '    H = Table.PromoteHeaders(Skip, [PromoteAllScalars=true]),\n'
        '    T = Table.TransformColumnTypes(H,{{"ticker", Int64.Type}, '
        '{"period", type text}, {"quarter", type text}, '
        '{"sales", type number}, {"gross_profit", type number}, '
        '{"operating_profit", type number}, {"source", type text}, '
        '{"updated_at", type text}, {"recency_key", type text}, '
        '{"lookup_key", type text}}),\n'
        '    QSK = Table.AddColumn(T, "quarter_sort_key", each '
        'if [quarter] = "1Q" then 1 '
        'else if [quarter] = "2Q" then 2 '
        'else if [quarter] = "3Q" then 3 '
        'else if [quarter] = "FY" then 4 '
        'else 0, Int64.Type),\n'
        '    SK = Table.AddColumn(QSK, "sort_key", each '
        'Date.Year(Date.From([period])) * 10 + [quarter_sort_key], Int64.Type)\n'
        'in\n    SK'
    )
    m_seg = (
        'let\n'
        f'    Source = Excel.Workbook(File.Contents("{dp}"), null, true),\n'
        '    Sheet = Source{[Item="SEGMENT_DATA",Kind="Sheet"]}[Data],\n'
        '    Skip = Table.Skip(Sheet, 1),\n'
        '    H = Table.PromoteHeaders(Skip, [PromoteAllScalars=true]),\n'
        '    T = Table.TransformColumnTypes(H,{{"ticker", Int64.Type}, '
        '{"period", type text}, {"quarter", type text}, '
        '{"segment_name", type text}, {"metric_name", type text}, '
        '{"value_mil", type number}, {"recency_key", type text}, '
        '{"lookup_key", type text}})\nin    T'
    )

    wb.api.Queries.Add("Query_DATA", m_data)
    wb.api.Queries.Add("Query_SEGMENT", m_seg)

    # xlOpenXMLWorkbook = 51
    wb.api.SaveAs(str(output_path), FileFormat=51)
    wb.close()


def _phase2(app, output_path):
    """テーブルロード: ListObjects.Add + Refresh"""
    import win32com.client

    wb = app.books.open(str(output_path))
    xl_wb = wb.api

    # --- PQ_DATA -> Tbl_DATA ---
    ws_data = xl_wb.Sheets("PQ_DATA")
    _add_pq_table(ws_data, "Query_DATA", "Tbl_DATA")

    # --- PQ_SEGMENT -> Tbl_SEGMENT ---
    ws_seg = xl_wb.Sheets("PQ_SEGMENT")
    _add_pq_table(ws_seg, "Query_SEGMENT", "Tbl_SEGMENT")

    # テーブル確認
    for s_name in ["PQ_DATA", "PQ_SEGMENT"]:
        ws = xl_wb.Sheets(s_name)
        for i in range(1, ws.ListObjects.Count + 1):
            lo = ws.ListObjects(i)
            print(f"    {lo.Name}: {lo.ListRows.Count} rows")

    wb.save()
    wb.close()


def _add_pq_table(ws, query_name, table_name):
    """COM で ListObjects.Add + QueryTable を使って PQ テーブルをロードする。"""
    conn_str = (
        "OLEDB;Provider=Microsoft.Mashup.OleDb.1;"
        "Data Source=$Workbook$;"
        f"Location={query_name};"
        'Extended Properties=""'
    )

    # ListObjects.Add(SourceType, Source, LinkSource, XlListObjectHasHeaders, Destination)
    # SourceType: xlSrcExternal = 0 (VBA), but via COM the positional enum is 0
    # We use named params via COM

    dest = ws.Range("A1")

    # Array for Source (must be an array/tuple)
    import win32com.client
    src_array = win32com.client.VARIANT(
        win32com.client.pythoncom.VT_ARRAY | win32com.client.pythoncom.VT_VARIANT,
        [conn_str]
    )

    lo = ws.ListObjects.Add(
        0,           # xlSrcExternal
        src_array,   # Source (as array)
        True,        # LinkSource
        1,           # xlYes (HasHeaders)
        dest,        # Destination
    )
    lo.Name = table_name

    qt = lo.QueryTable
    # CommandText must be set as array for PQ
    qt.CommandText = f"SELECT * FROM [{query_name}]"
    qt.BackgroundQuery = False
    qt.RefreshStyle = 1   # xlOverwriteCells

    print(f"    Refreshing {table_name}...")
    qt.Refresh(False)     # BackgroundQuery=False
    print(f"    {table_name} loaded: {lo.ListRows.Count} rows")


def _phase3(app, output_path):
    """数式配置 + 接続設定 + 保存"""
    wb = app.books.open(str(output_path))

    # 接続設定
    for i in range(1, wb.api.Connections.Count + 1):
        try:
            c = wb.api.Connections(i)
            c.OLEDBConnection.BackgroundQuery = False
            c.OLEDBConnection.RefreshOnFileOpen = True
            c.OLEDBConnection.RefreshPeriod = 5
        except Exception:
            pass

    # 数式配置
    pl_ws = wb.sheets["PL_VIEW"]
    _build_pl_view(pl_ws)

    # シート非表示
    try:
        wb.sheets["PQ_DATA"].api.Visible = 0
        wb.sheets["PQ_SEGMENT"].api.Visible = 0
        wb.sheets["CONFIG"].api.Visible = 0
    except Exception:
        pass

    pl_ws.activate()
    wb.save()
    wb.close()


def _f(ws, cell, formula):
    """数式を設定する (Formula2 経由)"""
    ws.range(cell).api.Formula2 = formula


def _build_pl_view(ws):
    # =============================================================
    # 非表示データエリア (行 28~) のレイアウト:
    #   A = period      (SORTBY recency_key DESC)
    #   B = quarter      (SORTBY recency_key DESC)
    #   C = sales        (SORTBY recency_key DESC)
    #   D = gross_profit (SORTBY recency_key DESC)
    #   E = operating_profit (SORTBY recency_key DESC)
    #
    # PL 表示エリア (行 4~23) :
    #   B=期  C=Q  D=売上  E=粗利  F=粗利率  G=販管費  H=営業利益
    # =============================================================

    # --- Row 1: タイトル ---
    ws.range("A1").value = "PL VIEWER"
    ws.range("A1").font.size = 14
    ws.range("A1").font.bold = True

    # --- Row 2: ticker 入力 (数値) ---
    ws.range("A2").value = "ticker >"
    ws.range("B2").value = 1736
    ws.range("B2").font.size = 14
    ws.range("B2").font.bold = True
    ws.range("B2").font.color = (0, 0, 200)
    ws.range("C2").value = "<- 4桁コード"

    # --- Row 3: ヘッダー ---
    for c, v in {"B3":"期","C3":"Q","D3":"売上","E3":"粗利","F3":"粗利率",
                 "G3":"販管費","H3":"営業利益",
                 "J3":"売上","K3":"粗利","L3":"粗利率",
                 "M3":"販管費","N3":"営業利益","P3":"期/Q"}.items():
        ws.range(c).value = v

    # セグメント名 (Q-Z: 5セグ x 売上/利益)
    # ticker は PQ 側で Int64 なので $B$2 を直接比較
    for i in range(0, 10, 2):
        c = chr(ord("Q") + i)
        c2 = chr(ord("R") + i)
        idx = i // 2 + 1
        _f(ws, f"{c}3", (
            f'=IFERROR(INDEX(_xlfn.UNIQUE(_xlfn._xlws.FILTER('
            f'Tbl_SEGMENT[segment_name],'
            f'(Tbl_SEGMENT[ticker]=$B$2)'
            f'*(Tbl_SEGMENT[metric_name]="segment_sales")'
            f')),{idx},1),"")'
        ))
        _f(ws, f"{c2}3", f"={c}$3")

    ws.range("A3:Z3").font.bold = True

    # --- 非表示データエリア (行27~) ---
    ws.range("A27").value = "--- DATA (hidden) ---"

    # SORTBY: ticker一致行を sort_key 昇順(上が古い→下が新しい)で並べ替え
    # A28=period, B28=quarter, C28=sales, D28=gross_profit, E28=operating_profit
    sortby_cols = {
        "A": "period",
        "B": "quarter",
        "C": "sales",
        "D": "gross_profit",
        "E": "operating_profit",
    }
    for col_letter, col_name in sortby_cols.items():
        _f(ws, f"{col_letter}28", (
            f'=IFERROR(_xlfn.SORTBY('
            f'_xlfn._xlws.FILTER(Tbl_DATA[{col_name}],Tbl_DATA[ticker]=$B$2),'
            f'_xlfn._xlws.FILTER(Tbl_DATA[sort_key],Tbl_DATA[ticker]=$B$2),1'
            f'),"")'
        ))

    # --- PL 表示行 (行4~23, 最大20行) ---
    # 非表示エリア: A=period, B=quarter, C=sales, D=gross_profit, E=operating_profit
    # 表示エリア:   B=期,     C=Q,      D=売上,  E=粗利,         F=粗利率, G=販管費, H=営業利益
    for r in range(4, 24):
        d = r + 24  # data row: 4->28, 5->29, ...
        p = r - 1   # previous row

        # B: 期 (= 非表示 A列 = period)
        _f(ws, (r, 2), f'=IFERROR(A{d},"")')
        # C: Q (= 非表示 B列 = quarter)
        _f(ws, (r, 3), f'=IFERROR(B{d},"")')
        # D: 売上 (= 非表示 C列 = sales, 百万円に変換)
        _f(ws, (r, 4), f'=IFERROR(C{d}/1000000,"")')
        # E: 粗利 (= 非表示 D列 = gross_profit, 百万円)
        _f(ws, (r, 5), f'=IFERROR(D{d}/1000000,"")')
        # F: 粗利率
        _f(ws, (r, 6), f'=IFERROR(E{r}/D{r},"")')
        # G: 販管費 = 粗利 - 営業利益
        _f(ws, (r, 7), f'=IFERROR(E{r}-H{r},"")')
        # H: 営業利益 (= 非表示 E列 = operating_profit, 百万円)
        _f(ws, (r, 8), f'=IFERROR(E{d}/1000000,"")')

        # --- 四半期分解 (J-N) ---
        _f(ws, (r, 10), f'=IFERROR(IF($C{r}="1Q",D{r},D{r}-D{p}),"")')  # J: 四半期売上
        _f(ws, (r, 11), f'=IFERROR(IF($C{r}="1Q",E{r},E{r}-E{p}),"")')  # K: 四半期粗利
        _f(ws, (r, 12), f'=IFERROR(K{r}/J{r},"")')                       # L: 四半期粗利率
        _f(ws, (r, 13), f'=IFERROR(IF($C{r}="1Q",G{r},G{r}-G{p}),"")')  # M: 四半期販管費
        _f(ws, (r, 14), f'=IFERROR(IF($C{r}="1Q",H{r},H{r}-H{p}),"")')  # N: 四半期営業利益

        # --- 期/Q ラベル (P) ---
        _f(ws, (r, 16), f'=IFERROR(_xlfn.CONCAT($B{r}," ",$C{r}),"")')

        # --- セグメント (Q-Z) ---
        for si in range(0, 10, 2):
            cs, cp = si + 17, si + 18
            sc = chr(ord("Q") + si)
            _f(ws, (r, cs), (
                f'=IFERROR(SUMIFS(Tbl_SEGMENT[value_mil],'
                f'Tbl_SEGMENT[ticker],$B$2,'
                f'Tbl_SEGMENT[period],$B{r},Tbl_SEGMENT[quarter],$C{r},'
                f'Tbl_SEGMENT[segment_name],{sc}$3,'
                f'Tbl_SEGMENT[metric_name],"segment_sales"),"")'))
            _f(ws, (r, cp), (
                f'=IFERROR(SUMIFS(Tbl_SEGMENT[value_mil],'
                f'Tbl_SEGMENT[ticker],$B$2,'
                f'Tbl_SEGMENT[period],$B{r},Tbl_SEGMENT[quarter],$C{r},'
                f'Tbl_SEGMENT[segment_name],{sc}$3,'
                f'Tbl_SEGMENT[metric_name],"segment_profit"),"")'))

    # --- 数値書式 ---
    ws.range("D4:E23").number_format = "#,##0"
    ws.range("F4:F23").number_format = "0.0%"
    ws.range("G4:H23").number_format = "#,##0"
    ws.range("J4:K23").number_format = "#,##0"
    ws.range("L4:L23").number_format = "0.0%"
    ws.range("M4:N23").number_format = "#,##0"
    ws.range("Q4:Z23").number_format = "#,##0"

    # --- 列幅 ---
    for c, w in {"A":8,"B":14,"C":5,"D":12,"E":12,"F":8,"G":12,"H":12,
                 "I":2,"J":12,"K":12,"L":8,"M":12,"N":12,"O":2,"P":18}.items():
        ws.range(f"{c}:{c}").column_width = w

    # --- 行27以降を非表示 (スピル範囲は例外) ---
    for r in range(27, 50):
        try:
            ws.range(f"{r}:{r}").api.Hidden = True
        except Exception:
            pass


if __name__ == "__main__":
    main()
