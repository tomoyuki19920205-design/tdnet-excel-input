# ============================================================
# fetcher.py — TDnet新着検知（API優先 + HTMLフォールバック二重化）
#   + 過去日付バックフィル取得対応
# ============================================================
from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from datetime import date as date_type, datetime

import requests
from bs4 import BeautifulSoup

from .common_ticker import strip_tdnet_trailing_zero
from .models import DisclosureItem, DisclosureType, FINANCIAL_STATEMENT_KEYWORDS
from .pdf_only_materials import classify_pdf_only_material
from .review_completion import classify_procedural_review_completion
from .security_eligibility import classify_disclosure_security
from .utils import sha256, today_yyyymmdd
from lib.backfill.xbrl_url_inference import infer_xbrl_url_from_pdf

logger = logging.getLogger("tdnet")

# 除外キーワード（「配当」は予想修正内に含まれるため除外しない）
# 注: 「自己株式」は buyback 系開示を通過させるため除外しない。
#     buyback_classifier.py 側で「自己株式の処分」「取得状況/結果」等の除外を行う。
EXCLUDE_KEYWORDS = ["人事", "訴訟", "資本業務提携", "株式分割", "新株予約権"]

# ETF/投信/ファンド除外キーワード（正規化後で判定）
INSTRUMENT_EXCLUDE_KEYWORDS = [
    "etf", "etn", "投資信託", "ファンド", "ishares", "iシェアーズ",
    "アイシェアーズ", "上場投信", "インデックス",
]

_USER_AGENT = "TDnetExcelInput/1.0"

# yanoshin.jp API の既知上限件数（実測: 決算集中日に300件固定で返る）
# この件数に到達した場合、301件目以降を静かに取りこぼすリスクがある。
TDNET_API_FETCH_LIMIT = 300


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
        DisclosureType.FORECAST_REVISION | DisclosureType.FINANCIAL_STATEMENT
        | DisclosureType.DIVIDEND_REVISION | None
    """
    n = normalize_title(title)

    # ── forecast_revision 判定 ──
    # (1) 「業績」または「予想」を含む
    has_gyoseki_or_yoso = ("業績" in n or "予想" in n)
    # (2) 以下のいずれかを含む
    revision_keywords = ["修正", "変更", "上方修正", "下方修正", "差異"]
    has_revision = any(kw in n for kw in revision_keywords)

    if has_gyoseki_or_yoso and has_revision:
        # 「業績」を含まず「配当」だけの場合は
        # forecast_revision ではなく dividend_revision として分類
        if "業績" not in n and "配当" in n:
            return DisclosureType.DIVIDEND_REVISION
        return DisclosureType.FORECAST_REVISION

    # ── dividend_revision 判定 ──
    # 「配当」+修正系キーワードを含むが「予想」を含まないケースもカバー
    if "配当" in n and has_revision:
        return DisclosureType.DIVIDEND_REVISION

    # ── buyback 判定 ──
    # 自己株式取得系（取得枠決議・ToSTNeT等）を通過させる。
    # 以下の強除外パターンが含まれる場合は通さない。
    _BUYBACK_MUST_PASS = [
        "自己株式取得",
        "自己株式の取得",
        "自己株式立会外",
        "tostnet",  # 正規化後は小文字
    ]
    _BUYBACK_HARD_EXCLUDE = [
        "自己株式の処分",
        "自己株式処分",
        "ストックオプション",
        "譲渡制限付株式",
        "持株会",
    ]
    _has_buyback_kw = any(kw in n or kw in title for kw in _BUYBACK_MUST_PASS)
    _has_buyback_excl = any(kw in title for kw in _BUYBACK_HARD_EXCLUDE)
    if _has_buyback_kw and not _has_buyback_excl:
        return DisclosureType.BUYBACK

    # ── viewer-only metadata events (PDF URL is validated downstream) ──
    # Run before the broad "通期決算" financial-statement keyword so
    # "通期決算説明資料" is not sent to numeric statement extraction.
    material = classify_pdf_only_material(title)
    if material:
        return material.event_type

    # A re-filed statement whose only new information is completion of the
    # statutory interim review is raw disclosure history, not a new earnings
    # event.  Explicit corrections/revisions are deliberately excluded by the
    # semantic classifier and continue through the existing routes below.
    if classify_procedural_review_completion(title):
        return DisclosureType.REVIEW_COMPLETION

    # ── financial_statement 判定 ──
    if any(kw in n for kw in FINANCIAL_STATEMENT_KEYWORDS):
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
    Backward-compatible wrapper around the security-first eligibility rule.

    J-Quants/TDnet official attributes are resolved first.  Text matching is
    used only when no authoritative master classification is available.

    Returns:
        True = 除外対象（スキップする）
    """
    item = DisclosureItem(
        disclosure_id="", ticker=ticker, company_name=company_name,
        title=title, doc_url="", published_at="",
    )
    return classify_disclosure_security(item).is_etf_like


