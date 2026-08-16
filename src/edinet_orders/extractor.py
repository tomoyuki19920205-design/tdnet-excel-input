# src/edinet_orders/extractor.py
"""
EDINET有価証券報告書から受注・受注残高・RPOを抽出するモジュール。
"""
from __future__ import annotations

import os
import re
import unicodedata
import zipfile
from typing import Any

from bs4 import BeautifulSoup

from .semantic_table import (
    BEGIN_CARRYOVER_KEYWORDS,
    COMPLETED_CONSTRUCTION_KEYWORDS,
    END_CARRYOVER_KEYWORDS,
    EXPLICIT_BACKLOG_KEYWORDS,
    ORDER_KEYWORDS,
    extract_semantic_tables,
)

# ── デフォルトキャッシュディレクトリ ──
_DEFAULT_CACHE_DIR = os.environ.get(
    "EDINET_CACHE_DIR",
    r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\edinet_cache",
)


def _norm(text: str) -> str:
    """NFKC正規化 + 空白/改行/全角スペース除去"""
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("\n", "").replace("\u3000", "")


def _parse_number(s: str | None) -> int | None:
    """文字列から整数を抽出。△▲をマイナスとして扱う"""
    if not s:
        return None
    s = _norm(s)
    s = s.replace(",", "").replace("△", "-").replace("▲", "-")
    match = re.search(r"(-?\d+)", s)
    if match:
        return int(match.group(1))
    return None


def _detect_unit(texts: list[str]) -> str | None:
    """テキストリストから単位を検出。百万円 > 千円 > 億円 > 円"""
    combined_n = _norm(" ".join(texts))
    if "百万円" in combined_n:
        return "百万円"
    if "千円" in combined_n:
        return "千円"
    if "億円" in combined_n:
        return "億円"
    # 単なる「円」は誤検知を避けるため厳格に判定
    if re.search(r"\(円\)|単位[：:]円|金額[：:]円", combined_n):
        return "円"
    return None


# ── キーワード定義 ──
# 受注高候補（優先順位順）
_ORDER_KW = list(ORDER_KEYWORDS) + ["受注実績", "受注状況", "受注(契約)高"]
# Explicit backlog only.  Construction carryover is a different metric.
_BACKLOG_KW = list(EXPLICIT_BACKLOG_KEYWORDS)
_BEGIN_CARRYOVER_KW = list(BEGIN_CARRYOVER_KEYWORDS)
_END_CARRYOVER_KW = list(END_CARRYOVER_KEYWORDS)
# 除外すべき類似語（受注高として絶対使わない）
_COMPLETED_KW = list(COMPLETED_CONSTRUCTION_KEYWORDS) + ["売上高", "生産高", "仕入高"]
_RPO_KW = ["残存履行義務", "未充足の履行義務"]
_SECTION_KW = _ORDER_KW + _BACKLOG_KW + _BEGIN_CARRYOVER_KW + _END_CARRYOVER_KW + _COMPLETED_KW

# 当期を示すキーワード
_CURRENT_PERIOD_KW = ["当事業年度", "当連結会計年度", "当期", "今期"]
# 前期を示すキーワード
_PREV_PERIOD_KW = ["前事業年度", "前連結会計年度", "前期", "前年", "前年同期"]

# 合計行を示すキーワード（行ラベルの0列目）
_TOTAL_ROW_KW = ["合計", "計", "総計", "報告セグメント計"]


def _parse_html_table(table_soup) -> list[list[str]]:
    """
    tableをcolspan/rowspan展開してグリッドにする。
    返値は grid[行][列] = テキストの2次元リスト。
    """
    rows = table_soup.find_all("tr")
    if not rows:
        return []

    grid: list[list[str | None]] = []
    for r_idx, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        while len(grid) <= r_idx:
            grid.append([])

        c_idx = 0
        for cell in cells:
            while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                c_idx += 1

            text = _norm(cell.get_text())
            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))

            for i in range(rowspan):
                for j in range(colspan):
                    rt = r_idx + i
                    ct = c_idx + j
                    while len(grid) <= rt:
                        grid.append([])
                    while len(grid[rt]) <= ct:
                        grid[rt].append(None)
                    grid[rt][ct] = text

            c_idx += colspan

    # None を空文字に変換
    for r in grid:
        for c in range(len(r)):
            if r[c] is None:
                r[c] = ""

    return grid  # type: ignore[return-value]


