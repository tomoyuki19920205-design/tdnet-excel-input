# ============================================================
# fetcher.py — TDnet新着検知（API優先 + HTMLフォールバック二重化）
#   + 過去日付バックフィル取得対応
# ============================================================
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date as date_type, datetime

import requests
from bs4 import BeautifulSoup

from .common_ticker import strip_tdnet_trailing_zero
from .models import DisclosureItem, DisclosureType
from .utils import sha256, today_yyyymmdd
from lib.backfill.xbrl_url_inference import infer_xbrl_url_from_pdf

logger = logging.getLogger("tdnet")

# 除外キーワード（「配当」は予想修正内に含まれるため除外しない）
EXCLUDE_KEYWORDS = ["自己株式", "人事", "訴訟", "資本業務提携", "株式分割", "新株予約権"]

# ETF/投信/ファンド除外キーワード（正規化後で判定）
INSTRUMENT_EXCLUDE_KEYWORDS = [
    "etf", "etn", "投資信託", "ファンド", "ishares", "iシェアーズ",
    "アイシェアーズ", "上場投信", "インデックス",
]

_USER_AGENT = "TDnetExcelInput/1.0"


# ============================================================
# タイトル正規化 & 分類
# ============================================================

def normalize_title(title: str) -> str:
    """
    タイトルを正規化する。
    - 全角→半角（英数字・記号）
    - 改行除去
    - スペース除去
    - 小文字化
    """
    # 改行除去
    s = title.replace("\n", "").replace("\r", "")
    # 全角英数字→半角
    s = unicodedata.normalize("NFKC", s)
    # スペース除去（全角半角両方）
    s = re.sub(r"\s+", "", s)
    # 小文字化
    s = s.lower()
    return s


def classify_disclosure(title: str) -> str | None:
    """
    タイトルから開示タイプを分類する。

    Returns:
        DisclosureType.FORECAST_REVISION | DisclosureType.FINANCIAL_STATEMENT | None
    """
    n = normalize_title(title)

    # ── forecast_revision 判定 ──
    # (1) 「業績」または「予想」を含む
    has_gyoseki_or_yoso = ("業績" in n or "予想" in n)
    # (2) 以下のいずれかを含む
    revision_keywords = ["修正", "変更", "上方修正", "下方修正", "差異"]
    has_revision = any(kw in n for kw in revision_keywords)

    if has_gyoseki_or_yoso and has_revision:
        # 「業績」を含まず「配当」だけの場合は対象外
        # 例: 「配当予想の修正」→ 対象外, 「業績予想及び配当予想の修正」→ 対象
        if "業績" not in n and "配当" in n:
            return None
        return DisclosureType.FORECAST_REVISION

    # ── financial_statement 判定 ──
    fs_keywords = ["決算短信", "四半期決算", "通期決算", "訂正決算短信"]
    if any(kw in n for kw in fs_keywords):
        return DisclosureType.FINANCIAL_STATEMENT

    return None


def _matches_filter(title: str) -> bool:
    """対象タイトルかどうかフィルタリング（分類ベース + 除外キーワード）"""
    # 分類可能なら対象
    dtype = classify_disclosure(title)
    if dtype is None:
        return False
    # 除外チェック（正規化前のオリジナルタイトルで判定）
    if any(kw in title for kw in EXCLUDE_KEYWORDS):
        return False
    return True


def is_instrument_excluded(ticker: str, title: str, company_name: str) -> bool:
    """
    ETF/投信/ファンド等の開示かどうかを判定する。

    判定方法（OR条件）:
    - 正規化後のタイトルまたは銘柄名にINSTRUMENT_EXCLUDE_KEYWORDSいずれかを含む
    - 銘柄コードがETF帯（1300-1799）かつタイトル/銘柄名にキーワードを含む

    Returns:
        True = 除外対象（スキップする）
    """
    n_title = normalize_title(title)
    n_name = normalize_title(company_name)

    # キーワード判定（正規化後で部分一致）
    for kw in INSTRUMENT_EXCLUDE_KEYWORDS:
        if kw in n_title or kw in n_name:
            return True

    # ETFコード帯（1300-1799）+ キーワード併用
    # コード帯だけでは誤判定があるため、タイトル/銘柄名に
    # 一般的なETF関連語が含まれるか簡易チェック
    # ETFコード帯チェック: 英字付きコードは数値変換できないのでスキップ
    if ticker.isdigit():
        code_num = int(ticker)
        if 1300 <= code_num <= 1799:
            # コード帯に該当する場合、追加の簡易キーワードでも判定
            etf_hints = ["連動", "指数", "日経", "topix", "s&p", "ナスダック",
                         "nasdaq", "ダウ", "reit", "リート", "レバレッジ",
                         "インバース", "ブル", "ベア", "原油", "金価格"]
            for hint in etf_hints:
                if hint in n_title or hint in n_name:
                    return True

    return False


