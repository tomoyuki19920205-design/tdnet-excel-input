#!/usr/bin/env python3
# ============================================================
# ixbrl_probe.py — ZIP内iXBRLの診断CLI
# ============================================================
#
# 使い方:
#   .\.venv\Scripts\python.exe tools\ixbrl_probe.py <ZIPパス>
#
# 出力:
#   - ZIP内 ixbrl.htm / xbrl の一覧 (index付き)
#   - 各ファイルの先頭hex, BOM有無, encoding推定
#   - XMLパース可否 (ET.fromstring → lxml HTMLフォールバック)
#   - 失敗時: 先頭120文字プレビュー + 例外内容
#   - 売上/営業利益 候補 name のスコアリング表示
#   - 単位推定の手掛かり
# ============================================================
from __future__ import annotations

import io
import sys
import os
import zipfile
from xml.etree import ElementTree as ET

# プロジェクトルートを sys.path に追加（tools/ から実行するため）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from src.xbrl_clean import read_xbrl_bytes, detect_encoding_info

# ============================================================
# 定数
# ============================================================

# iXBRLファイルの拡張子パターン
_IXBRL_EXTENSIONS = (".ixbrl.htm", ".ixbrl.html", "-ixbrl.htm", "-ixbrl.html", ".xbrl", ".ixbrl")

# 売上候補キーワード（name属性にこれらが含まれればスコア加算）
_SALES_KEYWORDS = [
    ("NetSales", 10),
    ("Revenue", 8),
    ("OperatingRevenue", 7),
    ("売上高", 10),
    ("売上収益", 9),
    ("営業収益", 7),
    ("経常収益", 6),
    ("NetRevenue", 7),
    ("Sales", 5),
]

# 営業利益候補キーワード
_OP_KEYWORDS = [
    ("OperatingIncome", 10),
    ("OperatingProfit", 10),
    ("営業利益", 10),
    ("営業損益", 8),
    ("OperatingLoss", 6),
    ("ProfitLoss", 5),
]

# ix名前空間プレフィックス（探索用）
_IX_NS_PREFIXES = [
    "{http://www.xbrl.org/2013/inlineXBRL}",
    "{http://www.xbrl.org/2008/inlineXBRL}",
]


# ============================================================
# ユーティリティ
# ============================================================

# lxml HTMLパーサが返すタグ名パターン（名前空間なし）
_IX_LOCAL_TAGS = {
    "nonFraction": ["nonfraction", "ix:nonfraction"],
    "nonNumeric": ["nonnumeric", "ix:nonnumeric"],
}


def _find_ix_elements(root) -> dict[str, list]:
    """
    ix:nonFraction / ix:nonNumeric 要素を走査して返す。

    ET.Element と lxml HtmlElement の両方に対応。
    lxml HTMLパーサは名前空間を保持しないため、
    ローカルタグ名でも検索する。
    """
    result: dict[str, list] = {
        "nonFraction": [],
        "nonNumeric": [],
    }

    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue

        # 標準XMLパーサ: {namespace}localName
        for ns in _IX_NS_PREFIXES:
            if tag == f"{ns}nonFraction":
                result["nonFraction"].append(elem)
                break
            elif tag == f"{ns}nonNumeric":
                result["nonNumeric"].append(elem)
                break
        else:
            # lxml HTMLパーサ: namespace無しのローカルタグ名
            tag_lower = tag.lower()
            for key, patterns in _IX_LOCAL_TAGS.items():
                if tag_lower in patterns:
                    result[key].append(elem)
                    break

    return result


