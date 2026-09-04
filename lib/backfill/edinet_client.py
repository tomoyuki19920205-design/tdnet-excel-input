"""lib/backfill/edinet_client.py — EDINET API v2 クライアント

API key 未設定時は安全に skip し、mock/cache ベースで動作可能。
`EDINET_API_KEY` 環境変数が設定されていれば live API に切り替わる。
.env ファイルからも自動的に読み込む。
"""
from __future__ import annotations
from lib.runtime_paths import runtime_path

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

# .env から環境変数を読み込む
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("backfill.edinet")

# ============================================================
# Data Classes
# ============================================================


@dataclass
class EdinetDocument:
    """EDINET 書類一覧 API のドキュメント1件。"""
    doc_id: str
    issuer_name: str = ""
    ticker: str = ""
    document_date: str = ""
    title: str = ""
    doc_type_code: str = ""
    doc_description: str = ""
    xbrl_available: bool = False
    zip_available: bool = False
    edinetCode: str = ""
    secCode: str = ""


@dataclass
class EdinetResolveResult:
    """EDINET resolve (書類特定) の結果。"""
    attempted: bool = False
    succeeded: bool = False
    skipped: bool = False
    skipped_reason: str = ""
    doc_id: str = ""
    match_score: float = 0.0
    match_basis: str = ""
    candidate_count: int = 0
    # debug: top candidates
    top1_doc_id: str = ""
    top1_score: float = 0.0
    top2_score: float = 0.0
    selected_reason: str = ""  # "above_threshold" / "below_threshold" / "no_candidates"
    # 詳細 fail_reason
    fail_reason: str = ""  # no_candidates_in_window / candidates_found_but_no_ticker_match / score_below_threshold / margin_too_small
    # 探索窓統計
    window_stats: dict = field(default_factory=dict)  # {"window_0d": {"candidates": N, "ticker_matches": M}, ...}
    ticker_match_count: int = 0  # 全窓で ticker が一致した候補数


@dataclass
class EdinetDownloadResult:
    """EDINET XBRL ZIP ダウンロードの結果。"""
    attempted: bool = False
    succeeded: bool = False
    skipped: bool = False
    skipped_reason: str = ""
    doc_id: str = ""
    cache_path: str = ""
    cache_hit: bool = False
    failure_reason: str = ""


# ============================================================
# EDINET Client
# ============================================================

_EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"
_USER_AGENT = "TDnetExcelInput/1.0"