def _get_table_context(table_soup) -> str:
    """テーブル直前の兄弟要素のテキストとcaptionを取得する。
    兄弟要素に単位が見つからない場合は、祖先要素のテキストも探す。"""
    title_text = ""
    cap = table_soup.find("caption")
    if cap:
        title_text = _norm(cap.get_text()) + " "

    prev = table_soup.find_previous_sibling()
    count = 0
    while prev and count < 5:
        t = _norm(prev.get_text())
        if t:
            title_text = t + " " + title_text
            count += 1
        prev = prev.find_previous_sibling()

    # もし兄弟要素からテキストが取れなかった場合、親要素（divラッパー等）の兄弟を探す
    if count == 0 and table_soup.parent:
        prev = table_soup.parent.find_previous_sibling()
        while prev and count < 5:
            t = _norm(prev.get_text())
            if t:
                title_text = t + " " + title_text
                count += 1
            prev = prev.find_previous_sibling()

    # 兄弟要素に単位キーワードがなければ祖先要素も探す（中電工のように列ヘッダに単位がない場合）
    _UNIT_KW = ["百万円", "千円", "億円", "（円）", "単位：円", "単位：百万円", "単位：千円", "単位：億円"]
    if not any(kw in title_text for kw in _UNIT_KW):
        parent = table_soup.parent
        depth = 0
        while parent and depth < 5:
            sib = parent.find_previous_sibling()
            sib_count = 0
            while sib and sib_count < 3:
                t = _norm(sib.get_text())
                if t:
                    for kw in _UNIT_KW:
                        if kw in t:
                            # 長文の祖先テキストは追加せず、単位KWのみ追加（受注KW混入を防ぐ）
                            title_text = kw + " " + title_text
                            break
                sib = sib.find_previous_sibling()
                sib_count += 1
            if any(kw in title_text for kw in _UNIT_KW):
                break
            parent = parent.parent
            depth += 1

    return title_text



def _is_total_row_label(label: str) -> bool:
    """テキストが「合計」「計」「報告セグメント計」などを示すか"""
    import re
    cleaned = re.sub(r"[(（].*?[)）]$", "", label).strip()
    return any(kw == cleaned or cleaned.endswith(kw) for kw in _TOTAL_ROW_KW)


def _row_total_label(row: list[str]) -> str:
    """
    行の先頭数列から「合計」「計」を示す最初のラベルを返す。
    建設系テーブルは [期別, 工種, ...] の2列構造なので、
    0列目が空でなければ期別ラベル、1列目以降を工種ラベルとして確認する。
    """
    for cell in row[:3]:  # 先頭3列まで確認
        if _is_total_row_label(cell):
            return cell
    return ""


def _is_prev_period(label: str) -> bool:
    """ラベルが前期・前事業年度などを示すか"""
    return any(kw in label for kw in _PREV_PERIOD_KW)


def _is_current_period(label: str) -> bool:
    """ラベルが当期・当事業年度などを示すか"""
    return any(kw in label for kw in _CURRENT_PERIOD_KW)