def _matches_watchlist(ticker: str, watch_tickers: list[str]) -> bool:
    """ウォッチリストフィルタ（空＝全銘柄対象）"""
    if not watch_tickers:
        return True
    return ticker in watch_tickers


# ============================================================
# ルート1: yanoshin.jp 非公式API（優先）
# ============================================================

def _fetch_via_api() -> list[DisclosureItem]:
    """yanoshin.jp APIから開示一覧を取得"""
    url = "https://webapi.yanoshin.jp/webapi/tdnet/list/today.json"
    logger.info(f"[API] yanoshin.jp APIから取得中: {url}")

    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    resp_preview = resp.text[:200] if resp.text else "(empty)"

    try:
        data = resp.json()
    except Exception as e:
        logger.error(
            f"[API] JSONパース失敗: {e} "
            f"status_code={resp.status_code}, content_type={content_type}, "
            f"response_preview={resp_preview}"
        )
        raise

    # ── JSONスキーマ自動判定 ──
    detected_schema = "unknown"
    items_raw: list[dict] = []

    if isinstance(data, dict):
        if "items" in data:
            items_raw = data["items"]
            detected_schema = "items"
        elif "list" in data:
            items_raw = data["list"]
            detected_schema = "list"
        else:
            logger.error(
                f"[API] 未知のJSONスキーマ: 'items'/'list' キーが見つかりません. "
                f"status_code={resp.status_code}, content_type={content_type}, "
                f"response_preview={resp_preview}, detected_schema={detected_schema}, "
                f"keys={list(data.keys())}"
            )
            raise ValueError(f"未知のAPIレスポンススキーマ: keys={list(data.keys())}")
    elif isinstance(data, list):
        items_raw = data
        detected_schema = "root_array"
    else:
        logger.error(
            f"[API] 予期しないレスポンス型: {type(data).__name__}. "
            f"status_code={resp.status_code}, content_type={content_type}, "
            f"response_preview={resp_preview}, detected_schema={detected_schema}"
        )
        raise ValueError(f"予期しないAPIレスポンス型: {type(data).__name__}")

    if not isinstance(items_raw, list):
        logger.error(
            f"[API] items_raw がリストではありません: {type(items_raw).__name__}. "
            f"detected_schema={detected_schema}"
        )
        raise ValueError(f"items_raw がリストではありません: {type(items_raw).__name__}")

    logger.info(f"[API] detected_schema={detected_schema}, raw_items_count={len(items_raw)}")

    results: list[DisclosureItem] = []
    skipped_count = 0

    for item in items_raw:
        # {"Tdnet": {...}} ラッパーの正規化
        if isinstance(item, dict) and "Tdnet" in item:
            item = item["Tdnet"]

        if not isinstance(item, dict):
            skipped_count += 1
            continue

        # フィールド名マッピング（実API: company_code, pubdate, url_xbrl 等に対応）
        title = (
            item.get("Ttitle")
            or item.get("title")
            or ""
        )
        ticker = strip_tdnet_trailing_zero(
            item.get("Tcode")
            or item.get("company_code")
            or item.get("code")
            or ""
        )

        company_name = (
            item.get("Tname")
            or item.get("company_name")
            or item.get("name")
            or ""
        )
        doc_url = (
            item.get("TdocURL")
            or item.get("document_url")
            or item.get("url")
            or ""
        )
        published_at = (
            item.get("Ttime")
            or item.get("pubdate")
            or item.get("time")
            or ""
        )
        xbrl_url = (
            item.get("TxbrlURL")
            or item.get("url_xbrl")
            or item.get("xbrl_url")
        )

        if not title or not ticker or not doc_url:
            skipped_count += 1
            continue

        # 分類
        dtype = classify_disclosure(title) or ""

        results.append(DisclosureItem(
            disclosure_id=sha256(doc_url),
            ticker=ticker,
            company_name=company_name,
            title=title,
            doc_url=doc_url,
            published_at=published_at,
            xbrl_url=xbrl_url,
            disclosure_type=dtype,
        ))

    if skipped_count > 0:
        logger.warning(f"[API] {skipped_count}件をスキップ（title/ticker/doc_url欠損）")

    # 先頭1件プレビューログ
    if results:
        first = results[0]
        logger.info(
            f"[API] first_item_preview: "
            f"id={first.disclosure_id[:12]}..., pubdate={first.published_at}, "
            f"title={first.title[:40]}, code={first.ticker}"
        )

    return results