def _score_names(
    elements: list[ET.Element],
    keywords: list[tuple[str, int]],
) -> list[tuple[str, int, str]]:
    """
    要素のname属性をキーワードでスコアリングする。

    Returns:
        [(name, score, unitRef/format), ...] スコア降順
    """
    scored: dict[str, tuple[int, str]] = {}

    for elem in elements:
        name = elem.get("name", "")
        if not name:
            continue

        unit_ref = elem.get("unitRef", "")
        fmt = elem.get("format", "")
        unit_hint = unit_ref or fmt or ""

        for keyword, score in keywords:
            if keyword.lower() in name.lower():
                existing_score = scored.get(name, (0, ""))[0]
                if score > existing_score:
                    scored[name] = (score, unit_hint)

    return sorted(
        [(name, s, hint) for name, (s, hint) in scored.items()],
        key=lambda x: -x[1],
    )


def _try_parse_xml(xml_str: str) -> tuple[ET.Element | None, str, str]:
    """
    XML → HTMLフォールバック の順でパースを試行。

    Returns:
        (root or None, parse_method, error_message)
        parse_method: "XML" / "HTML_FALLBACK" / "FAILED"
    """
    # Step 1: 標準 XML パース
    try:
        root = ET.fromstring(xml_str)
        return root, "XML", ""
    except ET.ParseError as xml_err:
        xml_error = str(xml_err)

    # Step 2: lxml HTML フォールバック（XMLパース失敗時のみ）
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(xml_str.encode("utf-8"))
        # lxml.html は HtmlElement を返す。ElementTree互換のiterを持つ
        # ただし ET.Element ではないので、変換のため lxml.etree を使う
        from lxml import etree as lxml_etree
        # lxml の Element を文字列化して ET で再パース…ではなく
        # lxml の Element をそのまま使う（iterが使えるため）
        return doc, "HTML_FALLBACK", f"(XML失敗: {xml_error})"
    except Exception as html_err:
        return None, "FAILED", f"XML: {xml_error} / HTML: {html_err}"


# ============================================================
# メイン処理
# ============================================================

