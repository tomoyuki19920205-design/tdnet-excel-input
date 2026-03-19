"""lib/backfill/listing_sources/tdnet_html.py — TDnet HTML 日付ページ provider

位置づけ: fallback / 開発検証用 / 近日期間用。
3年フルバックフィルの唯一の正本 source として使わない。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .base import FilingInfo, make_filing_id
from lib.backfill.xbrl_url_inference import infer_xbrl_url_from_pdf

logger = logging.getLogger("backfill.listing.html")

_USER_AGENT = "TDnetExcelInput/1.0"

# 決算短信分類キーワード — substring マッチ
# "決算短信" で Q/FY/訂正すべてカバー。個別追加は安全側で残す。
_FS_KEYWORDS = ["決算短信", "四半期決算短信", "通期決算", "訂正決算短信"]
_FORECAST_KEYWORDS_BASE = ["業績", "予想"]
_FORECAST_REVISION_KEYWORDS = ["修正", "変更", "上方修正", "下方修正", "差異"]

# ETF/投信除外 (fetcher.py is_instrument_excluded 互換)
_INSTRUMENT_EXCLUDE_KEYWORDS = [
    "etf", "etn", "投資信託", "ファンド", "ishares", "iシェアーズ",
    "アイシェアーズ", "上場投信", "インデックス",
]

# 除外キーワード
_EXCLUDE_KEYWORDS = ["自己株式", "人事", "訴訟", "資本業務提携", "株式分割", "新株予約権"]


def _strip_trailing_zero(code: str) -> str:
    """TDnet の5桁コードから末尾0を除去して4桁にする。common_ticker に委譲。"""
    from src.common_ticker import normalize_ticker
    return normalize_ticker(code)


def _normalize_title_simple(title: str) -> str:
    """簡易タイトル正規化 (分類判定用)。"""
    import re
    import unicodedata
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _classify_disclosure(title: str) -> str | None:
    """タイトルから doc_type を判定。fetcher.py 互換。"""
    n = _normalize_title_simple(title)

    # forecast_revision
    has_base = any(kw in n for kw in _FORECAST_KEYWORDS_BASE)
    has_revision = any(kw in n for kw in _FORECAST_REVISION_KEYWORDS)
    if has_base and has_revision:
        if "業績" not in n and "配当" in n:
            return None
        return "forecast_revision"

    # financial_statement
    if any(kw in n for kw in _FS_KEYWORDS):
        return "financial_statement"

    return None


def _is_instrument_excluded(ticker: str, title: str, company_name: str) -> bool:
    """ETF/投信除外。fetcher.py 互換。"""
    n_title = _normalize_title_simple(title)
    n_name = _normalize_title_simple(company_name)
    for kw in _INSTRUMENT_EXCLUDE_KEYWORDS:
        if kw in n_title or kw in n_name:
            return True
    return False


def _matches_filter(title: str) -> bool:
    """タイトルフィルタ。"""
    if _classify_disclosure(title) is None:
        return False
    if any(kw in title for kw in _EXCLUDE_KEYWORDS):
        return False
    return True


def _date_range(start: str, end: str):
    """YYYY-MM-DD の日付を1日ずつ yield する。"""
    d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    while d <= end_d:
        yield d.strftime("%Y%m%d"), d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


class TdnetHtmlListingProvider:
    """TDnet HTML 日付ページからの filing 一覧取得。

    位置づけ: fallback / 開発検証 / 近日期間。
    3年フルバックフィルの正本 source としては使わない。
    """

    def __init__(
        self,
        *,
        rate_limit: float = 0.5,
        max_pages_per_day: int = 20,
        listing_log_dir: str | None = None,
    ) -> None:
        self.rate_limit = rate_limit
        self.max_pages_per_day = max_pages_per_day
        self.listing_log_dir = listing_log_dir

    @property
    def name(self) -> str:
        return "tdnet_html"

    def list_filings(
        self,
        start_date: str,
        end_date: str,
        *,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> list[FilingInfo]:
        """日付ページを日付ループでスクレイピングし、filing 一覧を返す。"""
        all_filings: list[FilingInfo] = []

        for date_compact, date_iso in _date_range(start_date, end_date):
            day_filings, day_log = self._fetch_one_day(
                date_compact, date_iso,
                tickers=tickers, doc_types=doc_types,
            )
            all_filings.extend(day_filings)

            # 日次 listing ログ保存
            if self.listing_log_dir:
                self._save_listing_log(date_iso, day_log)

        logger.info(
            f"[html] listing complete: "
            f"range={start_date}~{end_date} total={len(all_filings)}"
        )
        return all_filings

    def _fetch_one_day(
        self,
        date_compact: str,
        date_iso: str,
        *,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
    ) -> tuple[list[FilingInfo], dict]:
        """1日分の filing を取得。"""
        filings: list[FilingInfo] = []
        pages_attempted = 0
        pages_succeeded = 0
        pages_failed = 0
        raw_count = 0
        filtered_out = 0
        errors: list[str] = []

        for page in range(1, self.max_pages_per_day + 1):
            page_str = str(page).zfill(3)
            url = f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{date_compact}.html"
            pages_attempted += 1

            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": _USER_AGENT},
                    timeout=15,
                )
                if resp.status_code == 404:
                    break  # ページ終端
                resp.raise_for_status()
                pages_succeeded += 1

                # TDnet は Content-Type に charset を返さず requests が
                # ISO-8859-1 を想定するため、日本語タイトルが文字化けする。
                # HTML meta charset="UTF-8" に合わせて明示的に UTF-8 で解読。
                resp.encoding = resp.apparent_encoding or "utf-8"
                soup = BeautifulSoup(resp.text, "html.parser")
                for tr in soup.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 4:
                        continue

                    pub_time = tds[0].get_text(strip=True)
                    ticker = _strip_trailing_zero(tds[1].get_text(strip=True))
                    company_name = tds[2].get_text(strip=True)
                    title = tds[3].get_text(strip=True)

                    # PDF リンク
                    link_tag = tds[3].find("a") or tr.find("a")
                    href = link_tag.get("href", "") if link_tag else ""
                    if href and not href.startswith("http"):
                        href = f"https://www.release.tdnet.info/inbs/{href}"

                    # XBRL URL 推定 (PDF URL 1401→0812 パターン)
                    inferred_xbrl = infer_xbrl_url_from_pdf(href)

                    if not ticker or not title or not href:
                        continue

                    raw_count += 1

                    # フィルタ
                    if _is_instrument_excluded(ticker, title, company_name):
                        filtered_out += 1
                        continue
                    if not _matches_filter(title):
                        filtered_out += 1
                        continue
                    if tickers and ticker not in tickers:
                        filtered_out += 1
                        continue

                    doc_type = _classify_disclosure(title) or "unknown"
                    if doc_types and doc_type not in doc_types:
                        filtered_out += 1
                        continue

                    filing_id = make_filing_id(date_iso, ticker, title, href)

                    filings.append(FilingInfo(
                        filing_id=filing_id,
                        ticker=ticker,
                        title=title,
                        disclosure_date=date_iso,
                        doc_url=href,
                        xbrl_url=inferred_xbrl,
                        doc_type=doc_type,
                        company_name=company_name,
                        published_at=f"{date_iso} {pub_time}",
                        listing_source="tdnet_html",
                        has_xbrl=False,  # 推定段階では未確認
                        xbrl_url_inferred=inferred_xbrl is not None,
                    ))

            except Exception as e:
                pages_failed += 1
                errors.append(f"page={page}: {e}")
                if page == 1:
                    logger.error(f"[html] {date_iso} page 1 failed: {e}")
                    break
                logger.warning(f"[html] {date_iso} page {page} failed: {e}")
                break

            time.sleep(self.rate_limit)

        # 分類集計
        type_counts: dict[str, int] = {}
        for f in filings:
            dt = f.doc_type or "unknown"
            type_counts[dt] = type_counts.get(dt, 0) + 1

        log_entry = {
            "date": date_iso,
            "provider": "tdnet_html",
            "pages_attempted": pages_attempted,
            "pages_succeeded": pages_succeeded,
            "pages_failed": pages_failed,
            "raw_filings": raw_count,
            "filtered_out": filtered_out,
            "kept": len(filings),
            "financial_statement_count": type_counts.get("financial_statement", 0),
            "forecast_revision_count": type_counts.get("forecast_revision", 0),
            "other_types_count": sum(v for k, v in type_counts.items() if k not in ("financial_statement", "forecast_revision")),
            "errors": errors,
        }

        if filings:
            logger.info(
                f"[html] {date_iso}: kept={len(filings)} "
                f"raw={raw_count} filtered={filtered_out} "
                f"pages={pages_succeeded}/{pages_attempted}"
            )
        else:
            logger.debug(f"[html] {date_iso}: 0 filings (raw={raw_count})")

        return filings, log_entry

    def _save_listing_log(self, date_iso: str, log_entry: dict) -> None:
        """日次 listing ログを JSON ファイルに保存。"""
        try:
            log_dir = Path(self.listing_log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{date_iso}.json"
            log_path.write_text(
                json.dumps(log_entry, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[html] listing log save failed: {date_iso}: {e}")
