import logging
import os
import time
import traceback
from typing import List, Dict, Any

from lib.pipeline.prior_comparative_saver import save_prior_comparative_from_event

logger = logging.getLogger("prior_comparative_realtime")

import re

def resolve_tdnet_doc_id_for_prior_comparative(item) -> str | None:
    candidates = set()
    
    fields_to_check = ["tdnet_doc_id", "document_id", "doc_id", "disclosure_id"]
    for field in fields_to_check:
        val = getattr(item, field, None)
        if val and isinstance(val, str):
            for match in re.finditer(r'(140120\d{10,})', val):
                candidates.add(match.group(1))
                
    urls_to_check = ["doc_url", "xbrl_url", "pdf_url", "archive_url"]
    for url_field in urls_to_check:
        url = getattr(item, url_field, None)
        if url and isinstance(url, str):
            for match in re.finditer(r'(140120\d{10,})', url):
                candidates.add(match.group(1))
                
    if not candidates:
        return None
        
    if len(candidates) > 1:
        return None
        
    return candidates.pop()

def run_prior_comparative_realtime_hook(target_items: List[Any], max_docs: int) -> Dict[str, Any]:
    """
    TDNET realtime pipeline から呼ばれる prior_comparative 抽出の hook.
    """
    pc_dry_run = os.environ.get("PRIOR_COMPARATIVE_REALTIME_DRY_RUN", "1") != "0"
    pc_canaries_str = os.environ.get("PRIOR_COMPARATIVE_REALTIME_CANARY_TICKERS", "").strip()
    is_all_xbrl_mode = pc_canaries_str.upper() == "ALL_XBRL"
    pc_canaries = [t.strip() for t in pc_canaries_str.split(",") if t.strip()] if pc_canaries_str and not is_all_xbrl_mode else []
    
    # 【保存ONガード設定（全体）】
    # 個別アイテムのループ内で詳細判定を行う
    items_to_process = target_items[:max_docs]
    if not items_to_process:
        logger.info("[PRIOR_COMPARATIVE_REALTIME] No docs to process")
        return {"processed": 0}
        
    summary = {
        "targets": len(items_to_process),
        "errors": 0,
    }
    
    for item in items_to_process:
        raw_disclosure_id = getattr(item, "disclosure_id", "unknown")
        ticker = getattr(item, "ticker", "unknown")
        title = getattr(item, "title", "unknown")
        disclosure_date = getattr(item, "published_at", None) or getattr(item, "disclosure_datetime", "unknown")
        
        resolved_doc_id = resolve_tdnet_doc_id_for_prior_comparative(item)
        if not resolved_doc_id:
            logger.info(f"[PRIOR_COMPARATIVE_REALTIME_SKIP] reason=doc_id_unresolved raw_disclosure_id={raw_disclosure_id} ticker={ticker} title=\"{title[:30]}\"")
            continue
            
        logger.info(f"[PRIOR_COMPARATIVE_REALTIME] raw_disclosure_id={raw_disclosure_id} resolved_doc_id={resolved_doc_id}")
        
        start_time = time.monotonic()
        
        error_msg = None
        stats = {}
        generated_rows = 0
        would_insert_rows = 0
        duplicate_count = 0
        skip_reason = "none"

        # 【個別アイテムの保存許可判定】
        current_save_mode = "dry_run"
        current_dry_run = True

        if not pc_dry_run:
            if not pc_canaries and not is_all_xbrl_mode:
                logger.info(f"[PRIOR_COMPARATIVE_REALTIME_SAFE_SKIP] reason=dry_run_false_without_canary_tickers ticker={ticker} raw_disclosure_id={raw_disclosure_id} resolved_doc_id={resolved_doc_id}")
                continue
            elif is_all_xbrl_mode:
                current_save_mode = "realtime_all_xbrl_insert"
                current_dry_run = False
            elif ticker not in pc_canaries:
                logger.info(f"[PRIOR_COMPARATIVE_REALTIME_SAFE_SKIP] reason=ticker_not_in_canary_list ticker={ticker} raw_disclosure_id={raw_disclosure_id} resolved_doc_id={resolved_doc_id}")
                current_save_mode = "dry_run"
                current_dry_run = True
            else:
                current_save_mode = "realtime_canary_insert"
                current_dry_run = False
        
        try:
            # ensure scratch/prior_comparative_rollback exists
            rollback_dir = "scratch/prior_comparative_rollback"
            os.makedirs(rollback_dir, exist_ok=True)
            
            # save_prior_comparative_from_event に1件だけ渡して詳細な統計を取る
            stats = save_prior_comparative_from_event(
                disclosure_ids=[resolved_doc_id],
                dry_run=current_dry_run,
                save_mode=current_save_mode,
                canary_tickers=pc_canaries if pc_canaries else None,
                max_insert_rows=50,
                rollback_preview_dir=rollback_dir,
            )
            
            # saver 側の stats から推測
            if stats.get("errors", 0) > 0:
                error_msg = "Error inside saver"
                
            would_insert_rows = stats.get("would_insert_rows", 0)
            duplicate_count = stats.get("duplicate_source_row_key_count", 0)
            
            # skip理由の判定
            if stats.get("skipped_already_exists"): skip_reason = "already_exists"
            elif stats.get("skipped_denylist"): skip_reason = "denylist"
            elif stats.get("skipped_no_prior"): skip_reason = "no_prior_in_xbrl"
            elif stats.get("skipped_no_official"): skip_reason = "no_official_current_db"
            elif stats.get("skipped_duplicate_payload"): skip_reason = "duplicate_payload"
            elif stats.get("db_source_row_key_collisions"): skip_reason = "db_source_row_key_collision"
            elif would_insert_rows == 0 and not error_msg: skip_reason = "no_rows_generated_or_skipped"
            
            # generated_rows is loosely would_insert_rows unless skipped_duplicate_payload
            generated_rows = would_insert_rows
            if skip_reason == "duplicate_payload":
                generated_rows = -1 # payload generated but dropped
                
        except Exception as e:
            error_msg = str(e)
            summary["errors"] += 1
            logger.error(f"[PRIOR_COMPARATIVE_REALTIME] Exception resolved_doc_id={resolved_doc_id}: {e}\n{traceback.format_exc()}")
            
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        
        log_prefix = "[PRIOR_COMPARATIVE_REALTIME_CANARY_INSERT]" if not current_dry_run else "[PRIOR_COMPARATIVE_REALTIME_DRY_RUN]"
        log_msg = (
            f"{log_prefix} "
            f"ticker={ticker} raw_disclosure_id={raw_disclosure_id} resolved_doc_id={resolved_doc_id} date={disclosure_date} title=\"{title[:30]}\" "
            f"generated_rows={generated_rows} would_insert_rows={would_insert_rows} inserted_rows={stats.get('inserted_rows', 0)} "
            f"duplicate_source_row_key_count={duplicate_count} "
            f"save_mode={current_save_mode} canary_tickers={pc_canaries_str} "
            f"skip_reason={skip_reason} elapsed_ms={elapsed_ms} error={error_msg is not None}"
        )
        logger.info(log_msg)
        
        if duplicate_count > 0:
            logger.warning(f"{log_prefix} BLOCKED doc_id={resolved_doc_id}: duplicate_source_row_key_count={duplicate_count}")
            
    return summary
