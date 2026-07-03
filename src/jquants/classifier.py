"""classifier.py — J-Quants DiscItemsコード → DisclosureType 変換

TDnet 開示分類コード (DiscItems) から既存 DisclosureType への変換を行う。
タイトルキーワード分類のフォールバックも提供する。

禁止事項（このモジュールは以下を一切行わない）:
  - DB更新
  - Discord通知
  - 環境変数・認証情報の出力
"""
from __future__ import annotations

import unicodedata
import re
from typing import Optional

# 既存モデルの参照（読み取りのみ）
from src.models import DisclosureType


# ============================================================
# DiscItems コード → DisclosureType マッピング
# ============================================================
# 調査結果に基づく実測マッピング (2026-07-03 調査済み)
# https://www.release.tdnet.info/inbs/ のTDnet標準コード体系に基づく推定

_DISC_ITEMS_MAP: dict[str, str] = {
    # ── 決算短信 ──────────────────────────────────────────
    "11301": DisclosureType.FINANCIAL_STATEMENT,   # 通期決算短信 (日本基準/連結)
    "11302": DisclosureType.FINANCIAL_STATEMENT,   # 通期決算短信 (日本基準/単体)
    "11303": DisclosureType.FINANCIAL_STATEMENT,   # 通期決算短信 (IFRS/連結)
    "11304": DisclosureType.FINANCIAL_STATEMENT,   # 四半期決算短信 (日本基準/連結)
    "11305": DisclosureType.FINANCIAL_STATEMENT,   # 四半期決算短信 (日本基準/単体)
    "11306": DisclosureType.FINANCIAL_STATEMENT,   # 四半期決算短信 (IFRS/連結)
    "11307": DisclosureType.FINANCIAL_STATEMENT,   # 四半期決算短信 (US-GAAP/連結)
    "11308": DisclosureType.FINANCIAL_STATEMENT,   # 四半期決算短信 (IFRS+日基準/単体)
    "11309": DisclosureType.FINANCIAL_STATEMENT,   # 決算短信 (その他基準)
    "36507": DisclosureType.FINANCIAL_STATEMENT,   # ETF/投信系決算短信 (後でETF除外適用)

    # ── 業績予想修正 ───────────────────────────────────────
    "11350": DisclosureType.FORECAST_REVISION,     # 業績予想修正
    "11351": DisclosureType.FORECAST_REVISION,     # 業績予想修正 (上方)
    "11352": DisclosureType.FORECAST_REVISION,     # 業績予想修正 (下方)
    "11353": DisclosureType.FORECAST_REVISION,     # 業績予想修正 (差異)
    "11354": DisclosureType.FORECAST_REVISION,     # 業績予想の修正 (連結)
    "11299": DisclosureType.FORECAST_REVISION,     # 業績予想関連その他

    # ── 配当予想修正 ───────────────────────────────────────
    "11360": DisclosureType.DIVIDEND_REVISION,     # 配当予想修正
    "11361": DisclosureType.DIVIDEND_REVISION,     # 配当予想修正 (増配)
    "11362": DisclosureType.DIVIDEND_REVISION,     # 配当予想修正 (減配)
    "11363": DisclosureType.DIVIDEND_REVISION,     # 配当予想修正 (復配)

    # ── 自社株買い ─────────────────────────────────────────
    "11101": DisclosureType.BUYBACK,               # 自己株式取得 (ToSTNeT含む)
    "11102": DisclosureType.BUYBACK,               # 自己株式取得に係る事項決定
    "11103": DisclosureType.BUYBACK,               # 自己株式取得状況
    "11104": DisclosureType.BUYBACK,               # 自己株式立会外買付取引
    "11105": DisclosureType.BUYBACK,               # 自己株式取得結果
    "11106": DisclosureType.BUYBACK,               # 自己株式取得終了
}