def _matches_watchlist(ticker: str, watch_tickers: list[str]) -> bool:
    """ウォッチリストフィルタ（空＝全銘柄対象）"""
    if not watch_tickers:
        return True
    return ticker in watch_tickers


# ============================================================
# ルート1: yanoshin.jp 非公式API（優先）
# ============================================================

DEFAULT_YANOSHIN_API_TIMEOUT_SEC = 15

def _fetch_via_api(session: requests.Session | None = None, timeout_sec: float | None = None) -> list[DisclosureItem]:
    """yanoshin.jp APIから開示一覧を取得"""
    url = "https://webapi.yanoshin.jp/webapi/tdnet/list/today.json"
    logger.info(f"[API] yanoshin.jp APIから取得中: {url}")

    client = session or requests
    actual_timeout = timeout_sec if timeout_sec is not None else DEFAULT_YANOSHIN_API_TIMEOUT_SEC
    resp = client.get(url, headers={"User-Agent": _USER_AGENT}, timeout=actual_timeout)
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
        # API feeds can omit url_xbrl even when the official attachment exists.
        # Infer TDnet's deterministic 1401 PDF -> 0812 XBRL mapping; download
        # still decides availability from the response.
        xbrl_url = xbrl_url or infer_xbrl_url_from_pdf(doc_url)

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