# ============================================================
# ルート2: TDnet HTMLスクレイピング（フォールバック + 過去日付対応）
# ============================================================

def _build_tdnet_list_url(page: int, target_date: str) -> str:
    """TDnet 開示一覧 URL を生成する。

    Args:
        page: ページ番号 (1-based)
        target_date: YYYYMMDD 形式の日付文字列

    Returns:
        TDnet 一覧ページ URL
    """
    page_str = str(page).zfill(3)
    return f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{target_date}.html"


def _parse_target_date(target_date: str | date_type | None) -> str:
    """target_date を YYYYMMDD 文字列に正規化する。

    Args:
        target_date: YYYY-MM-DD / YYYYMMDD / date オブジェクト / None (=今日)

    Returns:
        YYYYMMDD 形式の文字列
    """
    if target_date is None:
        return today_yyyymmdd()
    if isinstance(target_date, date_type):
        return target_date.strftime("%Y%m%d")
    # 文字列: YYYY-MM-DD → YYYYMMDD
    s = str(target_date).replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"target_date のフォーマットが不正: {target_date!r} (YYYY-MM-DD or YYYYMMDD)")
    return s


def _fetch_via_html(target_date: str | date_type | None = None) -> list[DisclosureItem]:
    """TDnet HTMLページから開示一覧を取得。

    Args:
        target_date: 取得対象日付。None=今日。YYYY-MM-DD / YYYYMMDD / date 型対応。
    """
    date_str = _parse_target_date(target_date)
    is_backfill = (date_str != today_yyyymmdd())
    results: list[DisclosureItem] = []
    total_pages_scanned = 0

    if is_backfill:
        logger.info(f"[HTML] バックフィルモード: target_date={date_str}")

    for page in range(1, 21):
        url = _build_tdnet_list_url(page, date_str)
        logger.info(f"[HTML] TDnet HTMLから取得中: {url} (page={page})")

        page_item_count = 0
        try:
            resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
            if resp.status_code == 404:
                logger.info(f"[HTML] page={page} → 404 (ページ終端)")
                break  # ページ終端
            resp.raise_for_status()

            # TDnet HTML は UTF-8 だが Content-Type にcharset指定がない場合
            # requests がISO-8859-1と誤認するため明示設定
            resp.encoding = "utf-8"

            soup = BeautifulSoup(resp.text, "html.parser")
            for tr in soup.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue

                pub_time = tds[0].get_text(strip=True)
                ticker = strip_tdnet_trailing_zero(tds[1].get_text(strip=True))
                company_name = tds[2].get_text(strip=True)
                title = tds[3].get_text(strip=True)

                # PDFリンクを探す
                link_tag = tds[3].find("a") or tr.find("a")
                href = link_tag.get("href", "") if link_tag else ""
                if href and not href.startswith("http"):
                    href = f"https://www.release.tdnet.info/inbs/{href}"

                if ticker and title and href:
                    dtype = classify_disclosure(title) or ""
                    # XBRL URL 推定 (PDF URL 1401→0812 パターン)
                    inferred_xbrl = infer_xbrl_url_from_pdf(href)
                    results.append(DisclosureItem(
                        disclosure_id=sha256(href),
                        ticker=ticker,
                        company_name=company_name,
                        title=title,
                        doc_url=href,
                        published_at=pub_time,
                        xbrl_url=inferred_xbrl,
                        disclosure_type=dtype,
                    ))
                    page_item_count += 1

        except Exception:
            if page == 1:
                raise  # 1ページ目失敗は致命的
            logger.warning(f"[HTML] page={page} 取得失敗、中断")
            break

        total_pages_scanned += 1
        logger.info(f"[HTML] page={page} items_found={page_item_count}")

        if page_item_count == 0:
            logger.info(f"[HTML] page={page} → 0件 (打ち切り)")
            break

        time.sleep(0.5)  # 礼儀正しいクロール間隔

    logger.info(
        f"[HTML] target_date={date_str} total_pages={total_pages_scanned} "
        f"total_items={len(results)}"
    )
    return results


# ============================================================
# メインエクスポート: 二重化取得
# ============================================================

