#!/usr/bin/env python3
"""summary_pipeline.py — AI要約パイプライン オーケストレータ

イベントパイプライン process_documents() 完了後に呼び出され、
ジョブ管理・重複判定・優先度制御・AI要約生成・Discord通知を行う。

AI要約失敗でもイベント検知・通知基盤全体は止めない。

dry-run モード仕様:
  - OpenAI API 呼び出し: なし
  - DB 書き込み: なし (メモリDB で処理)
  - Discord 送信: なし (ログ出力のみ)
  - 副作用: なし (課金・永続状態の変更は一切発生しない)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from .common_models import DocumentMeta
from .summary_models import (
    SummaryJob,
    AISummary,
    SummaryPriority,
    SummaryType,
    JobStatus,
    CURRENT_PROMPT_VERSION,
)
from .summary_storage import (
    ensure_summary_tables,
    insert_summary_job,
    get_pending_jobs,
    update_job_status,
    get_job_retry_count,
    save_ai_summary,
    get_unnotified_summaries,
    mark_summary_notified,
)
from .summary_prioritizer import classify_priority
from .summary_text_extractor import extract_summary_input
from .summary_ai_client import call_summary_api
from .summary_notify import send_summary_discord

logger = logging.getLogger("summary_pipeline")

JST = timezone(timedelta(hours=9))

MAX_RETRIES = 2  # リトライ上限


# ============================================================
# パイプライン結果
# ============================================================
@dataclass
class SummaryPipelineResult:
    """AI要約パイプライン実行結果"""
    jobs_created: int = 0
    jobs_skipped: int = 0      # fingerprint 重複 or low 優先度スキップ
    jobs_processed: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    notifications_sent: int = 0
    errors: list[str] = field(default_factory=list)
    # V2: 決算短信要約
    earnings_generated: int = 0
    earnings_saved: int = 0
    earnings_notified: int = 0
    earnings_filtered: int = 0
    earnings_no_yoy: int = 0
    earnings_already_exists: int = 0


# ============================================================
# Phase 1: ジョブ登録
# ============================================================
def _register_jobs(
    docs: list[DocumentMeta],
    conn: sqlite3.Connection,
    event_results: list[dict] | None = None,
    skip_low: bool = True,
) -> tuple[int, int]:
    """文書リストからAI要約ジョブを登録する。

    Parameters
    ----------
    docs : list[DocumentMeta]
    conn : sqlite3.Connection
    event_results : list[dict] | None
        イベントパイプラインの検知結果。event_type / subtype / extracted_payload_json を含む
    skip_low : bool
        low 優先度をスキップするか

    Returns
    -------
    (created_count, skipped_count)
    """
    # event_results から doc_id → event 情報のマップを構築
    event_map: dict[str, dict] = {}
    if event_results:
        for er in event_results:
            doc_id = er.get("doc_id", "")
            if doc_id:
                event_map[doc_id] = er

    created = 0
    skipped = 0

    for doc in docs:
        # イベント結果から event_type / subtype を取得
        ev_info = event_map.get(doc.doc_id, {})
        event_type = ev_info.get("event_type", "")
        subtype = ev_info.get("subtype", "")
        extracted_payload = ev_info.get("extracted_payload_json", "")

        # 優先度判定
        priority = classify_priority(doc.title, event_type, subtype)

        # low スキップ
        if skip_low and priority == SummaryPriority.LOW:
            skipped += 1
            logger.info(f"[SUMMARY] skip_reason=low_priority ticker={doc.ticker} title={doc.title[:40]}")
            continue

        # fingerprint は既存イベントパイプラインのものを流用
        from .common_normalizers import compute_fingerprint
        fp = compute_fingerprint("summary", doc.doc_id, doc.ticker, doc.title)

        job = SummaryJob(
            doc_id=doc.doc_id,
            fingerprint=fp,
            ticker=doc.ticker,
            company_name=doc.company_name,
            title=doc.title,
            event_type=event_type,
            subtype=subtype,
            priority=priority,
            extracted_payload_json=extracted_payload,
        )

        action = insert_summary_job(conn, job)
        if action == "inserted":
            created += 1
        else:
            skipped += 1
            logger.info(
                f"[SUMMARY] skip_reason=duplicate_fingerprint "
                f"ticker={doc.ticker} fp={fp[:12]} title={doc.title[:40]}"
            )

    return created, skipped


# ============================================================
# Phase 2: ジョブ実行（AI要約生成）
# ============================================================
def _process_pending_jobs(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    model: str = "",
) -> tuple[int, int, int, list[str], set[str]]:
    """pending ジョブを処理する。

    Returns
    -------
    (processed, succeeded, failed, errors, saved_fingerprints)
        saved_fingerprints: DB保存成功分の fingerprint 集合
    """
    jobs = get_pending_jobs(conn, exclude_low=True)
    processed = 0
    succeeded = 0
    failed = 0
    errors: list[str] = []
    saved_fingerprints: set[str] = set()

    for job in jobs:
        processed += 1
        try:
            # リトライ上限チェック
            retry_count = get_job_retry_count(conn, job.fingerprint)
            if retry_count >= MAX_RETRIES:
                update_job_status(conn, job.fingerprint, JobStatus.FAILED,
                                  error_msg="max retries exceeded")
                failed += 1
                errors.append(f"max_retries: {job.ticker} {job.title[:30]}")
                continue

            # ステータスを running に
            update_job_status(conn, job.fingerprint, JobStatus.RUNNING)

            # テキスト入力を構築
            input_text = extract_summary_input(
                title=job.title,
                event_type=job.event_type,
                subtype=job.subtype,
                extracted_payload_json=job.extracted_payload_json,
            )

            if dry_run:
                logger.info(
                    f"[DRY-RUN] would call AI for {job.ticker} "
                    f"priority={job.priority} input_len={len(input_text)}"
                )
                print(
                    f"[DRY-RUN] AI要約対象: {job.ticker} {job.title[:40]}\n"
                    f"  priority={job.priority}\n"
                    f"  input_len={len(input_text)}\n"
                    f"  input_preview={input_text[:200]}..."
                )
                # dry-run: DB更新なし、API呼び出しなし
                succeeded += 1
                continue

            # AI API 呼び出し
            result, usage = call_summary_api(
                input_text=input_text,
                model=model,
                priority=job.priority,
                max_retries=MAX_RETRIES,
            )

            # AISummary を構築・保存
            summary = AISummary.from_api_response(
                doc_id=job.doc_id,
                fingerprint=job.fingerprint,
                ticker=job.ticker,
                company_name=job.company_name,
                title=job.title,
                priority=job.priority,
                api_result=result,
                usage=usage,
            )

            save_ai_summary(conn, summary)
            update_job_status(conn, job.fingerprint, JobStatus.COMPLETED)
            saved_fingerprints.add(job.fingerprint)
            succeeded += 1

            logger.info(
                f"[SUMMARY] OK {job.ticker} headline=\"{summary.headline}\" "
                f"tone={summary.tone} review={summary.needs_review}"
            )

        except Exception as e:
            failed += 1
            error_msg = str(e)[:500]
            errors.append(f"{job.ticker}: {error_msg[:100]}")
            update_job_status(
                conn, job.fingerprint, JobStatus.FAILED,
                error_msg=error_msg,
                increment_retry=True,
            )
            logger.error(
                f"[SUMMARY] FAILED {job.ticker} {job.title[:30]}: {e}"
            )
            # パイプライン全体は止めない

    return processed, succeeded, failed, errors, saved_fingerprints


# ============================================================
# Phase 3: Discord 通知
# ============================================================

# V1 通知の一時停止フラグ
# True: V1通知有効。processed_fingerprints による絞込みは必ず行われる。
_V1_NOTIFY_ENABLED = True


def _validate_summary_for_notify(summary: AISummary) -> tuple[bool, str]:
    """通知前バリデーション。

    Returns: (is_valid, skip_reason)
    """
    # headline が空
    if not summary.headline or not summary.headline.strip():
        return False, "empty_headline"

    # bullets が全て空
    bullets = summary.bullets
    if not bullets or all(not b.strip() for b in bullets):
        return False, "empty_bullets"

    # ticker が空
    if not summary.ticker or not summary.ticker.strip():
        return False, "empty_ticker"

    return True, ""


def _send_notifications(
    conn: sqlite3.Connection,
    webhook_url: str,
    dry_run: bool = False,
    processed_fingerprints: set[str] | None = None,
) -> int:
    """未通知の V1 要約を Discord に送信する。

    通知対象は「今回 DB 保存成功分」のみ。
    過去の未通知レコードは自動実行では拾わない。
    過去未通知の救済は専用の手動バックフィルコマンドでのみ実施する。

    Parameters
    ----------
    processed_fingerprints : set[str] | None
        今回 DB 保存成功分の fingerprint 集合。
        None または空の場合、V1 通知は 0 件で即 return。
        これにより呼び出し漏れでも再送事故を防げる。

    Returns
    -------
    int : 実際に送信成功した件数

    通知フロー:
        1. get_unnotified_summaries() で未通知レコードを取得
        2. fingerprint in processed_fingerprints で今回分のみに絞る
        3. _validate_summary_for_notify() で空コンテンツ排除
        4. Discord 送信
        5. 送信成功時のみ notified_at を更新（失敗時は未更新）
    """
    # ガード: V1通知無効化フラグ
    if not _V1_NOTIFY_ENABLED:
        logger.info("[SUMMARY] V1 notifications DISABLED (_V1_NOTIFY_ENABLED=False)")
        return 0

    # ガード: processed_fingerprints が None / 空 → 0件即return
    if not processed_fingerprints:
        logger.info(
            "[SUMMARY] V1 notifications: 0 (no processed_fingerprints provided)"
        )
        return 0

    summaries = get_unnotified_summaries(conn)
    sent = 0
    skipped_not_current = 0
    skipped_validation = 0
    already_sent_fps: set[str] = set()  # 同一実行内重複送信防止

    for summary in summaries:
        # 今回 DB 保存成功分以外はスキップ（過去未通知レコードの再送防止）
        if summary.fingerprint not in processed_fingerprints:
            skipped_not_current += 1
            continue

        # 同一実行内重複送信防止
        if summary.fingerprint in already_sent_fps:
            continue

        # 送信前バリデーション
        is_valid, skip_reason = _validate_summary_for_notify(summary)
        if not is_valid:
            logger.warning(
                f"[SUMMARY] skip notify: {summary.ticker} reason={skip_reason}"
            )
            skipped_validation += 1
            continue

        try:
            ok = send_summary_discord(webhook_url, summary, dry_run=dry_run)
            if ok:
                # Discord 送信成功時のみ notified_at を更新
                # 送信失敗時は notified_at 未更新 → 次回再試行対象
                if not dry_run:
                    mark_summary_notified(conn, summary.summary_id)
                already_sent_fps.add(summary.fingerprint)
                sent += 1
        except Exception as e:
            logger.error(f"[SUMMARY] notification failed: {e}")
            # 送信失敗: notified_at は更新しない → 次回再試行対象

    if skipped_not_current > 0:
        logger.info(f"[SUMMARY] skipped {skipped_not_current} old unnotified summaries")
    if skipped_validation > 0:
        logger.info(f"[SUMMARY] skipped {skipped_validation} invalid summaries")

    return sent


# ============================================================
# メインエントリポイント
# ============================================================
def run_summary_pipeline(
    docs: list[DocumentMeta],
    db_path: str,
    dry_run: bool = False,
    target_date: str | None = None,
    skip_low: bool = True,
    webhook_url: str = "",
    model: str = "",
    event_results: list[dict] | None = None,
) -> SummaryPipelineResult:
    """AI要約パイプラインを実行する。

    イベントパイプライン完了後に呼び出す。
    AI要約失敗でも全体のイベント検知・通知基盤は止めない。

    Parameters
    ----------
    docs : list[DocumentMeta]
        処理対象の文書リスト
    db_path : str
        SQLite DB パス (decision_db.db)
    dry_run : bool
        True の場合:
        - OpenAI API 呼び出しなし
        - DB 書き込みなし (メモリDB使用)
        - Discord 送信なし (ログ出力のみ)
    skip_low : bool
        low 優先度をスキップするか
    webhook_url : str
        Discord Webhook URL
    target_date : str | None
        対象日付 (V2 決算短信処理で使用)
    model : str
        使用モデル（空の場合はデフォルト gpt-5.4-mini）
    event_results : list[dict] | None
        イベントパイプラインの検知結果
    """
    result = SummaryPipelineResult()

    conn = None
    try:
        if dry_run:
            # dry-run: メモリDB使用 → 永続DBへの書き込みなし
            conn = sqlite3.connect(":memory:")
            logger.info("[SUMMARY] dry-run mode: using in-memory DB (no persistent writes)")
        else:
            conn = sqlite3.connect(db_path)
        ensure_summary_tables(conn)

        # ============================================================
        # Phase 0: 決算短信V2 — 全件保存・条件付き通知
        # ============================================================
        try:
            from .earnings_production_pipeline import run_earnings_production
            from src.fetcher import fetch_new_disclosures

            # fetch_new_disclosures() で DisclosureItem（xbrl_url付き）を直接取得
            # DocumentMeta 経由では xbrl_url が喪失するため
            disclosure_items = fetch_new_disclosures(target_date=target_date)
            logger.info(f"[SUMMARY] V2: fetched {len(disclosure_items)} DisclosureItems for V2")

            v2_result = run_earnings_production(
                docs=disclosure_items,
                conn=conn,
                webhook_url=webhook_url if not dry_run else "",
                model=model,
                dry_run=dry_run,
            )
            result.earnings_generated = v2_result.generated_count
            result.earnings_saved = v2_result.saved_count
            result.earnings_notified = v2_result.notified_count
            result.earnings_filtered = v2_result.filtered_count
            result.earnings_no_yoy = v2_result.no_yoy_count
            result.earnings_already_exists = v2_result.already_exists_count
            result.errors.extend(v2_result.errors)
            logger.info(
                f"[SUMMARY] V2 earnings: generated={v2_result.generated_count} "
                f"saved={v2_result.saved_count} notified={v2_result.notified_count} "
                f"filtered={v2_result.filtered_count}"
            )
        except Exception as e:
            logger.error(f"[SUMMARY] V2 earnings error (non-fatal): {e}")
            result.errors.append(f"earnings_v2_error: {str(e)[:200]}")

        # ============================================================
        # Phase 1: ジョブ登録 (既存V1)
        # ============================================================
        created, skipped = _register_jobs(docs, conn, event_results, skip_low)
        result.jobs_created = created
        result.jobs_skipped = skipped
        logger.info(f"[SUMMARY] jobs registered: created={created} skipped={skipped}")

        # Phase 2: AI要約生成 (既存V1)
        processed, succeeded, failed, errors, saved_fps = _process_pending_jobs(
            conn, dry_run=dry_run, model=model,
        )
        result.jobs_processed = processed
        result.jobs_succeeded = succeeded
        result.jobs_failed = failed
        result.errors.extend(errors)
        logger.info(
            f"[SUMMARY] jobs processed: {processed} "
            f"succeeded={succeeded} failed={failed} saved_fps={len(saved_fps)}"
        )

        # Phase 3: Discord 通知 (既存V1)
        # 通知対象: 今回DB保存成功分のみ (saved_fps)
        # 過去の未通知レコードは自動実行では拾わない
        if webhook_url or dry_run:
            sent = _send_notifications(
                conn, webhook_url, dry_run=dry_run,
                processed_fingerprints=saved_fps,
            )
            result.notifications_sent = sent
            logger.info(f"[SUMMARY] notifications sent: {sent}")

    except Exception as e:
        # AI要約全体が失敗してもイベントパイプラインは止めない
        logger.error(f"[SUMMARY] pipeline error (non-fatal): {e}")
        result.errors.append(f"pipeline_error: {str(e)[:200]}")

    finally:
        if conn:
            conn.close()

    return result