def probe_zip(zip_path: str) -> int:
    """
    ZIPファイル内のiXBRLを診断する。

    Returns:
        0: 正常終了, 1: エラーあり
    """
    if not os.path.isfile(zip_path):
        print(f"[ERROR] ZIPファイルが見つかりません: {zip_path}")
        return 1

    print("=" * 60)
    print(f"  iXBRL Probe - {os.path.basename(zip_path)}")
    print("=" * 60)
    print()

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except Exception as e:
        print(f"[ERROR] ZIP読み込み失敗: {e}")
        return 1

    # --- ixbrl ファイル列挙 ---
    ixbrl_names = [
        name for name in zf.namelist()
        if any(name.lower().endswith(ext) for ext in _IXBRL_EXTENSIONS)
    ]

    if not ixbrl_names:
        print("[WARN] ZIP内にiXBRL/XBRLファイルが見つかりません")
        print(f"  ZIP内ファイル一覧: {zf.namelist()}")
        zf.close()
        return 1

    print(f"[iXBRL一覧] {len(ixbrl_names)}件")
    for i, name in enumerate(ixbrl_names):
        print(f"  [{i}] {name}")
    print()

    has_error = False
    all_sales_candidates: list[tuple[str, int, str]] = []
    all_op_candidates: list[tuple[str, int, str]] = []
    all_unit_hints: set[str] = set()

    # --- 各ファイルの解析 ---
    for i, name in enumerate(ixbrl_names):
        print(f"--- [{i}] {name} ---")

        raw = zf.read(name)

        # エンコーディング情報
        enc_info = detect_encoding_info(raw)
        print(f"  先頭hex   : {enc_info['head_hex']}")
        print(f"  BOM       : {'あり (' + enc_info['bom_type'] + ')' if enc_info['has_bom'] else 'なし'}")
        print(f"  encoding  : {enc_info['encoding_guess']}")

        # 正規化
        try:
            xml_str = read_xbrl_bytes(raw, debug=True)
        except UnicodeDecodeError as e:
            print(f"  decode    : NG - {e}")
            print(f"  先頭120文字: (デコード不可)")
            has_error = True
            print()
            continue

        # パース
        root, method, error_msg = _try_parse_xml(xml_str)

        if root is None:
            print(f"  parse     : NG - {error_msg}")
            preview = xml_str[:120].replace("\n", "\\n").replace("\r", "\\r")
            print(f"  先頭120文字: {preview}")
            has_error = True
            print()
            continue

        # HTMLフォールバック時: ix要素の存在チェック（必須）
        ix_elements = _find_ix_elements(root)
        ix_nf_count = len(ix_elements["nonFraction"])
        ix_nn_count = len(ix_elements["nonNumeric"])

        if method == "HTML_FALLBACK":
            if ix_nf_count == 0 and ix_nn_count == 0:
                print(f"  parse     : NG — HTMLフォールバック成功だがixタグなし {error_msg}")
                print(f"  判定      : ix:nonFraction=0, ix:nonNumeric=0 → エラー扱い")
                has_error = True
                print()
                continue
            print(f"  parse     : OK (HTMLフォールバック) {error_msg}")
        else:
            print(f"  parse     : OK (XML)")

        print(f"  ix:nonFraction : {ix_nf_count}件")
        print(f"  ix:nonNumeric  : {ix_nn_count}件")

        # --- 売上/営業利益 候補スコアリング ---
        all_elements = ix_elements["nonFraction"] + ix_elements["nonNumeric"]

        sales_candidates = _score_names(all_elements, _SALES_KEYWORDS)
        op_candidates = _score_names(all_elements, _OP_KEYWORDS)

        all_sales_candidates.extend(sales_candidates)
        all_op_candidates.extend(op_candidates)

        # 単位のヒント収集
        for elem in ix_elements["nonFraction"]:
            unit_ref = elem.get("unitRef", "")
            if unit_ref:
                all_unit_hints.add(unit_ref)

        print()

    # --- サマリ ---
    print("=" * 60)
    print("  候補サマリ")
    print("=" * 60)
    print()

    # 売上候補（重複排除、スコア順）
    print("[売上/売上高/Revenue 候補]")
    seen_sales: set[str] = set()
    displayed = 0
    for name, score, hint in sorted(all_sales_candidates, key=lambda x: -x[1]):
        if name not in seen_sales:
            seen_sales.add(name)
            unit_str = f" (unit: {hint})" if hint else ""
            print(f"  score={score:2d}  {name}{unit_str}")
            displayed += 1
            if displayed >= 10:
                break
    if displayed == 0:
        print("  (候補なし)")
    print()

    # 営業利益候補
    print("[営業利益/OperatingIncome 候補]")
    seen_op: set[str] = set()
    displayed = 0
    for name, score, hint in sorted(all_op_candidates, key=lambda x: -x[1]):
        if name not in seen_op:
            seen_op.add(name)
            unit_str = f" (unit: {hint})" if hint else ""
            print(f"  score={score:2d}  {name}{unit_str}")
            displayed += 1
            if displayed >= 10:
                break
    if displayed == 0:
        print("  (候補なし)")
    print()

    # 単位ヒント
    print("[単位 (unitRef) 一覧]")
    if all_unit_hints:
        for hint in sorted(all_unit_hints):
            print(f"  {hint}")
    else:
        print("  (検出なし)")
    print()

    zf.close()

    if has_error:
        print("[結果] 一部ファイルでエラーあり")
        return 1
    else:
        print("[結果] 全ファイル parse: OK")
        return 0


# ============================================================
# エントリポイント
# ============================================================

def main():
    # Windows cp932 コンソールでの UnicodeEncodeError を防止
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    if len(sys.argv) < 2:
        print("使い方: python tools/ixbrl_probe.py <ZIPファイルパス>")
        print("例:     ..\\.venv\\Scripts\\python.exe tools\\ixbrl_probe.py .\\data\\docs\\081220260225568550.zip")
        sys.exit(1)

    zip_path = sys.argv[1]
    exit_code = probe_zip(zip_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