def _assign_period_labels(grid: list[list[str]], n_header: int) -> list[str]:
    """
    データ行（n_header行目以降）に期別ラベルを割り当てる。
    0列目の非空かつ「期別」らしいテキストを引き継ぐ。
    工種ラベル（計・合計・建築工事 等）は引き継がない。
    「第X期(自...至...)」形式も期別ラベルとして扱う。
    返値: 各行の期別ラベル（データ行のみ、長さ = len(grid) - n_header）
    """
    period_labels = []
    current_label = ""
    for row in grid[n_header:]:
        label_col0 = row[0] if row else ""
        is_period = (
            _is_current_period(label_col0)
            or _is_prev_period(label_col0)
            # 「第X期(...」形式：数字+期 が含まれる
            or bool(re.search(r"第\d+期", label_col0))
        )
        # 工種・分類ラベルは引き継がない（「計」「合計」「建築工事」「製鉄プラント」等）
        # 注意: 「事業」「工事」はラベル単体ではなく長い文字列に含まれる場合がある
        # 「前事業年度(自...至...)」のように期別KWが入っているものは除外しない
        is_total_or_type = (
            _is_total_row_label(label_col0)
            and not _is_prev_period(label_col0)
            and not _is_current_period(label_col0)
            and not re.search(r"第\d+期", label_col0)
        )
        if label_col0 and is_period and not is_total_or_type:
            current_label = label_col0
        period_labels.append(current_label)
    return period_labels


def _period_label_order(label: str, all_labels: list[str]) -> int:
    """
    期別ラベルが表の中で何番目に出現するかを返す（0始まり）。
    後ろほど新しい期（当期）とみなす。
    """
    unique = []
    for l in all_labels:
        if l and l not in unique:
            unique.append(l)
    try:
        return unique.index(label)
    except ValueError:
        return -1


def _latest_period_label(all_labels: list[str]) -> str:
    """表に現れた期別ラベルの中で最後（最新）のものを返す。"""
    seen: list[str] = []
    for l in all_labels:
        if l and l not in seen:
            seen.append(l)
    return seen[-1] if seen else ""


