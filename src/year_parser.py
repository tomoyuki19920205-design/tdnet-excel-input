# ============================================================
# year_parser.py — 年度抽出・R表記変換
# ============================================================
from __future__ import annotations

import calendar
import logging
import re

logger = logging.getLogger("tdnet")

# 令和元年 = 2019年
_REIWA_BASE = 2018  # R1 = 2019 - 2018 = 1


def to_reiwa(ad_year: int, month: int) -> str:
    """
    西暦年と月 → R表記
    例: 2026, 3 → "R8/3"
    """
    reiwa_year = ad_year - _REIWA_BASE
    if reiwa_year < 1:
        raise ValueError(f"令和に変換できません: {ad_year}年")
    return f"R{reiwa_year}/{month}"


def parse_reiwa(r_str: str) -> tuple[int, int] | None:
    """
    R表記 → (西暦年, 月)
    例: "R8/3" → (2026, 3)
    """
    m = re.match(r"^R(\d+)/(\d+)$", r_str)
    if not m:
        return None
    reiwa_year = int(m.group(1))
    month = int(m.group(2))
    ad_year = reiwa_year + _REIWA_BASE
    return (ad_year, month)


def _era_period_to_iso(period: str) -> str:
    """
    R表記 → ISO日付文字列に変換する。
    例: "R8/3" → "2026-03-31"、"R7/3" → "2025-03-31"
    変換不能な形式はそのまま返す。
    """
    parsed = parse_reiwa(period)
    if parsed is None:
        return period
    ad_year, month = parsed
    last_day = calendar.monthrange(ad_year, month)[1]
    return f"{ad_year}-{month:02d}-{last_day:02d}"


def detect_quarter(title: str) -> str | None:
    """
    タイトルから四半期を検出する。

    対応パターン:
    - "第1四半期" / "第１四半期" → "1Q"
    - "第2四半期" / "第２四半期" → "2Q"
    - "第3四半期" / "第３四半期" → "3Q"
    - "通期" / "本決算" → "4Q"
    - "中間" / "中間期" → "2Q"
    """
    # 全角数字→半角
    normalized = title.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    m = re.search(r"第(\d)四半期", normalized)
    if m:
        q = int(m.group(1))
        if 1 <= q <= 3:
            return f"{q}Q"

    # 通期 = 4Q
    if "通期" in title or "本決算" in title:
        return "4Q"

    # 中間期 / 中間 = 2Q
    if "中間期" in title or "中間" in title:
        return "2Q"

    # 累計（第2四半期累計など、既に上で拾えるが単独出現対応）
    if "累計" in title and "第2" in normalized:
        return "2Q"

    # 英語表記
    m_en = re.search(r"(\d)(?:st|nd|rd|th)\s*Quarter", title, re.IGNORECASE)
    if m_en:
        q = int(m_en.group(1))
        if 1 <= q <= 4:
            return f"{q}Q"

    if re.search(r"Full[- ]?Year|Annual", title, re.IGNORECASE):
        return "4Q"

    return None


def detect_all_quarters(text: str) -> list[str]:
    """
    テキスト内の全Qを検出してリストで返す（重複なし、出現順）。

    対応パターン:
    - "第N四半期" → "NQ"
    - "通期" / "通期連結" → "4Q"
    - "中間" / "中間期" → "2Q"
    - 英語: "Nth Quarter", "Full Year"
    """
    # 全角数字→半角
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    found: list[str] = []
    seen: set[str] = set()

    def _add(q: str):
        if q not in seen:
            found.append(q)
            seen.add(q)

    # 第N四半期
    for m in re.finditer(r"第(\d)四半期", normalized):
        q = int(m.group(1))
        if 1 <= q <= 3:
            _add(f"{q}Q")

    # 通期
    if "通期" in text or "本決算" in text:
        _add("4Q")

    # 中間
    if "中間期" in text or "中間" in text:
        _add("2Q")

    # 英語
    for m_en in re.finditer(r"(\d)(?:st|nd|rd|th)\s*Quarter", text, re.IGNORECASE):
        q = int(m_en.group(1))
        if 1 <= q <= 4:
            _add(f"{q}Q")

    if re.search(r"Full[- ]?Year|Annual", text, re.IGNORECASE):
        _add("4Q")

    return found


def extract_fiscal_year_from_title(title: str) -> str | None:
    """
    タイトルから決算年度をR表記で抽出する。

    対応パターン:
    - "令和8年3月期" → "R8/3"
    - "令和10年12月期" → "R10/12"
    - "2026年3月期" → "R8/3"
    """
    # 全角数字→半角
    normalized = title.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    # 令和パターン
    m = re.search(r"令和(\d+)年(\d+)月期", normalized)
    if m:
        reiwa_year = int(m.group(1))
        month = int(m.group(2))
        return f"R{reiwa_year}/{month}"

    # 西暦パターン
    m = re.search(r"(20\d{2})年(\d+)月期", normalized)
    if m:
        ad_year = int(m.group(1))
        month = int(m.group(2))
        return to_reiwa(ad_year, month)

    return None


def extract_fiscal_year_from_text(text: str) -> str | None:
    """
    PDFテキストから決算年度をR表記で抽出する。
    タイトルより広い範囲を探索する。
    """
    # 全角数字→半角
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    # 令和パターン（本文中）
    m = re.search(r"令和(\d+)年(\d+)月期", normalized)
    if m:
        reiwa_year = int(m.group(1))
        month = int(m.group(2))
        return f"R{reiwa_year}/{month}"

    # 西暦パターン（本文中）
    m = re.search(r"(20\d{2})年(\d+)月期", normalized)
    if m:
        ad_year = int(m.group(1))
        month = int(m.group(2))
        return to_reiwa(ad_year, month)

    return None


def extract_all_fiscal_years(text: str) -> list[str]:
    """
    テキスト内の全年度をR表記でリストとして返す（重複なし）。

    例: "2026年3月期...2027年3月期..." → ["R8/3", "R9/3"]
    """
    # 全角数字→半角
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    found: list[str] = []
    seen: set[str] = set()

    def _add(fy: str):
        if fy not in seen:
            found.append(fy)
            seen.add(fy)

    # 令和パターン
    for m in re.finditer(r"令和(\d+)年(\d+)月期", normalized):
        reiwa_year = int(m.group(1))
        month = int(m.group(2))
        _add(f"R{reiwa_year}/{month}")

    # 西暦パターン
    for m in re.finditer(r"(20\d{2})年(\d+)月期", normalized):
        ad_year = int(m.group(1))
        month = int(m.group(2))
        try:
            _add(to_reiwa(ad_year, month))
        except ValueError:
            pass

    return found


def extract_fiscal_info(
    title: str, text: str | None = None, published_at: str | None = None
) -> tuple[str | None, str | None]:
    """
    年度と四半期を抽出する（優先順位付き統合関数）

    Returns:
        (fiscal_year, quarter) — 例: ("R8/3", "2Q")
        抽出できない場合は None
    """
    # 四半期はタイトルから検出
    quarter = detect_quarter(title)

    # 年度は優先順位つき
    # 1. タイトルから
    fiscal_year = extract_fiscal_year_from_title(title)

    # 2. 本文から
    if fiscal_year is None and text:
        fiscal_year = extract_fiscal_year_from_text(text)

    # 3. 推定不能 → Noneのまま

    # R表記 → ISO日付に変換 ("R8/3" → "2026-03-31")
    if fiscal_year is not None:
        fiscal_year = _era_period_to_iso(fiscal_year)

    return (fiscal_year, quarter)
