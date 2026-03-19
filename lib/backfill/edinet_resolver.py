"""lib/backfill/edinet_resolver.py — EDINET 書類マッチング / スコアリング

TDnet filing 情報から EDINET 書類の候補群をスコアリングし、
最もマッチする書類を特定する。
"""
from __future__ import annotations

import re
import logging
import unicodedata
from typing import Optional

logger = logging.getLogger("backfill.edinet.resolver")

# 循環 import を避けるため TYPE_CHECKING で型だけ参照
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .edinet_client import EdinetDocument, EdinetResolveResult


# ============================================================
# Constants
# ============================================================

# resolve success 条件
_MIN_SCORE = 0.50       # top1 がこれ以上必要
_MIN_MARGIN = 0.10      # top1 - top2 がこれ以上必要（誤候補防止）
_MATCH_THRESHOLD = 0.55  # 後方互換 (外部パラメータ用)

# filer_name で除外する一般語
_FILER_COMMON_WORDS = frozenset({
    "株式会社", "有限会社", "合同会社", "合名会社", "合資会社",
    "ホールディングス", "グループ", "ジャパン", "日本",
    "インターナショナル", "コーポレーション",
})

_FILER_NAME_MIN_LEN = 3  # 正規化後この文字数未満は無視


# ============================================================
# Title / Ticker normalization
# ============================================================

def _normalize_title(title: str) -> str:
    """タイトルを正規化 (比較用)。"""
    s = unicodedata.normalize("NFKC", title)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _normalize_ticker(raw: str) -> str:
    """証券コードを正規化。common_ticker.normalize_ticker に委譲。"""
    from src.common_ticker import normalize_ticker
    s = raw.strip()
    if not s or s.lower() in ("none", "null", "nan"):
        return ""
    return normalize_ticker(s)


def _normalize_filer_name(name: str) -> str:
    """企業名を正規化: 一般語除去 + 正規化。"""
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"\s+", "", s)
    # 一般語を除去
    for w in _FILER_COMMON_WORDS:
        s = s.replace(w, "")
    return s.strip()


# ============================================================
# 決算短信 doc_type_code マッピング
# ============================================================

_FS_KEYWORDS = ["決算短信", "四半期決算短信", "通期決算"]
_FORECAST_KEYWORDS = ["業績予想", "修正"]


# ============================================================
# Score candidates
# ============================================================

def score_edinet_candidate(
    *,
    ticker: str,
    disclosure_date: str,
    title: str,
    doc_type: str,
    candidate,  # EdinetDocument
    period: str | None = None,
    quarter: str | None = None,
) -> tuple[float, str]:
    """EDINET 候補1件をスコアリング。

    Returns:
        (score, basis) — score は 0.0～1.0, basis はマッチ根拠の文字列
    """
    score = 0.0
    basis_parts: list[str] = []

    # --- ticker / secCode match (ペナルティ方式) ---
    norm_ticker = _normalize_ticker(ticker)
    cand_ticker = _normalize_ticker(candidate.ticker or "")
    cand_sec = _normalize_ticker(candidate.secCode or "")

    if norm_ticker and (cand_ticker == norm_ticker or cand_sec == norm_ticker):
        score += 0.40
        basis_parts.append(f"ticker={norm_ticker}")
    elif norm_ticker and (cand_ticker or cand_sec):
        # ticker 情報はあるが不一致 → 大きなペナルティ
        score -= 0.50
        basis_parts.append(f"ticker_mismatch({cand_ticker or cand_sec}!={norm_ticker})")
    else:
        # 候補に ticker/secCode 情報なし → ペナルティ小
        basis_parts.append("ticker_unknown")

    # --- 日付 match ---
    if candidate.document_date:
        if candidate.document_date == disclosure_date:
            score += 0.20
            basis_parts.append("date_exact")
        else:
            try:
                from datetime import datetime
                d1 = datetime.strptime(disclosure_date, "%Y-%m-%d")
                d2 = datetime.strptime(candidate.document_date, "%Y-%m-%d")
                diff = abs((d1 - d2).days)
                if diff <= 1:
                    score += 0.15
                    basis_parts.append(f"date_near({diff}d)")
                elif diff <= 3:
                    score += 0.10
                    basis_parts.append(f"date_close({diff}d)")
                elif diff <= 7:
                    score += 0.05
                    basis_parts.append(f"date_week({diff}d)")
                else:
                    basis_parts.append(f"date_far({diff}d)")
            except ValueError:
                basis_parts.append("date_parse_error")

    # --- タイトル類似 ---
    norm_title = _normalize_title(title)
    norm_cand = _normalize_title(candidate.title or candidate.doc_description or "")

    if norm_title and norm_cand:
        # 決算短信 keyword マッチ
        has_fs_kw = any(kw in norm_cand for kw in _FS_KEYWORDS)
        title_has_fs = any(kw in norm_title for kw in _FS_KEYWORDS)

        if has_fs_kw and title_has_fs:
            score += 0.20
            basis_parts.append("title_fs_match")
        elif has_fs_kw or title_has_fs:
            score += 0.10
            basis_parts.append("title_fs_partial")

        # 部分一致
        if norm_title in norm_cand or norm_cand in norm_title:
            score += 0.10
            basis_parts.append("title_substring")
        elif len(norm_title) > 5 and len(norm_cand) > 5:
            common = sum(1 for c in norm_title if c in norm_cand)
            ratio = common / max(len(norm_title), 1)
            if ratio > 0.6:
                score += 0.05
                basis_parts.append(f"title_overlap({ratio:.0%})")

    # --- doc_type match ---
    if doc_type == "financial_statement" and any(
        kw in (candidate.doc_description or "") for kw in _FS_KEYWORDS
    ):
        score += 0.10
        basis_parts.append("doc_type_fs")

    # --- filer_name match (issuer_name) ---
    if candidate.issuer_name:
        norm_issuer = _normalize_filer_name(candidate.issuer_name)
        if norm_issuer and len(norm_issuer) >= _FILER_NAME_MIN_LEN:
            # タイトル内に企業名が含まれるか
            norm_title_for_filer = _normalize_title(title)
            if norm_issuer in norm_title_for_filer:
                score += 0.05
                basis_parts.append(f"filer_name_match({norm_issuer[:8]})")

    # --- XBRL available ---
    if candidate.xbrl_available:
        score += 0.05
        basis_parts.append("xbrl_avail")

    # Clamp to [0.0, 1.0]
    score = max(0.0, min(score, 1.0))

    return score, "; ".join(basis_parts)