def extract_from_company(
    target: dict[str, Any],
    cache_dir: str = _DEFAULT_CACHE_DIR,
) -> dict[str, Any]:
    """
    1社分のEDINET有価証券報告書から受注データを抽出する。

    Parameters
    ----------
    target : dict
        {"ticker": str, "company": str, "doc_id": str}
    cache_dir : str
        EDINET XBRLキャッシュルートディレクトリ

    Returns
    -------
    dict
        抽出結果（ticker / company / doc_id / unit / orders_received /
        order_backlog / construction_carryover / rpo / source_type /
        source_tag / snippet / confidence / notes）
    """
    ticker = target["ticker"]
    company = target["company"]
    doc_id = target["doc_id"]

    result: dict[str, Any] = {
        "ticker": ticker,
        "company": company,
        "doc_id": doc_id,
        "source_type": None,
        "source_tag": None,
        "unit": None,
        "orders_received": None,
        "order_backlog": None,
        "construction_carryover": None,
        "completed_construction": None,
        "rpo": None,
        "snippet": "",
        "confidence": "low",
        "notes": "",
    }

    zip_path = os.path.join(cache_dir, doc_id, "xbrl.zip")
    if not os.path.exists(zip_path):
        result["notes"] = "ZIP not found"
        return result

    # Preferred path: retain DOM header paths, evaluate every table/row, and
    # keep explicit backlog separate from construction carryover.  The legacy
    # path below remains only as a compatibility fallback for documents whose
    # order disclosure is not represented by a semantic table.
    semantic = extract_semantic_tables(zip_path, target)
    if semantic is not None:
        result.update(semantic)
        return result

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        htm_files = [n for n in names if "PublicDoc" in n and n.endswith(".htm")]

        found_order = False

        for fname in htm_files:
            raw = z.read(fname)
            try:
                text = raw.decode("utf-8")
            except Exception:
                text = raw.decode("cp932", errors="replace")

            soup = BeautifulSoup(text, "html.parser")
            tables = soup.find_all("table")

            for table in tables:
                grid = _parse_html_table(table)
                if not grid:
                    continue

                context_text = _get_table_context(table)
                table_text = " ".join([" ".join(r) for r in grid])

                # 単位の検出:
                # 1. context_text（表タイトル・周辺テキスト）
                # 2. フラットヘッダ（後で flat_header を作ってから再確認）
                # ※ table_text 全体は誤検出の原因になるため、単位検出には使わない
                unit = _detect_unit([context_text])

                # ── ヘッダ行の特定 ──
                # 「財務数値」を含む行をデータ行とみなす。
                # 「財務数値」の定義：コンマ区切り数値（例: 1,234）または3桁以上の数字列だけ。
                # 除外: 年号のみ（「自2024年」「至2025年」）や期別ラベル文字列。
                def _is_financial_number(s: str) -> bool:
                    """True if s contains a financial value (comma-formatted or 3+ digit number alone)."""
                    s_n = _norm(s).replace(",", "").replace("，", "")
                    # コンマ区切り数値（少なくとも6桁）
                    if re.search(r"\d{1,3}(,\d{3})+", _norm(s)):
                        return True
                    # 単一の数字列づ3桁以上（年号がテキストに混入していない場合のみ）
                    m = re.fullmatch(r"[\-∆▲]?\d{3,}", s_n.strip())
                    return m is not None

                header_rows: list[list[str]] = []
                for row in grid:
                    has_number = any(_is_financial_number(cell) for cell in row[1:])
                    if not has_number:
                        header_rows.append(row)
                    else:
                        break
                if not header_rows:
                    header_rows = [grid[0]]
                n_header = len(header_rows)

                # ヘッダ行中に数字が混入していないか再チェック（トーカロ等iXBRL表対応）
                # 最大でヘッダ = 全行の半分までとし、安全側に払う
                n_header = min(n_header, max(1, len(grid) // 2))

                # ── 複合ヘッダをフラット化 ──
                cols = max(len(r) for r in grid)
                flat_header: list[str] = []
                for c in range(cols):
                    col_texts = []
                    for hr in header_rows:
                        if c < len(hr) and hr[c] not in col_texts:
                            col_texts.append(hr[c])
                    flat_header.append("".join(col_texts))

                # flat_header から単位を再確認（最優先: 列名に単位が直接含まれる場合）
                # 「受注高(千円)」「受注高(百万円)」のように列名に単位が入っていれば最優先で採用
                header_unit = _detect_unit(flat_header)
                if header_unit:
                    unit = header_unit  # 列名の単位で上書き（context誤検出を防ぐ）

                # ── 受注関連テーブルか判定 ──
                has_order_kw = any(any(kw in h for kw in _SECTION_KW) for h in flat_header)
                is_order_title = any(kw in context_text for kw in ["受注実績", "受注状況", "受注高"])
                is_backlog_only_title = (
                    "受注残高" in context_text
                    and "受注高" not in context_text
                    and "受注実績" not in context_text
                )

                if not has_order_kw and not is_order_title and not is_backlog_only_title:
                    continue

                # ── 列インデックスを特定 ──
                def find_order_col(kws: list[str]) -> int | None:
                    """受注高候補列を探す。列名が「計」「合計」で終わる列は除外。列0（行ラベル）は除外。"""
                    _LABEL_KW = ["セグメント", "区分", "品目", "工事種別", "得意先", "種類別", "期別"]
                    matches = []
                    for i, h in enumerate(flat_header):
                        if i == 0:
                            continue  # 列0（行ラベル列）は常に除外
                        if any(lkw in h for lkw in _LABEL_KW):
                            continue  # ラベル系列は除外
                        if any(kw in h for kw in kws):
                            h_stripped = re.sub(r"[（(][^）)]*[）)]$", "", h)
                            if h_stripped.endswith(("計", "合計")):
                                continue  # 「計」で終わる列は除外
                            if any(ex in h for ex in _COMPLETED_KW):
                                continue
                            # 「比率」「増減率」「前期比」「構成比」「%」を含む列は数値ではなく比率列
                            if any(rate_kw in h for rate_kw in [
                                "比率", "増減率", "前期比", "前年比", "前年同期比",
                                "構成比", "前年度比", "前年度末比",
                                "(%)", "（%）", "（％）", "(%）"
                            ]):
                                continue
                            matches.append(i)
                    if matches:
                        return matches[-1]  # 後ろ（当期側）を優先
                    # フォールバック：「受注実績」等のコンテキストがある場合は「当期」列を採用する。
                    # ただし受注高を探しているときは is_order_title、受注残高を探しているときは is_backlog_only_title の場合に限る。
                    # (これを行わないと、1つの表の「当期」列が受注高・受注残高の両方にフォールバック採用されてしまう)
                    is_target_order = (kws == _ORDER_KW and is_order_title)
                    is_target_backlog = (kws == _BACKLOG_KW and is_backlog_only_title)
                    
                    if is_target_order or is_target_backlog:
                        for i, h in enumerate(flat_header):
                            if i == 0:
                                continue
                            if any(kw in h for kw in _CURRENT_PERIOD_KW):
                                h_stripped = re.sub(r"[（(][^）)]*[）)]$", "", h)
                                if not h_stripped.endswith(("計", "合計")):
                                    # フォールバックでも比率列は除外
                                    if not any(rate_kw in h for rate_kw in [
                                        "比率", "増減率", "前期比", "前年比", "前年同期比",
                                        "構成比", "前年度比", "前年度末比",
                                        "(%)", "（%）", "（％）", "(%）"
                                    ]):
                                        matches.append(i)
                        if matches:
                            return matches[-1]
                    return None


                idx_or = None if is_backlog_only_title else find_order_col(_ORDER_KW)
                idx_ob = find_order_col(_BACKLOG_KW)

                # idx_or/idx_obが両方Noneでも、テーブルに受注キーワードがある場合
                # →ヘッダ行の全セルを再スキャンして受注高列を探す（セル内pタグ形式対応）
                if idx_or is None and idx_ob is None:
                    # ヘッダ行（数値行より前）のみを対象にする（データ行での誤検出を防ぐ）
                    for row in grid[:n_header + 3]:  # ヘッダ行＋最初3行まで
                        for c_idx, cell_text in enumerate(row):
                            if any(kw in cell_text for kw in _ORDER_KW) and not any(ex in cell_text for ex in _COMPLETED_KW):
                                if "計" not in cell_text and "合計" not in cell_text:
                                    if idx_or is None:
                                        idx_or = c_idx
                            if any(kw in cell_text for kw in _BACKLOG_KW):
                                if "計" not in cell_text and "合計" not in cell_text and idx_ob is None:
                                    idx_ob = c_idx

                if idx_or is None and idx_ob is None and not is_order_title and not is_backlog_only_title:
                    continue

                # ── 期別ラベルを各データ行に割り当てる ──
                period_labels = _assign_period_labels(grid, n_header)
                data_rows = grid[n_header:]

                # 「通常データ行」の先頭ラベル列数を計算（建設系合計行の列ずれ補正用）
                # 通常行: [期別, 種類別, 数値, ...] → ラベル列数=2
                # 合計行: [合計, 数値, ...] → ラベル列数=1（1列ずれ）
                def _count_leading_labels(r: list[str]) -> int:
                    """
                    先頭から最初の財務数値セル（コンマ区切り数値）までのセルインデックス（0-indexed）。
                    空セルも1列分としてカウントする。
                    例: ['合計', '1,234', ...] → 1
                        ['', '合計', '1,234', ...] → 2
                        ['期別', '種類別', '1,234', ...] → 2
                    """
                    for i, c in enumerate(r):
                        if _is_financial_number(c):
                            return i
                    return len(r)

                _non_total_rows = [
                    r for r in data_rows
                    if _row_total_label(r) == "" and any(_is_financial_number(c) for c in r[1:])
                ]
                if _non_total_rows:
                    _expected_label_count = min(
                        _count_leading_labels(r) for r in _non_total_rows[:5]
                    )
                else:
                    _expected_label_count = 1

                # ── 当期の合計行を優先して探す ──
                # 探索方针：
                #   Pass 1: 当期ラベル + 合計行キーワード
                #   Pass 2: 当期ラベルがついているデータ行が1行のみ → その行
                #   Pass 3: 前期ラベルなし + 合計行キーワード（セグメント表など）
                #   Pass 4: 合計行キーワードがある行（期別区別なし）
                #   Pass 5: データ行が1行のみ（期別区別なし）

                def try_extract_from_row(row: list[str]) -> tuple[int | None, int | None]:
                    """
                    idx_or/idx_obから値を取る。
                    建設系「合計」行で期別+種類別が合体され列ずれが発生する場合に自動補正する。
                    """
                    actual_label = _count_leading_labels(row)
                    offset = max(0, _expected_label_count - actual_label)

                    or_v = None
                    ob_v = None
                    if idx_or is not None:
                        adj = max(0, idx_or - offset)
                        if adj < len(row):
                            or_v = _parse_number(row[adj])
                    if idx_ob is not None:
                        adj = max(0, idx_ob - offset)
                        if adj < len(row):
                            ob_v = _parse_number(row[adj])
                    return or_v, ob_v


                target_row: list[str] | None = None
                target_confidence = "low"

                # Pass 1: 当期ラベル（引き継ぎ含む） + 合計行
                # 建設系: 行[期別=当期, 工種=計] → period_label=当期, _row_total_label で '計' を検出
                for row, plabel in zip(data_rows, period_labels):
                    if not _is_current_period(plabel):
                        continue
                    if _row_total_label(row):  # 先頭3列で「計」「合計」を確認
                        or_v, ob_v = try_extract_from_row(row)
                        if or_v is not None or ob_v is not None:
                            target_row = row
                            target_confidence = "high"
                            break

                # Pass 1b: 「第X期」形式の場合、表内で最後に出現した期別ラベルを当期とみなす
                # Pass 1b-1: まず「合計」行（計より上位）を探す
                # Pass 1b-2: 次に「計」行を探す
                if target_row is None and period_labels:
                    latest_label = _latest_period_label(period_labels)
                    if latest_label and not _is_current_period(latest_label) and not _is_prev_period(latest_label):
                        # 第X期形式：最後の期別ラベルを当期とみなして合計行を探す（合計優先）
                        for priority_kw in ["合計", "総計", "報告セグメント計", "計"]:
                            if target_row is not None:
                                break
                            for row, plabel in zip(data_rows, period_labels):
                                if plabel != latest_label:
                                    continue
                                # 先頭3列のいずれかが priority_kw に一致するか
                                row_cells = row[:3]
                                if any(c == priority_kw or c.endswith(priority_kw) for c in row_cells):
                                    or_v, ob_v = try_extract_from_row(row)
                                    if or_v is not None or ob_v is not None:
                                        target_row = row
                                        target_confidence = "high"
                                        break
                    # 「当期ラベル」でも「合計」行を同じ優先順で
                    if target_row is None:
                        for priority_kw in ["合計", "総計", "報告セグメント計", "計"]:
                            if target_row is not None:
                                break
                            for row, plabel in zip(data_rows, period_labels):
                                if not _is_current_period(plabel):
                                    continue
                                row_cells = row[:3]
                                if any(c == priority_kw or c.endswith(priority_kw) for c in row_cells):
                                    or_v, ob_v = try_extract_from_row(row)
                                    if or_v is not None or ob_v is not None:
                                        target_row = row
                                        target_confidence = "high"
                                        break

                # Pass 2: 当期ラベルのデータ行が1行だけ（前期行は除外）
                if target_row is None:
                    current_data_rows = [
                        row
                        for row, plabel in zip(data_rows, period_labels)
                        if _is_current_period(plabel) and not _is_prev_period(plabel)
                    ]
                    if len(current_data_rows) == 1:
                        row = current_data_rows[0]
                        or_v, ob_v = try_extract_from_row(row)
                        if or_v is not None or ob_v is not None:
                            target_row = row
                            target_confidence = "medium"

                # Pass 3: 期別ラベルなし + 合計行（セグメント表: 前期ラベルが付いていない行）
                # 「合計」>「報告セグメント計」>「計」の優先順で探す
                if target_row is None:
                    for priority_kw in ["合計", "総計", "報告セグメント計", "計"]:
                        if target_row is not None:
                            break
                        for row, plabel in zip(data_rows, period_labels):
                            if _is_prev_period(plabel) and not _is_current_period(plabel):
                                continue
                            row_cells = row[:3]
                            if any(c == priority_kw or c.endswith(priority_kw) for c in row_cells):
                                or_v, ob_v = try_extract_from_row(row)
                                if or_v is not None or ob_v is not None:
                                    target_row = row
                                    target_confidence = "high"
                                    break

                # Pass 4: 最後の合計行（前期ラベルなし or 当期ラベルの合計行のみ）
                # 有報テーブルは「前期→当期」の順が多いため、最後の合計行が当期である確率が高い
                # ただし前期ラベルが明示されている行は除外
                if target_row is None:
                    last_total_row = None
                    for row, plabel in zip(data_rows, period_labels):
                        # 前期ラベルが継承されている行はスキップ
                        if _is_prev_period(plabel) and not _is_current_period(plabel):
                            continue
                        if _row_total_label(row):
                            or_v, ob_v = try_extract_from_row(row)
                            if or_v is not None or ob_v is not None:
                                last_total_row = row  # 上書きしながら最後の合計行を記録
                    if last_total_row is not None:
                        target_row = last_total_row
                        target_confidence = "high"

                # Pass 5: データ行が1行だけ（前期ラベルが継承されていない行）
                if target_row is None:
                    non_prev_rows = [
                        row
                        for row, plabel in zip(data_rows, period_labels)
                        if not _is_prev_period(plabel) or _is_current_period(plabel)
                    ]
                    if len(non_prev_rows) == 1:
                        row = non_prev_rows[0]
                        or_v, ob_v = try_extract_from_row(row)
                        if or_v is not None or ob_v is not None:
                            target_row = row
                            target_confidence = "medium"


                # Pass 6: セグメント合算フォールバック (合計行なし・複数行)
                if target_row is None and len(non_prev_rows) >= 2:
                    valid_to_sum = True
                    sum_or = 0 if idx_or is not None else None
                    sum_ob = 0 if idx_ob is not None else None
                    
                    for row in non_prev_rows:
                        or_v, ob_v = try_extract_from_row(row)
                        if sum_or is not None:
                            if or_v is not None:
                                sum_or += or_v
                            else:
                                valid_to_sum = False
                        if sum_ob is not None:
                            if ob_v is not None:
                                sum_ob += ob_v
                            else:
                                valid_to_sum = False
                    
                    if valid_to_sum and (sum_or is not None or sum_ob is not None):
                        target_row_pseudo = ["SEGMENT_SUM_FALLBACK"] * max((idx_or or 0) + 1, (idx_ob or 0) + 1, 10)
                        if idx_or is not None: target_row_pseudo[idx_or] = str(sum_or)
                        if idx_ob is not None: target_row_pseudo[idx_ob] = str(sum_ob)
                        target_row = target_row_pseudo
                        target_confidence = "low"
                        result["notes"] += f"SEGMENT_SUM_FALLBACK applied (OR={sum_or}, OB={sum_ob}). "
                # ── 抽出 ──
                if target_row is not None:
                    or_v, ob_v = try_extract_from_row(target_row)
                    
                    target_unit = _detect_unit(target_row)
                    if target_unit:
                        if header_unit:
                            if header_unit != target_unit:
                                result["notes"] += f"UNIT_CONFLICT_REVIEW (header={header_unit}, target_row={target_unit}). "
                                or_v, ob_v = None, None
                        else:
                            if unit != target_unit:
                                result["notes"] += f"UNIT_OVERRIDE_FROM_TARGET_ROW (old={unit}, new={target_unit}). "
                            unit = target_unit
                    elif not unit:
                        pass

                    # 完全な結果（OR+OB両方）が不完全な先行結果（ORのみ/OBのみ）を上書きする。
                    # 例: 先行テーブルが比較表からORだけセットし、後続の正しいテーブルが両方提供する場合。
                    new_is_complete = (or_v is not None and ob_v is not None)
                    existing_incomplete = (
                        (result["orders_received"] is not None and result["order_backlog"] is None)
                        or (result["orders_received"] is None and result["order_backlog"] is not None)
                    )
                    if new_is_complete and existing_incomplete:
                        # 完全な表が不完全な先行結果を置き換える（単位も一緒に更新）
                        result["orders_received"] = or_v
                        result["order_backlog"] = ob_v
                        if unit:  # 正しい単位で上書き
                            result["unit"] = unit
                    else:
                        if result["orders_received"] is None and or_v is not None:
                            result["orders_received"] = or_v
                        if result["order_backlog"] is None and ob_v is not None:
                            result["order_backlog"] = ob_v

                    # ヘッダに単位が直接記載されている場合は常に優先（コンテキスト誤検出を上書き）
                    result["unit"] = unit or result["unit"]
                    result["confidence"] = target_confidence
                    result["source_type"] = "table"
                    result["snippet"] = (
                        f"Header: {' | '.join(flat_header[:10])}\n"
                        f"Row: {' | '.join(target_row[:10])}\n"
                        f"Title: {context_text[:100]}"
                    )
                    found_order = True

                if result["confidence"] == "high":
                    # 受注高・受注残の両方が取れた場合のみ即座にbreak
                    # 受注残がまだNoneなら次の表(受注残高テーブル)も確認する
                    if result["orders_received"] is not None and result["order_backlog"] is not None:
                        break
                    # 受注残だけがまだない場合は継続（found_orderは立てたまま）

            if found_order and result["order_backlog"] is not None:
                break
            if found_order and result["orders_received"] is not None:
                # 受注高は取れているが受注残がない: 受注残高テーブルを探すために続行
                # ただしファイルは一巡したらbreak（次のhtmファイルには進まない）
                pass

            # ── 2. iXBRL タグから補足 ──
            if not found_order:
                ix_tags = soup.find_all(
                    attrs={"name": re.compile(r"(ReceivedOrders|OrdersReceived|OrderBacklog)", re.I)}
                )
                for tag in ix_tags:
                    tag_name = tag.get("name", "")
                    val = _parse_number(tag.get_text())
                    if val is None:
                        continue
                    tag_l = tag_name.lower()
                    if "backlog" in tag_l and result["order_backlog"] is None:
                        result["order_backlog"] = val
                        result["source_type"] = result["source_type"] or "ixbrl"
                        result["source_tag"] = tag_name
                    elif result["orders_received"] is None:
                        result["orders_received"] = val
                        result["source_type"] = result["source_type"] or "ixbrl"
                        result["source_tag"] = tag_name

            # ── 3. テキストから受注高 fallback ──
            if not found_order:
                text_content = soup.get_text()
                for kw in _ORDER_KW:
                    pattern = kw + r"は([\d,]+)(百万円|千円|億円|円)"
                    m = re.search(pattern, text_content)
                    if m:
                        val = _parse_number(m.group(1))
                        unit_str = m.group(2)
                        if val:
                            result["orders_received"] = val
                            result["unit"] = unit_str
                            result["source_type"] = "text"
                            idx = m.start()
                            result["snippet"] = text_content[max(0, idx - 20): idx + 100].strip()[:500]
                            result["confidence"] = "medium"
                            result["notes"] += "Orders extracted from text. "
                            found_order = True
                            break

            if found_order:
                break

    # ── confidence 確定 ──
    has_value = any(
        result[k] is not None
        for k in ["orders_received", "order_backlog", "construction_carryover", "rpo"]
    )
    if not has_value:
        result["confidence"] = "low"
        result["notes"] += "No valid values extracted."
    elif result["confidence"] == "low" and has_value:
        result["confidence"] = "medium"

    return result


def extract(
    survey_data: list[dict[str, Any]],
    cache_dir: str = _DEFAULT_CACHE_DIR,
) -> list[dict[str, Any]]:
    """
    survey_data (survey_detail.json の内容) から全社抽出する。

    Parameters
    ----------
    survey_data : list of dict
        survey_detail.json をロードしたリスト。
        doc_id が存在する企業のみ対象とする。

    Returns
    -------
    list of dict
        extract_from_company() の結果リスト。
    """
    targets = [
        {"ticker": d["ticker"], "company": d["company"], "doc_id": d["doc_id"]}
        for d in survey_data
        if d.get("doc_id")
    ]

    results = []
    for i, t in enumerate(targets, 1):
        res = extract_from_company(t, cache_dir=cache_dir)
        results.append(res)

    return results
