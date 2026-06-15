# src/edinet_orders/extractor.py
"""
EDINET有価証券報告書から受注・受注残高・RPOを抽出するモジュール。
scratch/extract_edinet_orders.py のロジックを正式モジュール化。
"""
from __future__ import annotations

import os
import re
import unicodedata
import zipfile
from typing import Any

from bs4 import BeautifulSoup

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


def _detect_unit(header_texts: list[str]) -> str | None:
    """ヘッダーテキストから単位を検出"""
    combined = " ".join(header_texts)
    combined_n = _norm(combined)
    if "百万円" in combined_n:
        return "百万円"
    if "千円" in combined_n:
        return "千円"
    if "億円" in combined_n:
        return "億円"
    if "円" in combined_n:
        return "円"
    return None


# ── 対象キーワード ──
_ORDER_KW = ["受注高", "受注額", "受注金額"]
_BACKLOG_KW = ["受注残高", "受注残"]
_CARRYOVER_KW = ["繰越工事高", "次期繰越高", "期末繰越高"]
_COMPLETED_KW = ["完成工事高", "売上高"]
_RPO_KW = ["残存履行義務", "未充足の履行義務"]
_SECTION_KW = _ORDER_KW + _BACKLOG_KW + _CARRYOVER_KW + _COMPLETED_KW


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
        order_backlog / construction_carryover / completed_construction /
        rpo / source_type / source_tag / snippet / confidence / notes）
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

            # ── 1. HTMLテーブルから抽出 ──
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                if not rows:
                    continue

                header_texts = [_norm(c.get_text()) for c in rows[0].find_all(["th", "td"])]
                unit = _detect_unit(header_texts)

                # 受注関連カラムを検出
                has_order_kw = any(any(kw in h for kw in _SECTION_KW) for h in header_texts)
                if not has_order_kw:
                    continue

                # カラムインデックスを特定
                def find_col(kws):
                    for i, h in enumerate(header_texts):
                        if any(kw in h for kw in kws):
                            return i
                    return None

                idx_or = find_col(_ORDER_KW)
                idx_ob = find_col(_BACKLOG_KW)
                idx_cc = find_col(_CARRYOVER_KW)
                idx_comp = find_col(_COMPLETED_KW)

                if idx_or is None and idx_ob is None and idx_cc is None:
                    continue

                # データ行から合計行を探す
                for row in rows[1:]:
                    cells = row.find_all(["th", "td"])
                    cell_texts = [_norm(c.get_text()) for c in cells]

                    # 合計行を優先
                    row_label = cell_texts[0] if cell_texts else ""
                    is_total = "合計" in row_label or "計" == row_label

                    def get_val(idx):
                        if idx is None or idx >= len(cell_texts):
                            return None
                        return _parse_number(cell_texts[idx])

                    or_v = get_val(idx_or)
                    ob_v = get_val(idx_ob)
                    cc_v = get_val(idx_cc)
                    comp_v = get_val(idx_comp)

                    if any(v is not None for v in [or_v, ob_v, cc_v]):
                        # 合計行優先
                        if is_total or result["orders_received"] is None:
                            if or_v is not None:
                                result["orders_received"] = or_v
                            if ob_v is not None:
                                result["order_backlog"] = ob_v
                            if cc_v is not None:
                                result["construction_carryover"] = cc_v
                            if comp_v is not None:
                                result["completed_construction"] = comp_v
                            result["unit"] = unit
                            result["source_type"] = "table"

                            # snippet
                            header_str = " | ".join(header_texts[:8])
                            row_str = " | ".join(cell_texts[:8])
                            result["snippet"] = f"Header: {header_str}\nRow: {row_str}"

                            # source_tag: XBRL タグを探す
                            parent = table.find_parent(attrs={"name": True}) or \
                                     table.find_parent(attrs={"id": True})
                            if parent:
                                result["source_tag"] = parent.get("name") or parent.get("id")

                            if is_total:
                                result["confidence"] = "high"
                                found_order = True
                                break

                if result["confidence"] == "high":
                    break

            if found_order:
                break

            # ── 2. iXBRL タグから補足 ──
            if not found_order:
                ix_tags = soup.find_all(attrs={"name": re.compile(r"(ReceivedOrders|OrdersReceived|OrderBacklog)", re.I)})
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

            # ── 3. テキストからRPO抽出 ──
            if not found_order:
                text_content = soup.get_text()
                for kw in _RPO_KW:
                    idx = text_content.find(kw)
                    if idx == -1:
                        continue
                    snippet = text_content[max(0, idx - 30): idx + 150]
                    numbers = re.findall(r"[\d,]+", snippet.replace("△", "").replace("▲", ""))
                    for num_str in numbers:
                        val = _parse_number(num_str)
                        if val and val > 100:
                            if result["rpo"] is None:
                                result["rpo"] = val
                                result["unit"] = result["unit"] or "百万円"
                                result["source_type"] = "text"
                                # source_tag: 直近の XBRL 属性を探す
                                for tag in soup.find_all(attrs={"name": True}):
                                    tag_text = tag.get_text()
                                    if kw in tag_text:
                                        result["source_tag"] = tag.get("name")
                                        break
                                result["snippet"] = snippet.strip()[:500]
                                result["notes"] += "RPO extracted from text. "
                                found_order = True
                                break
                    if found_order:
                        break

            # ── 4. テキストから受注高 fallback ──
            if not found_order:
                text_content = soup.get_text()
                for kw in _ORDER_KW:
                    # 「受注高は99,008百万円」形式
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
        for k in ["orders_received", "order_backlog", "construction_carryover",
                  "completed_construction", "rpo"]
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
        print(f"[{i}/{len(targets)}] {t['ticker']} {t['company']} ...", end=" ")
        res = extract_from_company(t, cache_dir=cache_dir)
        print(f"conf={res['confidence']} orders={res['orders_received']} su={res['unit']}")
        results.append(res)

    return results