# ============================================================
# Pick best candidate
# ============================================================

def pick_best_edinet_candidate(
    *,
    ticker: str,
    disclosure_date: str,
    title: str,
    doc_type: str,
    candidates: list,  # list[EdinetDocument]
    period: str | None = None,
    quarter: str | None = None,
    min_score: float = _MIN_SCORE,
    min_margin: float = _MIN_MARGIN,
):
    """候補群からベストを選択。

    Success 条件:
        - top1_score >= min_score
        - top1_score - top2_score >= min_margin

    Returns:
        EdinetResolveResult
    """
    from .edinet_client import EdinetResolveResult

    if not candidates:
        return EdinetResolveResult(
            attempted=True, succeeded=False,
            candidate_count=0,
        )

    scored: list[tuple[float, str, object]] = []
    for c in candidates:
        s, b = score_edinet_candidate(
            ticker=ticker,
            disclosure_date=disclosure_date,
            title=title,
            doc_type=doc_type,
            candidate=c,
            period=period,
            quarter=quarter,
        )
        scored.append((s, b, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_basis, best_doc = scored[0]
    top2_score = scored[1][0] if len(scored) > 1 else 0.0
    margin = best_score - top2_score

    # Debug log: top 3 candidates
    log_parts = [
        f"[edinet] resolve: ticker={ticker} date={disclosure_date} "
        f"candidates={len(candidates)}"
    ]
    for i, (s, b, c) in enumerate(scored[:3]):
        log_parts.append(
            f"  #{i+1} score={s:.3f} doc_id={c.doc_id} "
            f"ticker={c.ticker} sec={c.secCode} "
            f"issuer={getattr(c, 'issuer_name', '')[:20]} "
            f"basis={b}"
        )
    logger.info("\n".join(log_parts))

    # Success conditions: both must pass
    if best_score >= min_score and margin >= min_margin:
        logger.info(
            f"[edinet] resolve SUCCESS: ticker={ticker} score={best_score:.3f} "
            f"margin={margin:.3f} doc_id={best_doc.doc_id} basis={best_basis}"
        )
        return EdinetResolveResult(
            attempted=True, succeeded=True,
            doc_id=best_doc.doc_id,
            match_score=best_score,
            match_basis=best_basis,
            candidate_count=len(candidates),
            top1_doc_id=best_doc.doc_id,
            top1_score=best_score,
            top2_score=top2_score,
            selected_reason="above_threshold",
        )
    else:
        reason_parts = []
        if best_score < min_score:
            reason_parts.append(f"score={best_score:.3f}<{min_score}")
        if margin < min_margin:
            reason_parts.append(f"margin={margin:.3f}<{min_margin}")
        reason = "; ".join(reason_parts)

        logger.info(
            f"[edinet] resolve FAIL: ticker={ticker} "
            f"best_score={best_score:.3f} margin={margin:.3f} "
            f"reason={reason} basis={best_basis}"
        )
        return EdinetResolveResult(
            attempted=True, succeeded=False,
            match_score=best_score,
            match_basis=f"below_threshold({reason})",
            candidate_count=len(candidates),
            top1_doc_id=best_doc.doc_id if best_score > 0 else "",
            top1_score=best_score,
            top2_score=top2_score,
            selected_reason=f"below_threshold({reason})",
        )