def fetch_new_disclosures(
    watch_tickers: list[str] | None = None,
    is_processed_fn=None,
    target_date: str | date_type | None = None,
) -> list[DisclosureItem]:
    """
    新着開示を取得（API優先 + HTML FB）。新規のみを返す。

    Args:
        watch_tickers: ウォッチリスト（空/Noneで全銘柄）
        is_processed_fn: disclosure_id → bool の冪等性チェック関数
        target_date: 取得対象日付。None=今日（API優先）。
                     過去日付指定時は HTML スクレイピングのみ使用。
    """
    watch = watch_tickers or []
    fetched_items: list[DisclosureItem] = []
    source = ""
    is_backfill = target_date is not None and _parse_target_date(target_date) != today_yyyymmdd()

    if is_backfill:
        # 過去日付: HTML スクレイピングのみ（yanoshin API は当日限定）
        logger.info(f"[BACKFILL] 過去日付モード: target_date={target_date}")
        try:
            fetched_items = _fetch_via_html(target_date)
            source = "HTML_BACKFILL"
            logger.info(f"[{source}] fetched_count={len(fetched_items)}")
        except Exception as html_err:
            logger.error(f"[HTML_BACKFILL] 取得失敗: {html_err}")
            return []
    else:
        # 当日: API 優先 + HTML フォールバック
        try:
            fetched_items = _fetch_via_api()
            source = "API"
            logger.info(f"[{source}] fetched_count={len(fetched_items)}")
        except Exception as api_err:
            logger.warning(f"[API] 取得失敗、HTMLフォールバックへ: {api_err}")

            try:
                fetched_items = _fetch_via_html(target_date)
                source = "HTML"
                logger.info(f"[{source}] fetched_count={len(fetched_items)}")
            except Exception as html_err:
                logger.error(f"[HTML] フォールバックも失敗: {html_err}")
                return []

    fetched_count = len(fetched_items)

    # ETF/投信/ファンド除外（ログ付き）
    instrument_skipped = 0
    after_instrument: list[DisclosureItem] = []
    for item in fetched_items:
        if is_instrument_excluded(item.ticker, item.title, item.company_name):
            instrument_skipped += 1
            logger.info(
                f"[{source}] skip_reason=instrument_excluded, "
                f"code={item.ticker}, name={item.company_name}, "
                f"title={item.title[:50]}"
            )
            continue
        after_instrument.append(item)

    if instrument_skipped > 0:
        logger.info(f"[{source}] instrument_skipped_count={instrument_skipped}")

    # フィルタリング（分類 + 除外キーワード + ウォッチリスト）
    filtered_items = [
        item for item in after_instrument
        if _matches_filter(item.title) and _matches_watchlist(item.ticker, watch)
    ]
    filtered_count = len(filtered_items)

    # カウント集計
    forecast_count = sum(1 for i in filtered_items if i.disclosure_type == DisclosureType.FORECAST_REVISION)
    financial_count = sum(1 for i in filtered_items if i.disclosure_type == DisclosureType.FINANCIAL_STATEMENT)

    logger.info(
        f"[{source}] fetched_count={fetched_count}, filtered_count={filtered_count}, "
        f"forecast_revision_count={forecast_count}, financial_statement_count={financial_count}"
    )

    # ウォッチリスト銘柄ごとの生ヒット数
    if watch:
        for code in watch:
            hit_count = sum(1 for i in fetched_items if i.ticker == code)
            logger.info(f"[{source}] raw_hit_{code}={hit_count}")

    # 重複排除（DB照合）+ 既処理除外ログ
    already_processed_count = 0
    already_processed_fs = 0  # financial_statement の既処理数
    already_processed_tickers: list[str] = []
    if is_processed_fn:
        new_items = []
        for item in filtered_items:
            if is_processed_fn(item.disclosure_id):
                already_processed_count += 1
                if item.disclosure_type == DisclosureType.FINANCIAL_STATEMENT:
                    already_processed_fs += 1
                    already_processed_tickers.append(item.ticker)
            else:
                new_items.append(item)
    else:
        new_items = filtered_items

    new_count = len(new_items)
    new_fs = sum(1 for i in new_items if i.disclosure_type == DisclosureType.FINANCIAL_STATEMENT)

    logger.info(
        f"[{source}] new_count={new_count} "
        f"already_processed={already_processed_count} "
        f"already_processed_financial_statement={already_processed_fs} "
        f"new_financial_statement={new_fs}"
    )
    if already_processed_tickers:
        logger.info(
            f"[{source}] already_processed_tickers="
            f"{','.join(already_processed_tickers[:20])}"
        )
    return new_items

