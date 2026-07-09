"""adapter.py — J-Quants TDnet アドオン APIクライアント + DisclosureItem 変換

Shadow Run専用。本番フローへの接続なし。

禁止事項（このモジュールは以下を一切行わない）:
  - DB更新 (INSERT / UPDATE / UPSERT / DELETE)
  - SQLite更新
  - Discord通知
  - 認証情報・APIキー・トークンの出力 (ログ・print含む)
  - 既存 fetcher.py の呼び出し・変更
  - .env 変更
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from src.events.env_loader import load_project_env
from src.models import DisclosureItem
from src.jquants.classifier import classify_disclosure_jquants
from lib.pipeline.retry_helper import with_retry

logger = logging.getLogger("jquants.adapter")

# ============================================================
# 定数
# ============================================================

_JQUANTS_BASE_URL = "https://api.jquants.com/v2"
_JQUANTS_LIST_PATH = "/td/list"
_JQUANTS_FILES_PATH = "/td/files"

# APIキー環境変数名 (値は絶対ログ出力しない)
_API_KEY_ENV = "JQUANTS_API_KEY"

# pagination リトライ間隔
_PAGE_SLEEP_SEC = 0.3

# レスポンスのルートキー (V2 API = "data")
_DATA_KEY = "data"
_PAGINATION_KEY = "pagination_key"

JST = timezone(timedelta(hours=9))


# ============================================================
# CommonDisclosure — J-Quants 独自フィールドを保持する拡張型
# ============================================================

@dataclass
class JQuantsDisclosure:
    """
    J-Quants /v2/td/list の1件分を DisclosureItem 互換形式に変換した構造体。

    shadow_runner が YANOSHIN/HTML 結果と比較する際の共通インターフェース。
    DB保存しない。Discord通知しない。本番フローに流さない。
    """
    # ── DisclosureItem 互換フィールド ──────────────────────
    disclosure_id: str       # DiscNo (14桁) をそのまま使用
    ticker: str              # Code の末尾0除去版 (4桁)
    company_name: str        # Name
    title: str               # Title
    doc_url: str             # DiscNo から生成する TDnet URL (参照用)
    published_at: str        # DiscDate + " " + DiscTime → "YYYY-MM-DD HH:MM"
    xbrl_url: Optional[str]  # "x" in Docs のとき /td/files で取得可能 (lazy)
    disclosure_type: str     # DisclosureType or ""

    # ── J-Quants 固有フィールド ────────────────────────────
    disc_no: str             # DiscNo (14桁) — 重複判定キー第一候補
    disc_date: str           # DiscDate (YYYY-MM-DD)
    disc_time: str           # DiscTime (HH:MM)
    disc_items: list[str]    # DiscItems コードリスト
    docs: list[str]          # 取得可能ファイル種別 ["g", "s", "x"]
    rev_no: str              # RevNo ("1"以外=訂正)
    disc_status: Optional[str]  # DiscStatus (null等)

    # ── 重複判定キー ───────────────────────────────────────
    dedup_key_primary: str   # "1401" + disc_no (TDnet FileID形式)
    dedup_key_secondary: str # sha256(disc_date + "|" + ticker + "|" + normalized_title)[:32]

    def to_disclosure_item(self) -> DisclosureItem:
        """既存 DisclosureItem 形式に変換（Shadow Run内部での比較用）"""
        return DisclosureItem(
            disclosure_id=self.disclosure_id,
            ticker=self.ticker,
            company_name=self.company_name,
            title=self.title,
            doc_url=self.doc_url,
            published_at=self.published_at,
            xbrl_url=self.xbrl_url,
            disclosure_type=self.disclosure_type,
        )


# ============================================================
# 内部ユーティリティ
# ============================================================

def _get_api_key() -> str:
    """環境変数から APIキーを取得する。値は一切ログに出力しない。"""
    load_project_env()
    key = os.environ.get(_API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"[JQUANTS_ADAPTER] {_API_KEY_ENV} が未設定です。.env を確認してください。"
        )
    # 値を返すが、呼び出し元も絶対にログに出力しないこと
    return key


def _normalize_title_for_dedup(title: str) -> str:
    """重複判定用タイトル正規化（大文字小文字・全半角・スペース統一）"""
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _make_dedup_key_secondary(disc_date: str, ticker: str, title: str) -> str:
    """
    第二候補重複判定キー: sha256(disc_date + "|" + ticker + "|" + normalized_title)[:32]
    """
    normalized = _normalize_title_for_dedup(title)
    combined = f"{disc_date}|{ticker}|{normalized}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]


def _make_doc_url_from_disc_no(disc_no: str) -> str:
    """
    DiscNo から TDnet 標準 PDF URL を組み立てる (参照用)。
    J-Quants の署名付きURLは有効期限があるため、
    ここでは TDnet 標準形式の URL を生成して doc_url フィールドに入れる。

    TDnet FileID = "1401" + DiscNo
    URL = https://www.release.tdnet.info/inbs/{file_id}.pdf
    """
    file_id = "1401" + disc_no
    return f"https://www.release.tdnet.info/inbs/{file_id}.pdf"


def _strip_trailing_zero(code: str) -> str:
    """
    5桁銘柄コード末尾の "0" を除去して4桁化。
    例: "73880" → "7388"、"365A0" → "365A"
    既存 src/common_ticker.strip_tdnet_trailing_zero() と同ロジック。
    """
    if not code:
        return code
    if len(code) == 5 and code.endswith("0"):
        return code[:-1]
    return code


def _parse_disc_datetime(disc_date: str, disc_time: str) -> str:
    """
    "2026-06-30" + "15:30" → "2026-06-30 15:30"
    published_at フィールドとして使用。
    """
    if disc_date and disc_time:
        return f"{disc_date} {disc_time}"
    return disc_date or ""


# ============================================================
# J-Quants APIクライアント
# ============================================================

def _build_headers() -> dict[str, str]:
    """リクエストヘッダー生成。APIキー値は返却するが呼び出し元でログに出さないこと。"""
    return {"x-api-key": _get_api_key()}


@with_retry(max_tries=3, status_forcelist=(429, 500, 502, 503, 504), backoff_factor=1.0)
def fetch_tdnet_list_raw(
    date_str: str,
    *,
    timeout_sec: float = 30.0,
    max_pages: int = 50,
    _session: Optional[requests.Session] = None,
) -> list[dict]:
    """
    GET /v2/td/list?date=YYYYMMDD を pagination_key で全件取得する。

    Args:
        date_str: 取得対象日 (YYYYMMDD形式)
        timeout_sec: HTTPタイムアウト秒
        max_pages: 無限ループ防止の最大ページ数
        _session: テスト用モック注入 (None=実HTTP)

    Returns:
        rawレスポンスのリスト (全ページ合算)

    Raises:
        requests.HTTPError: 4xx/5xx
        RuntimeError: 環境変数未設定 / ページ上限超過

    Security: APIキー・トークンは一切ログに出力しない。
    """
    headers = _build_headers()
    params: dict = {"date": date_str}
    client = _session or requests
    all_items: list[dict] = []
    page = 0

    logger.info(
        f"[JQUANTS_ADAPTER] fetch_start date={date_str!r} "
        f"url={_JQUANTS_BASE_URL}{_JQUANTS_LIST_PATH}"
    )

    while True:
        page += 1
        if page > max_pages:
            raise RuntimeError(
                f"[JQUANTS_ADAPTER] pagination page上限超過: "
                f"max_pages={max_pages} date={date_str}"
            )

        resp = client.get(
            f"{_JQUANTS_BASE_URL}{_JQUANTS_LIST_PATH}",
            headers=headers,
            params=params,
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        data: dict = resp.json()

        items: list[dict] = data.get(_DATA_KEY, [])
        all_items.extend(items)

        next_key = data.get(_PAGINATION_KEY)

        logger.info(
            f"[JQUANTS_SHADOW_PAGE] date={date_str!r} "
            f"page={page} this_page={len(items)} "
            f"total_so_far={len(all_items)} "
            f"has_next={'true' if next_key else 'false'}"
        )

        if not next_key:
            break

        params = {"date": date_str, _PAGINATION_KEY: next_key}
        time.sleep(_PAGE_SLEEP_SEC)

    logger.info(
        f"[JQUANTS_ADAPTER] fetch_done date={date_str!r} "
        f"total_pages={page} total_items={len(all_items)}"
    )
    return all_items


# ============================================================
# Raw → JQuantsDisclosure 変換
# ============================================================

def _convert_raw_item(raw: dict) -> Optional[JQuantsDisclosure]:
    """
    /v2/td/list の1件分 raw dict → JQuantsDisclosure に変換する。

    必須フィールドが欠損している場合は None を返す（スキップ）。
    認証情報・APIキーを一切含まない。
    """
    disc_no: str = raw.get("DiscNo", "")
    code: str = raw.get("Code", "")
    name: str = raw.get("Name", "")
    disc_date: str = raw.get("DiscDate", "")
    disc_time: str = raw.get("DiscTime", "00:00")
    title: str = raw.get("Title", "")
    disc_status = raw.get("DiscStatus")
    rev_no: str = str(raw.get("RevNo", "1"))
    disc_items: list[str] = raw.get("DiscItems", []) or []
    docs: list[str] = raw.get("Docs", []) or []

    # 必須フィールド確認
    if not disc_no or not code or not title:
        return None

    ticker = _strip_trailing_zero(code)
    published_at = _parse_disc_datetime(disc_date, disc_time)
    doc_url = _make_doc_url_from_disc_no(disc_no)

    # XBRL: "x" が Docs に含まれていれば取得可能 (lazy — 実際の URL は /td/files で取得)
    xbrl_url = None  # Shadow Run では on-demand 取得しない

    # 分類 (DiscItems 優先 + タイトル FB)
    disclosure_type = classify_disclosure_jquants(disc_items, title) or ""

    # 重複判定キー
    dedup_key_primary = "1401" + disc_no  # TDnet FileID形式
    dedup_key_secondary = _make_dedup_key_secondary(disc_date, ticker, title)

    return JQuantsDisclosure(
        disclosure_id=disc_no,
        ticker=ticker,
        company_name=name,
        title=title,
        doc_url=doc_url,
        published_at=published_at,
        xbrl_url=xbrl_url,
        disclosure_type=disclosure_type,
        disc_no=disc_no,
        disc_date=disc_date,
        disc_time=disc_time,
        disc_items=disc_items,
        docs=docs,
        rev_no=rev_no,
        disc_status=disc_status,
        dedup_key_primary=dedup_key_primary,
        dedup_key_secondary=dedup_key_secondary,
    )


# ============================================================
# メインエクスポート
# ============================================================

def fetch_jquants_disclosures(
    date_str: str,
    *,
    timeout_sec: float = 30.0,
    max_pages: int = 50,
    _session: Optional[requests.Session] = None,
) -> list[JQuantsDisclosure]:
    """
    指定日の J-Quants TDnet 開示一覧を全件取得して変換する。

    Shadow Run専用エントリーポイント。
    - DB更新なし
    - Discord通知なし
    - 本番フローへの接続なし

    Args:
        date_str: 取得日 (YYYYMMDD 形式)
        timeout_sec: HTTPタイムアウト秒
        max_pages: pagination ループ上限
        _session: テスト用モック注入

    Returns:
        JQuantsDisclosure のリスト
    """
    raw_items = fetch_tdnet_list_raw(
        date_str,
        timeout_sec=timeout_sec,
        max_pages=max_pages,
        _session=_session,
    )

    results: list[JQuantsDisclosure] = []
    skip_count = 0
    for raw in raw_items:
        converted = _convert_raw_item(raw)
        if converted is None:
            skip_count += 1
            continue
        results.append(converted)

    if skip_count > 0:
        logger.warning(
            f"[JQUANTS_ADAPTER] {skip_count}件スキップ "
            f"(disc_no/code/title欠損) date={date_str!r}"
        )

    return results


@with_retry(max_tries=3, status_forcelist=(429, 500, 502, 503, 504), backoff_factor=1.0)
def get_file_url(
    disc_no: str,
    file_type: str,  # "g"=全文PDF, "s"=サマリPDF, "x"=XBRL
    *,
    timeout_sec: float = 15.0,
    _session: Optional[requests.Session] = None,
) -> Optional[dict[str, str]]:
    """
    /v2/td/files からファイルURL(署名付き)を取得する。

    Shadow Run でファイル存在確認をする場合のみ使用する。
    取得したURLは有効期限900秒のため、キャッシュしない。

    Returns:
        {"pdf": url, "summaryPdf": url, "xbrl": url} or None (失敗時)
    """
    headers = _build_headers()
    client = _session or requests

    try:
        resp = client.get(
            f"{_JQUANTS_BASE_URL}{_JQUANTS_FILES_PATH}",
            headers=headers,
            params={"discNo": disc_no, "type": file_type},
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        files: dict = data.get("files", {})
        return files
    except Exception as e:
        logger.warning(
            f"[JQUANTS_ADAPTER] get_file_url failed: "
            f"disc_no={disc_no!r} type={file_type!r} err={e}"
        )
        return None