# DiscItems コードのうち、BUYBACK判定では除外すべき処分/消却系
# (これらは buyback_classifier.py 側の HARD_EXCLUDE と同様の除外が必要)
_BUYBACK_EXCLUDE_CODES: set[str] = {
    "11402",   # 自己株式処分
    "11403",   # 自己株式消却
}


# ============================================================
# DiscItems コード → DisclosureType 変換
# ============================================================

def classify_by_disc_items(disc_items: list[str]) -> Optional[str]:
    """
    DiscItems コードリストから DisclosureType を判定する。

    優先順位:
      1. 決算短信コード (11301-11309, 36507)
      2. 業績予想修正コード (11350-11354, 11299)
      3. 配当予想修正コード (11360-11363)
      4. 自社株買いコード (11101-11106) — 処分/消却コードを含まない場合のみ

    Returns:
        DisclosureType 定数 or None (対象外)
    """
    if not disc_items:
        return None

    has_buyback_exclude = any(c in _BUYBACK_EXCLUDE_CODES for c in disc_items)
    result: Optional[str] = None

    for code in disc_items:
        mapped = _DISC_ITEMS_MAP.get(code)
        if mapped is None:
            continue

        # 決算短信・業績修正・配当修正は即採用
        if mapped in (
            DisclosureType.FINANCIAL_STATEMENT,
            DisclosureType.FORECAST_REVISION,
            DisclosureType.DIVIDEND_REVISION,
        ):
            result = mapped
            break  # 上位種別が見つかれば確定

        # BUYBACK: 処分/消却コードがある場合は除外
        if mapped == DisclosureType.BUYBACK and not has_buyback_exclude:
            result = mapped
            # break しない — より上位の種別が後に来る可能性

    return result


# ============================================================
# タイトルキーワード分類（フォールバック）
# ============================================================

def _normalize_title(title: str) -> str:
    """タイトル正規化 (既存 fetcher.normalize_title と同ロジック)"""
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def classify_by_title_fallback(title: str) -> Optional[str]:
    """
    DiscItemsコードによる分類が None の場合のタイトルキーワードフォールバック。
    既存 fetcher.classify_disclosure() と同じロジックを踏襲する。

    注意: 既存 fetcher.py には依存しない（独立実装）。
    """
    n = _normalize_title(title)

    # 決算短信
    fs_kws = ["決算短信", "四半期決算", "通期決算", "訂正決算短信"]
    if any(kw in n for kw in fs_kws):
        return DisclosureType.FINANCIAL_STATEMENT

    # 業績予想修正
    has_gyoseki_or_yoso = ("業績" in n or "予想" in n)
    revision_kws = ["修正", "変更", "上方修正", "下方修正", "差異"]
    has_revision = any(kw in n for kw in revision_kws)
    if has_gyoseki_or_yoso and has_revision:
        if "業績" not in n and "配当" in n:
            return DisclosureType.DIVIDEND_REVISION
        return DisclosureType.FORECAST_REVISION

    # 配当予想修正
    if "配当" in n and has_revision:
        return DisclosureType.DIVIDEND_REVISION

    # 自社株買い
    buyback_must = ["自己株式取得", "自己株式の取得", "自己株式立会外", "tostnet"]
    buyback_hard_excl = ["自己株式の処分", "自己株式処分", "ストックオプション", "譲渡制限付株式", "持株会"]
    has_buyback = any(kw in n or kw in title for kw in buyback_must)
    has_excl = any(kw in title for kw in buyback_hard_excl)
    if has_buyback and not has_excl:
        return DisclosureType.BUYBACK

    return None


# ============================================================
# 統合分類 (DiscItems優先 + タイトルFB)
# ============================================================

def classify_disclosure_jquants(disc_items: list[str], title: str) -> Optional[str]:
    """
    DiscItemsコード優先、フォールバックとしてタイトルキーワードで分類する。

    Returns:
        DisclosureType 定数 or None
    """
    result = classify_by_disc_items(disc_items)
    if result is not None:
        return result
    return classify_by_title_fallback(title)
