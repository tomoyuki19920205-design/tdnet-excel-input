"""shadow_runner.py — J-Quants TDnet Shadow Run 実行エンジン

目的:
  既存 YANOSHIN/HTML 取得結果と J-Quants 取得結果を並走比較し、
  差分をログのみで記録する。

禁止事項:
  - DB INSERT / UPDATE / UPSERT / DELETE
  - SQLite更新
  - Discord通知
  - Realtime/Nightly手動実行
  - scheduler起動
  - 既存 fetcher の本番置き換え
  - .env変更
  - git操作
  - Vercel deploy
  - guard.py変更
  - APIキー・token・認証ヘッダー・.env値の出力

使い方 (read-only 手動確認のみ):
  from src.jquants.shadow_runner import run_shadow_comparison
  result = run_shadow_comparison("20260630")
"""
from __future__ import annotations

import hashlib
import logging
import time
import unicodedata
import re
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Optional

from src.jquants.adapter import JQuantsDisclosure, fetch_jquants_disclosures
from src.models import DisclosureItem

logger = logging.getLogger("jquants.shadow")


# ============================================================
# Shadow Run ログタグ定数
# ============================================================
# すべてのログは以下のタグで grep 可能

LOG_FETCH_START   = "[JQUANTS_SHADOW_FETCH_START]"
LOG_FETCH_DONE    = "[JQUANTS_SHADOW_FETCH_DONE]"
LOG_DIFF          = "[JQUANTS_SHADOW_DIFF]"
LOG_MISSING       = "[JQUANTS_SHADOW_MISSING_IN_LEGACY]"
LOG_EXTRA         = "[JQUANTS_SHADOW_EXTRA_IN_JQUANTS]"
LOG_DUPLICATE_KEY = "[JQUANTS_SHADOW_DUPLICATE_KEY]"
LOG_FILE_AVAIL    = "[JQUANTS_SHADOW_FILE_AVAILABLE]"
LOG_PAGE          = "[JQUANTS_SHADOW_PAGE]"  # adapter側で出力


# ============================================================
# Shadow Run 結果
# ============================================================

@dataclass
class ShadowDiffResult:
    """Shadow Run 比較結果サマリ"""
    date_str: str
    jquants_total: int = 0          # J-Quants 全取得件数
    jquants_filtered: int = 0       # J-Quants フィルタ通過件数
    legacy_total: int = 0           # 既存 (YANOSHIN/HTML) 全取得件数
    legacy_filtered: int = 0        # 既存フィルタ通過件数

    # DiscNo / FileID ベース比較
    missing_in_legacy: list[str] = field(default_factory=list)   # J-Quantsにあって既存にない (TDnet FileID)
    extra_in_jquants: list[str] = field(default_factory=list)    # 既存にあってJ-Quantsにない (doc_url)
    matched_count: int = 0

    # secondary key 衝突
    dedup_secondary_collisions: int = 0

    # ファイル種別確認
    pdf_available_count: int = 0    # "g" (全文PDF) を持つ件数
    xbrl_available_count: int = 0   # "x" (XBRL) を持つ件数
    summary_pdf_available_count: int = 0  # "s" (サマリPDF) を持つ件数

    # エラー
    fetch_error: Optional[str] = None

    @property
    def truncation_gap(self) -> int:
        """J-Quants全件 - 既存全件 (正の値 = 既存の取りこぼし推計)"""
        return self.jquants_total - self.legacy_total


# ============================================================
# 既存 DisclosureItem の重複判定キー生成
# ============================================================