def _fetch_via_html(target_date: str | date_type | None = None, session: requests.Session | None = None) -> list[DisclosureItem]:
    """TDnet HTMLページから開示一覧を取得。

    Args:
        target_date: 取得対象日付。None=今日。YYYY-MM-DD / YYYYMMDD / date 型対応。
        session: 再利用可能な requests.Session (オプショナル)
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
            client = session or requests
            resp = client.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
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
                
                if len(date_str) == 8 and ":" in pub_time:
                    yyyy = date_str[0:4]
                    mm = date_str[4:6]
                    dd = date_str[6:8]
                    # Ensure pub_time has hours and minutes
                    if pub_time.count(":") >= 1:
                        # Convert e.g., "15:00" to "2026-06-05 15:00"
                        pub_time_only = pub_time[:5]
                        pub_time = f"{yyyy}-{mm}-{dd} {pub_time_only}"
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
# ルート0: J-Quants TDnet API（Primary Path / Phase 3）
# ============================================================

def _fetch_via_jquants(
    target_date: str,
    *,
    session: requests.Session | None = None,
) -> list[DisclosureItem]:
    """
    J-Quants /v2/td/list から当日の開示一覧を全件取得し
    既存 DisclosureItem 形式に変換して返す。

    安全制約:
      - JQUANTS_PRIMARY_ENABLED=1 の場合のみ呼ばれる
      - DB保存なし / Discord通知なし / 本番フロー変更なし
      - APIキー・token・.env値は出力しない
      - 失敗した場合は例外を raise して呼び出し元が fallback する

    Args:
        target_date: YYYYMMDD 形式の日付文字列
        session: requests.Session (オプション、テスト用モック注入可)

    Returns:
        list[DisclosureItem] — disclosure_id = sha256(doc_url) で既存仕様に準拠

    Raises:
        任意の例外 — 呼び出し元 (fetch_new_disclosures) が fallback を行う
    """
    from src.jquants.adapter import fetch_jquants_disclosures

    logger.info(
        f"[JQUANTS_PRIMARY_FETCH_START] "
        f"date={target_date!r}"
    )

    # session は adapter 側の _session 引数に渡す
    jq_items = fetch_jquants_disclosures(
        target_date,
        timeout_sec=30.0,
        max_pages=50,
        _session=session,
    )

    results: list[DisclosureItem] = []
    for jq in jq_items:
        # doc_url は TDnet 標準 URL (署名付きURLは使わない)
        # FileID = "1401" + DiscNo → URL は adapter._make_doc_url_from_disc_no() で生成済み
        doc_url = jq.doc_url

        # disclosure_id は既存仕様に合わせて sha256(doc_url)
        disclosure_id = sha256(doc_url)

        item = DisclosureItem(
            disclosure_id=disclosure_id,
            ticker=jq.ticker,
            company_name=jq.company_name,
            title=jq.title,
            doc_url=doc_url,
            published_at=jq.published_at,
            xbrl_url=jq.xbrl_url,  # Shadow Run と同様に None (lazy)
            disclosure_type=jq.disclosure_type,
            source_doc_id=jq.disclosure_id,
        )
        item.tdnet_public_items = tuple(jq.disc_items)
        results.append(item)

    logger.info(
        f"[JQUANTS_PRIMARY_FETCH_DONE] "
        f"date={target_date!r} "
        f"total={len(results)}"
    )
    return results


# ============================================================
# メインエクスポート: 二重化取得
# ============================================================

def fetch_new_disclosures(
    watch_tickers: list[str] | None = None,
    is_processed_fn=None,
    target_date: str | date_type | None = None,
    session: requests.Session | None = None,
    yanoshin_timeout_sec: float | None = None,
) -> list[DisclosureItem]:
    """
    新着開示を取得（API優先 + HTML FB）。新規のみを返す。

    Args:
        watch_tickers: ウォッチリスト（空/Noneで全銘柄）
        is_processed_fn: disclosure_id → bool の冪等性チェック関数
        target_date: 取得対象日付。None=今日（API優先）。
                     過去日付指定時は HTML スクレイピングのみ使用。
        session: 再利用可能な requests.Session (オプショナル)
    """
    watch = watch_tickers or []
    fetched_items: list[DisclosureItem] = []
    source = ""
    possible_truncation = False
    is_backfill = target_date is not None and _parse_target_date(target_date) != today_yyyymmdd()

    # ── J-Quants Primary Path (Phase 3) ──────────────────────────────────
    # JQUANTS_PRIMARY_ENABLED=1 かつ 当日取得の場合のみ J-Quants を第一候補にする。
    # バックフィル時はスキップして既存 HTML スクレイピングへ。
    # 失敗時は [JQUANTS_PRIMARY_FALLBACK] ログを出して既存 YANOSHIN/HTML へ fallback。
    jquants_primary_enabled = os.environ.get("JQUANTS_PRIMARY_ENABLED", "0") == "1"

    if jquants_primary_enabled and not is_backfill:
        date_str = _parse_target_date(target_date)
        try:
            fetched_items = _fetch_via_jquants(date_str, session=session)
            source = "JQUANTS_PRIMARY"
            logger.info(
                f"[JQUANTS_PRIMARY_FETCH_DONE] "
                f"date={date_str!r} "
                f"fetched_count={len(fetched_items)}"
            )
            # J-Quants 成功 → 以降の YANOSHIN/HTML 取得をスキップして
            # フィルタリング処理へ進む
            fetched_count = len(fetched_items)

            # ETF/投信/ファンド除外
            instrument_skipped = 0
            after_instrument: list[DisclosureItem] = []
            for item in fetched_items:
                decision = classify_disclosure_security(item, as_of_date=date_str)
                if decision.is_etf_like:
                    instrument_skipped += 1
                    logger.info(
                        f"[{source}] skip_reason=etf_like_security, "
                        f"code={item.ticker}, name={item.company_name}, "
                        f"classification_source={decision.source}, "
                        f"product_category={decision.product_category or '-'}, "
                        f"title={item.title[:50]}"
                    )
                    continue
                after_instrument.append(item)

            if instrument_skipped > 0:
                logger.info(f"[{source}] instrument_skipped_count={instrument_skipped}")

            # フィルタリング
            filtered_items = [
                item for item in after_instrument
                if _matches_filter(item.title) and _matches_watchlist(item.ticker, watch)
            ]
            filtered_count = len(filtered_items)
            forecast_count = sum(1 for i in filtered_items if i.disclosure_type == DisclosureType.FORECAST_REVISION)
            financial_count = sum(1 for i in filtered_items if i.disclosure_type == DisclosureType.FINANCIAL_STATEMENT)

            logger.info(
                f"[{source}] fetched_count={fetched_count}, filtered_count={filtered_count}, "
                f"forecast_revision_count={forecast_count}, financial_statement_count={financial_count}, "
                f"possible_truncation=False"
            )

            # ウォッチリスト銘柄ごとの生ヒット数
            if watch:
                for code in watch:
                    hit_count = sum(1 for i in fetched_items if i.ticker == code)
                    logger.info(f"[{source}] raw_hit_{code}={hit_count}")

            # 重複排除（DB照合）
            already_processed_count = 0
            already_processed_fs = 0
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

        except Exception as jq_err:
            # J-Quants 失敗 → 既存 YANOSHIN/HTML へ fallback
            logger.warning(
                f"[JQUANTS_PRIMARY_FALLBACK] "
                f"J-Quants primary fetch failed, falling back to YANOSHIN/HTML: {jq_err}"
            )
            # 以降の処理（is_backfill ブランチ）へ fall through
    # ─────────────────────────────────────────────────────────────────────

    if is_backfill:
        # 過去日付: HTML スクレイピングのみ（yanoshin API は当日限定）
        logger.info(f"[BACKFILL] 過去日付モード: target_date={target_date}")
        try:
            fetched_items = _fetch_via_html(target_date, session=session)
            source = "HTML_BACKFILL"
            logger.info(f"[{source}] fetched_count={len(fetched_items)}")
        except Exception as html_err:
            logger.error(f"[HTML_BACKFILL] 取得失敗: {html_err}")
            return []
    else:
        # 当日: API 優先 + HTML フォールバック
        try:
            fetched_items = _fetch_via_api(session=session, timeout_sec=yanoshin_timeout_sec)
            source = "API"
            api_count = len(fetched_items)
            logger.info(f"[{source}] fetched_count={api_count}")

            # ── 300件上限到達チェック ──
            # yanoshin.jp API は300件固定上限を持つ。到達した場合は301件目以降を
            # 取りこぼすリスクがあるため、HTMLクロスチェックで全件をカバーする。
            if api_count >= TDNET_API_FETCH_LIMIT:
                logger.warning(
                    f"[TDNET_FETCH_LIMIT_REACHED] "
                    f"api_count={api_count} limit={TDNET_API_FETCH_LIMIT} "
                    f"date={today_yyyymmdd()} "
                    f"risk=possible_truncation action=html_crosscheck"
                )
                # HTML でも取得して doc_id ベースで dedupe
                html_items: list[DisclosureItem] = []
                html_ok = False
                try:
                    html_items = _fetch_via_html(target_date, session=session)
                    html_ok = True
                    logger.info(
                        f"[TDNET_FETCH_LIMIT_REACHED] "
                        f"html_crosscheck_count={len(html_items)}"
                    )
                    # HTML取得件数がAPI以下の場合はwarning（HTML側も上限の可能性）
                    if len(html_items) <= api_count:
                        logger.warning(
                            f"[TDNET_FETCH_LIMIT_REACHED] "
                            f"html_count={len(html_items)} <= api_count={api_count} "
                            f"html_may_also_be_limited=true"
                        )
                except Exception as html_xc_err:
                    logger.warning(
                        f"[TDNET_FETCH_LIMIT_REACHED] "
                        f"html_crosscheck_failed: {html_xc_err} "
                        f"possible_truncation=true"
                    )

                if html_ok and html_items:
                    # doc_id ベース dedupe: API + HTML を合算して重複排除
                    before_count = len(fetched_items) + len(html_items)
                    seen_ids: set[str] = set()
                    merged: list[DisclosureItem] = []
                    for item in fetched_items + html_items:
                        if item.disclosure_id not in seen_ids:
                            seen_ids.add(item.disclosure_id)
                            merged.append(item)
                    after_count = len(merged)
                    dup_count = before_count - after_count
                    logger.info(
                        f"[TDNET_FETCH_DEDUPED] "
                        f"before={before_count} "
                        f"after={after_count} "
                        f"duplicates={dup_count} "
                        f"key=disclosure_id "
                        f"api_count={api_count} html_count={len(html_items)}"
                    )
                    fetched_items = merged
                    source = "API+HTML_MERGED"
                    # HTML が API より多い件数を取得できていれば取りこぼしなし
                    if len(html_items) > api_count:
                        possible_truncation = False
                        logger.info(
                            f"[TDNET_FETCH_LIMIT_REACHED] "
                            f"resolved=true html_exceeded_api "
                            f"final_count={after_count}"
                        )
                    else:
                        # HTML も同件数以下 → まだリスクあり
                        possible_truncation = True
                        logger.warning(
                            f"[TDNET_FETCH_LIMIT_REACHED] "
                            f"resolved=false html_did_not_exceed_api "
                            f"possible_truncation=true "
                            f"final_count={after_count}"
                        )
                else:
                    # HTML 取得失敗または0件 → API 300件のまま継続するがリスクを明示
                    possible_truncation = True
                    logger.warning(
                        f"[TDNET_FETCH_UNRESOLVED_TRUNCATION_RISK] "
                        f"api_count={api_count} limit={TDNET_API_FETCH_LIMIT} "
                        f"html_crosscheck_failed=true "
                        f"possible_truncation=true "
                        f"action=continue_with_api_only"
                    )

        except Exception as api_err:
            logger.warning(f"[API] 取得失敗、HTMLフォールバックへ: {api_err}")

            try:
                fetched_items = _fetch_via_html(target_date, session=session)
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
        decision = classify_disclosure_security(item, as_of_date=_parse_target_date(target_date))
        if decision.is_etf_like:
            instrument_skipped += 1
            logger.info(
                f"[{source}] skip_reason=etf_like_security, "
                f"code={item.ticker}, name={item.company_name}, "
                f"classification_source={decision.source}, "
                f"product_category={decision.product_category or '-'}, "
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
        f"forecast_revision_count={forecast_count}, financial_statement_count={financial_count}, "
        f"possible_truncation={possible_truncation}"
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