class EdinetClient:
    """EDINET API v2 クライアント。

    - `EDINET_API_KEY` 環境変数で API key を読み込む
    - key 未設定時は safe skip (エラーで止めない)
    - rate limit: default 0.5s/req
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cache_dir: str | None = None,
        rate_limit: float = 0.5,
    ) -> None:
        self._api_key = api_key or os.environ.get("EDINET_API_KEY", "")
        self._cache_dir = cache_dir or os.path.join(
            os.environ.get("EDINET_CACHE_DIR", "data/edinet_cache")
        )
        self._cache_dir = str(runtime_path(self._cache_dir))
        self._rate_limit = rate_limit
        self._last_request_time: float = 0.0

        if not self._api_key:
            logger.warning(
                "[edinet] EDINET_API_KEY is not set. "
                "Live API calls will be skipped."
            )

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    @property
    def cache_dir(self) -> str:
        return self._cache_dir

    def _rate_limit_wait(self) -> None:
        """Rate limit enforcement."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_request_time = time.monotonic()

    # --------------------------------------------------------
    # 書類一覧 API
    # --------------------------------------------------------
    def search_documents(
        self,
        date: str,
        *,
        issuer: str | None = None,
        ticker: str | None = None,
    ) -> list[EdinetDocument]:
        """EDINET 書類一覧 API で指定日の書類を検索。

        Args:
            date: YYYY-MM-DD
            issuer: EDINET コード (optional)
            ticker: 4桁/5桁銘柄コード (optional, secCode フィルタ)

        Returns:
            EdinetDocument リスト (空 = 結果なし or skip)
        """
        if not self._api_key:
            logger.debug("[edinet] search_documents skipped: no API key")
            return []

        self._rate_limit_wait()

        url = f"{_EDINET_BASE}/documents.json"
        params = {
            "date": date,
            "type": 2,  # 2=メタデータ + 結果
            "Subscription-Key": self._api_key,
        }

        try:
            resp = requests.get(
                url, params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[edinet] search_documents error: {e}")
            return []

        # EDINET API returns HTTP 200 but body may contain statusCode: 401
        if "statusCode" in data and data["statusCode"] != 200:
            body_status = data.get("statusCode")
            body_msg = data.get("message", "")
            logger.warning(
                f"[edinet] search_documents: API body error "
                f"statusCode={body_status} message={body_msg}"
            )
            return []

        results_list = data.get("results", [])
        docs: list[EdinetDocument] = []

        for r in results_list:
            sec_code = r.get("secCode", "") or ""
            edinet_code = r.get("edinetCode", "") or ""
            doc_ticker = ""
            if sec_code and len(sec_code) >= 4:
                doc_ticker = sec_code[:4] if len(sec_code) == 5 and sec_code.endswith("0") else sec_code

            # ticker フィルタ
            if ticker and doc_ticker and doc_ticker != ticker:
                continue

            doc = EdinetDocument(
                doc_id=r.get("docID", ""),
                issuer_name=r.get("filerName", ""),
                ticker=doc_ticker,
                document_date=r.get("submitDateTime", "")[:10] if r.get("submitDateTime") else "",
                title=r.get("docDescription", ""),
                doc_type_code=r.get("docTypeCode", ""),
                doc_description=r.get("docDescription", ""),
                xbrl_available=bool(r.get("xbrlFlag", "0") == "1"),
                zip_available=bool(r.get("docID")),
                edinetCode=edinet_code,
                secCode=sec_code,
            )
            docs.append(doc)

        logger.info(
            f"[edinet] search_documents: date={date} "
            f"ticker={ticker} results={len(docs)}"
        )
        return docs

    # --------------------------------------------------------
    # XBRL ZIP ダウンロード
    # --------------------------------------------------------
    def download_xbrl_zip(
        self,
        doc_id: str,
        cache_dir: str | None = None,
    ) -> EdinetDownloadResult:
        """EDINET から XBRL ZIP をダウンロードして cache に保存。

        Args:
            doc_id: EDINET 書類ID
            cache_dir: 保存先 (default: self._cache_dir)

        Returns:
            EdinetDownloadResult
        """
        dest = cache_dir or self._cache_dir

        # cache check first
        from .edinet_xbrl_cache import EdinetXbrlCache
        cache = EdinetXbrlCache(dest)
        cached_path = cache.load_cached_xbrl_zip(doc_id)
        if cached_path:
            return EdinetDownloadResult(
                attempted=False, succeeded=True, skipped=False,
                doc_id=doc_id, cache_path=str(cached_path),
                cache_hit=True,
            )

        if not self._api_key:
            return EdinetDownloadResult(
                attempted=False, succeeded=False, skipped=True,
                skipped_reason="missing_api_key",
                doc_id=doc_id,
            )

        self._rate_limit_wait()

        url = f"{_EDINET_BASE}/documents/{doc_id}"
        params = {
            "type": 1,  # 1=書類本体 ZIP
            "Subscription-Key": self._api_key,
        }

        try:
            resp = requests.get(
                url, params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=30,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")

            if "application/octet-stream" not in content_type and "zip" not in content_type:
                return EdinetDownloadResult(
                    attempted=True, succeeded=False,
                    doc_id=doc_id,
                    failure_reason=f"unexpected_content_type:{content_type}",
                )

            # Save to cache
            saved_path = cache.save_xbrl_zip(doc_id, resp.content)
            logger.info(
                f"[edinet] download OK: doc_id={doc_id} "
                f"size={len(resp.content):,d} path={saved_path}"
            )
            return EdinetDownloadResult(
                attempted=True, succeeded=True,
                doc_id=doc_id, cache_path=str(saved_path),
                cache_hit=False,
            )

        except Exception as e:
            logger.warning(f"[edinet] download error: doc_id={doc_id} {e}")
            return EdinetDownloadResult(
                attempted=True, succeeded=False,
                doc_id=doc_id,
                failure_reason=str(e),
            )

    # --------------------------------------------------------
    # Resolve: TDnet filing → EDINET document
    # --------------------------------------------------------
    def resolve_document(
        self,
        ticker: str,
        disclosure_date: str,
        title: str,
        doc_type: str,
        *,
        period: str | None = None,
        quarter: str | None = None,
    ) -> EdinetResolveResult:
        """TDnet filing 情報に最もマッチする EDINET 書類を特定。

        段階的に探索窓を拡大: 0日 → ±3日 → ±7日 → ±14日。
        ticker 一致候補が見つかったら拡大停止。
        API key 未設定時は skip。
        """
        if not self._api_key:
            return EdinetResolveResult(
                attempted=False, skipped=True,
                skipped_reason="missing_api_key",
            )

        from .edinet_resolver import (
            pick_best_edinet_candidate,
            _normalize_ticker,
        )
        from datetime import datetime, timedelta

        base_date = datetime.strptime(disclosure_date, "%Y-%m-%d")
        norm_ticker = _normalize_ticker(ticker)

        # 段階的探索窓: (label, offsets)
        _WINDOWS = [
            ("0d",  range(0, 1)),       # 当日のみ
            ("3d",  range(-3, 4)),       # ±3日
            ("7d",  range(-7, 8)),       # ±7日
            ("14d", range(-14, 15)),     # ±14日
        ]

        all_candidates: list[EdinetDocument] = []
        seen_dates: set[str] = set()
        window_stats: dict = {}
        ticker_match_found = False

        for window_label, offsets in _WINDOWS:
            window_new = 0
            for offset in offsets:
                d = (base_date + timedelta(days=offset)).strftime("%Y-%m-%d")
                if d in seen_dates:
                    continue
                seen_dates.add(d)
                docs = self.search_documents(d, ticker=ticker)
                all_candidates.extend(docs)
                window_new += len(docs)

            # この窓までの ticker 一致数をカウント
            ticker_matches = sum(
                1 for c in all_candidates
                if _normalize_ticker(c.ticker or "") == norm_ticker
                or _normalize_ticker(c.secCode or "") == norm_ticker
            )

            window_stats[f"window_{window_label}"] = {
                "candidates": len(all_candidates),
                "new_in_window": window_new,
                "ticker_matches": ticker_matches,
            }

            logger.info(
                f"[edinet] resolve window={window_label}: "
                f"total_candidates={len(all_candidates)} "
                f"new={window_new} ticker_matches={ticker_matches}"
            )

            # ticker 一致候補が見つかったら拡大停止
            if ticker_matches > 0 and not ticker_match_found:
                ticker_match_found = True
                logger.info(
                    f"[edinet] ticker match found at window={window_label}, "
                    f"stopping expansion"
                )
                break

        total_ticker_matches = sum(
            1 for c in all_candidates
            if _normalize_ticker(c.ticker or "") == norm_ticker
            or _normalize_ticker(c.secCode or "") == norm_ticker
        )

        if not all_candidates:
            return EdinetResolveResult(
                attempted=True, succeeded=False,
                candidate_count=0,
                fail_reason="no_candidates_in_window",
                window_stats=window_stats,
                ticker_match_count=0,
            )

        best = pick_best_edinet_candidate(
            ticker=ticker,
            disclosure_date=disclosure_date,
            title=title,
            doc_type=doc_type,
            candidates=all_candidates,
            period=period,
            quarter=quarter,
        )

        # 詳細 fail_reason を設定
        if not best.succeeded:
            if total_ticker_matches == 0:
                best.fail_reason = "candidates_found_but_no_ticker_match"
            elif "margin" in best.selected_reason:
                best.fail_reason = "margin_too_small"
            elif "score" in best.selected_reason:
                best.fail_reason = "score_below_threshold"
            else:
                best.fail_reason = best.selected_reason

        best.window_stats = window_stats
        best.ticker_match_count = total_ticker_matches
        return best