def _normalize_title_for_dedup(title: str) -> str:
    """タイトル正規化 (adapter と同ロジック)"""
    s = title.replace("\n", "").replace("\r", "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _extract_file_id_from_doc_url(doc_url: str) -> Optional[str]:
    """
    既存の doc_url から TDnet FileID を抽出する。

    対応パターン:
    - https://www.release.tdnet.info/inbs/140120260617572986.pdf
    - https://webapi.yanoshin.jp/rd.php?https://...inbs/140120260617572986.pdf

    Returns:
        "140120260617572986" (18桁) or None
    """
    m = re.search(r"/(\d{14,18})\.pdf", doc_url)
    if m:
        return m.group(1)
    return None


def _file_id_to_disc_no(file_id: str) -> Optional[str]:
    """
    TDnet FileID (18桁: 1401 + DiscNo14桁) → DiscNo (14桁)

    検証済み変換ルール: FileID = "1401" + DiscNo
    """
    if len(file_id) == 18 and file_id.startswith("1401"):
        return file_id[4:]
    # 14桁の場合は既に DiscNo
    if len(file_id) == 14:
        return file_id
    return None


def _make_secondary_key_from_legacy(item: DisclosureItem) -> str:
    """
    既存 DisclosureItem から secondary 重複判定キーを生成。
    adapter._make_dedup_key_secondary() と同ロジック。
    """
    # published_at から日付部分を抽出 "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"
    disc_date = item.published_at[:10] if item.published_at else ""
    normalized = _normalize_title_for_dedup(item.title)
    combined = f"{disc_date}|{item.ticker}|{normalized}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]


# ============================================================
# Shadow Run 本体
# ============================================================

def run_shadow_comparison(
    date_str: str,
    legacy_items: Optional[list[DisclosureItem]] = None,
    *,
    log_missing_samples: int = 20,
    timeout_sec: float = 30.0,
    _session=None,
) -> ShadowDiffResult:
    """
    J-Quants取得結果と既存(YANOSHIN/HTML)取得結果を比較する Shadow Run。

    Args:
        date_str:
            取得日 (YYYYMMDD形式)
        legacy_items:
            既存 fetcher から取得済みの DisclosureItem リスト。
            None の場合は既存取得なしとして J-Quants 側の統計のみ記録する。
        log_missing_samples:
            [JQUANTS_SHADOW_MISSING_IN_LEGACY] で出力するサンプル件数上限
        timeout_sec:
            J-Quants API タイムアウト秒
        _session:
            テスト用モック注入

    Returns:
        ShadowDiffResult — DB保存しない。ログ出力のみ。

    Security:
        APIキー・認証情報は一切ログに出力しない。
    """
    result = ShadowDiffResult(date_str=date_str)

    logger.info(
        f"{LOG_FETCH_START} "
        f"date={date_str!r} "
        f"legacy_items={'N/A' if legacy_items is None else len(legacy_items)}"
    )

    # ── J-Quants 全件取得 ─────────────────────────────────
    jq_items: list[JQuantsDisclosure] = []
    try:
        jq_items = fetch_jquants_disclosures(
            date_str,
            timeout_sec=timeout_sec,
            _session=_session,
        )
    except Exception as e:
        result.fetch_error = str(e)
        logger.error(
            f"{LOG_FETCH_DONE} "
            f"date={date_str!r} "
            f"status=error err={e}"
        )
        return result

    result.jquants_total = len(jq_items)

    # ファイル種別集計
    result.pdf_available_count = sum(1 for i in jq_items if "g" in i.docs)
    result.xbrl_available_count = sum(1 for i in jq_items if "x" in i.docs)
    result.summary_pdf_available_count = sum(1 for i in jq_items if "s" in i.docs)

    # フィルタ通過件数 (disclosure_type が空でない = 対象種別)
    jq_filtered = [i for i in jq_items if i.disclosure_type]
    result.jquants_filtered = len(jq_filtered)

    logger.info(
        f"{LOG_FETCH_DONE} "
        f"date={date_str!r} "
        f"jq_total={result.jquants_total} "
        f"jq_filtered={result.jquants_filtered} "
        f"pdf_available={result.pdf_available_count} "
        f"xbrl_available={result.xbrl_available_count} "
        f"summary_pdf_available={result.summary_pdf_available_count}"
    )

    # ファイル種別ログ
    logger.info(
        f"{LOG_FILE_AVAIL} "
        f"date={date_str!r} "
        f"pdf={result.pdf_available_count} "
        f"xbrl={result.xbrl_available_count} "
        f"summary_pdf={result.summary_pdf_available_count} "
        f"total={result.jquants_total}"
    )

    # ── 既存なし → J-Quants 統計のみ ─────────────────────
    if legacy_items is None:
        logger.info(
            f"{LOG_DIFF} "
            f"date={date_str!r} "
            f"legacy=N/A "
            f"jq_total={result.jquants_total} "
            f"jq_filtered={result.jquants_filtered} "
            f"truncation_gap=N/A "
            f"note=legacy_items_not_provided"
        )
        return result

    # ── 比較処理 ─────────────────────────────────────────
    result.legacy_total = len(legacy_items)
    result.legacy_filtered = sum(1 for i in legacy_items if i.disclosure_type)

    # primary key: DiscNo / TDnet FileID ベース
    # J-Quants 側: disc_no → set
    jq_disc_no_set: set[str] = {i.disc_no for i in jq_items}
    # J-Quants 側: dedup_key_primary → set
    jq_file_id_set: set[str] = {i.dedup_key_primary for i in jq_items}

    # 既存側: doc_url → FileID → DiscNo に変換
    legacy_file_id_to_item: dict[str, DisclosureItem] = {}
    for item in legacy_items:
        fid = _extract_file_id_from_doc_url(item.doc_url)
        if fid:
            legacy_file_id_to_item[fid] = item

    legacy_file_id_set = set(legacy_file_id_to_item.keys())

    # secondary key: (disc_date + ticker + normalized_title) hash
    # J-Quants 側
    jq_secondary_set: set[str] = {i.dedup_key_secondary for i in jq_items}
    # secondary key 衝突検知
    jq_secondary_counts: dict[str, int] = {}
    for i in jq_items:
        jq_secondary_counts[i.dedup_key_secondary] = (
            jq_secondary_counts.get(i.dedup_key_secondary, 0) + 1
        )
    result.dedup_secondary_collisions = sum(
        1 for cnt in jq_secondary_counts.values() if cnt > 1
    )

    # ── 差分計算 (FileID ベース) ─────────────────────────
    # J-Quantsにあって既存にない (primary key)
    missing_in_legacy_fids = jq_file_id_set - legacy_file_id_set
    result.missing_in_legacy = sorted(missing_in_legacy_fids)

    # 既存にあってJ-Quantsにない (primary key)
    extra_in_jq_fids = legacy_file_id_set - jq_file_id_set
    result.extra_in_jquants = sorted(extra_in_jq_fids)

    # マッチ数
    result.matched_count = len(jq_file_id_set & legacy_file_id_set)

    # ── サマリログ ───────────────────────────────────────
    logger.info(
        f"{LOG_DIFF} "
        f"date={date_str!r} "
        f"jq_total={result.jquants_total} "
        f"jq_filtered={result.jquants_filtered} "
        f"legacy_total={result.legacy_total} "
        f"legacy_filtered={result.legacy_filtered} "
        f"truncation_gap={result.truncation_gap} "
        f"matched={result.matched_count} "
        f"missing_in_legacy={len(result.missing_in_legacy)} "
        f"extra_in_jquants={len(result.extra_in_jquants)} "
        f"secondary_collisions={result.dedup_secondary_collisions}"
    )

    # ── 欠落サンプルログ ─────────────────────────────────
    # J-Quantsにあって既存にない → フィルタ通過分のみをサンプル出力
    jq_file_id_to_item: dict[str, JQuantsDisclosure] = {
        i.dedup_key_primary: i for i in jq_items
    }
    missing_filtered_samples = []
    for fid in sorted(result.missing_in_legacy):
        item = jq_file_id_to_item.get(fid)
        if item and item.disclosure_type:
            missing_filtered_samples.append(item)
        if len(missing_filtered_samples) >= log_missing_samples:
            break

    for item in missing_filtered_samples:
        logger.info(
            f"{LOG_MISSING} "
            f"date={date_str!r} "
            f"disc_no={item.disc_no!r} "
            f"ticker={item.ticker!r} "
            f"name={item.company_name[:20]!r} "
            f"title={item.title[:50]!r} "
            f"disclosure_type={item.disclosure_type!r} "
            f"disc_items={item.disc_items!r} "
            f"docs={item.docs!r}"
        )

    # secondary key 衝突ログ
    if result.dedup_secondary_collisions > 0:
        for key, cnt in list(jq_secondary_counts.items())[:5]:
            if cnt > 1:
                logger.warning(
                    f"{LOG_DUPLICATE_KEY} "
                    f"date={date_str!r} "
                    f"secondary_key={key!r} "
                    f"count={cnt} "
                    f"note=secondary_key_collision"
                )

    return result


# ============================================================
# CLI エントリ (read-only ローカル確認用)
# ============================================================

def main_shadow_cli(date_str: str) -> None:
    """
    コマンドラインからの read-only Shadow Run 実行。
    YANOSHIN/HTML の結果なしで J-Quants 側の統計のみを出力する。

    使い方:
      python -c "from src.jquants.shadow_runner import main_shadow_cli; main_shadow_cli('20260630')"
    """
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = run_shadow_comparison(date_str, legacy_items=None)
    print("\n=== Shadow Run Result ===")
    print(f"  date           : {result.date_str}")
    print(f"  jq_total       : {result.jquants_total}")
    print(f"  jq_filtered    : {result.jquants_filtered}")
    print(f"  pdf_available  : {result.pdf_available_count}")
    print(f"  xbrl_available : {result.xbrl_available_count}")
    print(f"  fetch_error    : {result.fetch_error}")
