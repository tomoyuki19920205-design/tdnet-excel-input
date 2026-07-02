"""lib/pipeline/prior_comparative_saver.py - nightly/realtime用 prior_comparative 保存処理

batch_prior_comparative.py のロジックをベースに、指定された disclosure_ids に対し
安全に prior_comparative を抽出・生成し保存（当面はdry-runのみ）する。
"""

import logging
import os
import re
import json
import traceback

from src.segment.xbrl_segment_extractor import extract_segments_from_xbrl_zip
from src.segment.prior_comparative_generator import generate_prior_comparative_payload
from lib.pipeline.db import supabase_select

logger = logging.getLogger("pipeline.prior_comparative")

def normalize_doc_id(filename: str) -> str:
    basename = os.path.basename(filename)
    match = re.search(r'(20\d{12})', basename)
    if match:
        return f"1401{match.group(1)}"
    return "UNKNOWN"

def _load_denylist(denylist_path: str | None = None) -> set[str]:
    """NEEDS_REVIEW銘柄などの明示的除外リストを読み込む。
    読めない場合は例外送出 (Stop) する。
    """
    if not denylist_path:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        denylist_path = os.path.join(project_root, "data", "config", "needs_review_denylist.json")
    
    if not os.path.isfile(denylist_path):
        raise FileNotFoundError(f"Denylist file not found at {denylist_path}. MUST exist to run prior_comparative saver.")
    
    try:
        with open(denylist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Denylist must be a JSON array of tickers.")
            return set(str(t) for t in data)
    except Exception as e:
        raise RuntimeError(f"Failed to load denylist from {denylist_path}: {e}")

def get_xbrl_zip_path_for_doc_id(doc_id: str, project_root: str) -> str | None:
    """doc_idに対応するZIPパスを探す。
    tdnet_ingest.pyは 'data/xbrl_archive/{code}_{date}_{basename}' に保存する。
    """
    import glob
    archive_dir = os.path.join(project_root, "data", "xbrl_archive")
    zips = glob.glob(os.path.join(archive_dir, f"*{doc_id[4:]}*.zip"))
    if zips:
        return zips[0]
    return None

def save_prior_comparative_from_event(
    disclosure_ids: list[str],
    *,
    dry_run: bool = True,
    save_mode: str = "dry_run",
    canary_tickers: list[str] | None = None,
    canary_doc_ids: list[str] | None = None,
    allowed_source_row_keys: list[str] | None = None,
    rollback_preview_dir: str | None = None,
    max_insert_rows: int = 10,
    denylist_path: str | None = None,
) -> dict:
    """
    指定された disclosure_ids に対して prior_comparative を抽出・生成し保存する。

    Args:
        disclosure_ids: 処理対象のtdnet_disclosure_idリスト (e.g. ['14012026...'])
        dry_run: True時はDB更新せずログ出力のみ。※本フェーズでは実保存経路は無効化(Falseでも保存しない)
        canary_tickers: 指定された場合、この銘柄のみINSERT候補として評価する
        denylist_path: NEEDS_REVIEW除外リストJSONのパス
    """
    if dry_run and save_mode not in ("canary_insert", "realtime_canary_insert"):
        save_mode = "dry_run"
    if save_mode not in ("dry_run", "canary_insert", "realtime_canary_insert"):
        raise RuntimeError(f"STOP: Unknown save_mode={save_mode}")

    stats = {
        "targets": len(disclosure_ids),
        "processed": 0,
        "skipped_already_exists": 0,
        "skipped_denylist": 0,
        "skipped_9993": 0,
        "skipped_no_prior": 0,
        "skipped_no_official": 0,
        "skipped_duplicate_payload": 0,
        "duplicate_source_row_key_count": 0,
        "errors": 0,
        "would_insert_rows": 0,
        "save_mode": save_mode,
        "inserted_rows": 0,
        "inserted_ids": [],
        "rollback_preview_path": None,
        "readback_rows": 0,
        "existing_rows_before_insert": 0,
        "db_source_row_key_collisions": 0,
    }
    
    if not disclosure_ids:
        return stats

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    # --- 安全チェック1: 9993 ハードコードブロック ---
    # 万が一の混入も許さない
    HARD_BLOCKED = {"9993"}
    
    # --- 安全チェック2: NEEDS_REVIEW 明示的 denylist 読み込み ---
    # 存在しない場合はStop（例外）
    needs_review_denylist = _load_denylist(denylist_path)

    for raw_doc_id in disclosure_ids:
        try:
            doc_id = normalize_doc_id(raw_doc_id)
            if doc_id == "UNKNOWN":
                raise RuntimeError(f"CRITICAL STOP: doc_id normalization failed for raw_doc_id={raw_doc_id}")
            if doc_id != raw_doc_id:
                logger.info(f"[PRIOR_COMP_SAVER] normalized doc_id raw={raw_doc_id} canonical={doc_id}")

            zip_path = get_xbrl_zip_path_for_doc_id(doc_id, project_root)
            if not zip_path:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} SKIP: xbrl zip not found in archive (raw={raw_doc_id})")
                continue

            xbrl_rows = extract_segments_from_xbrl_zip(zip_path)
            if not xbrl_rows:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} SKIP: no xbrl segments extracted")
                continue

            ticker = xbrl_rows[0].normalized_ticker
            if not ticker:
                continue
                
            # canary指定がある場合は他をスキップ
            if canary_tickers is not None and ticker not in canary_tickers:
                continue
                
            if ticker in HARD_BLOCKED:
                # 9993混入時はSKIPではなくStop (例外送出)
                raise RuntimeError(f"CRITICAL STOP: 9993 was found in processing payload. doc_id={doc_id}")
                
            if ticker in needs_review_denylist:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP: denylist (NEEDS_REVIEW)")
                stats["skipped_denylist"] += 1
                continue

            periods = set(r.period for r in xbrl_rows if r.period)
            if len(periods) < 2:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP: no prior period in XBRL")
                stats["skipped_no_prior"] += 1
                continue
                
            current_period = max(periods)
            prior_period = sorted([p for p in periods if p != current_period])[-1]

            # 既存判定: ALREADY_EXISTS チェック (data_basis='prior_comparative' 限定)
            # 粒度: source_doc_id レベルでこのXBRLから抽出された prior_comparative が存在するかを確認。
            db_segments = supabase_select(
                "canonical_segments",
                params={
                    "ticker": f"eq.{ticker}",
                    "source_doc_id": f"eq.{doc_id}",
                    "data_basis": "eq.prior_comparative"
                }
            )
            
            already_exists = False
            if db_segments and len(db_segments) > 0:
                already_exists = True
                
            if already_exists:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP: ALREADY_EXISTS (prior_comparative data already generated from this doc_id)")
                stats["skipped_already_exists"] += 1
                continue

            quarter = xbrl_rows[0].quarter if xbrl_rows else ""

            # DBから official_current (前期) を取得
            # prior_comparative (前期の実績) を生成する際、前期の official_current が持つ
            # unit などのメタデータを引き継ぐために prior_period を使用します。
            official_sources = "in.(edinet_xbrl,backfill_xbrl,xbrl,v4_pdf,backfill_v4_pdf,attachment_xbrl,tdnet)"
            
            official_current_db = supabase_select(
                "canonical_segments",
                params={
                    "ticker": f"eq.{ticker}",
                    "period": f"eq.{prior_period}",
                    "source": official_sources,
                    "data_basis": "is.null"
                }
            )
            
            if not official_current_db and quarter:
                # 四半期決算等の場合、periodが決算期末日に正規化されている可能性があるため、
                # quarter一致かつ prior_period <= period < current_period を満たす行をフォールバック検索する
                candidates = supabase_select(
                    "canonical_segments",
                    params={
                        "ticker": f"eq.{ticker}",
                        "quarter": f"eq.{quarter}",
                        "source": official_sources,
                        "data_basis": "is.null"
                    }
                )
                if candidates:
                    valid = [c for c in candidates if c.get("period") and prior_period <= c["period"] < current_period]
                    if valid:
                        best_period = max(c["period"] for c in valid)
                        official_current_db = [c for c in valid if c["period"] == best_period]

            if not official_current_db:
                official_current_db = supabase_select(
                    "canonical_segments",
                    params={
                        "ticker": f"eq.{ticker}",
                        "period": f"eq.{prior_period}",
                        "data_basis": "is.null"
                    }
                )

            if not official_current_db:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP: no official_current for prior_period={prior_period}")
                stats["skipped_no_official"] += 1
                continue

            # disclosure_datetime 補完
            ev = supabase_select("tdnet_events", params={"ticker": f"eq.{ticker}", "source_url": f"ilike.%{doc_id}%"})
            disclosure_datetime = None
            if ev and ev[0].get("disclosed_at"):
                disclosure_datetime = ev[0]["disclosed_at"]
            else:
                match = re.search(r'1401(20\d{2})(\d{2})(\d{2})', doc_id)
                if match:
                    yyyy, mm, dd = match.groups()
                    disclosure_datetime = f"{yyyy}-{mm}-{dd}T15:00:00+09:00"
                else:
                    raise RuntimeError(f"STOP: Cannot determine source_disclosure_date for doc_id={doc_id}")
            
            if not disclosure_datetime:
                raise RuntimeError(f"STOP: source_disclosure_date is NULL for doc_id={doc_id}")

            quarter = official_current_db[0].get("quarter", "")

            # generator呼び出し
            planned_rows = generate_prior_comparative_payload(
                xbrl_rows,
                official_current_db,
                ticker,
                doc_id,
                disclosure_datetime,
                quarter
            )

            if not planned_rows:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP: payload generation yielded 0 rows")
                continue

            for pr in planned_rows:
                if not pr.get("source_disclosure_date"):
                    raise RuntimeError(f"STOP: generator produced NULL source_disclosure_date for doc_id={doc_id}")

            # --- 安全弁: payload内の source_row_key 重複チェック ---
            from collections import Counter
            source_row_keys = [pr.get("source_row_key") for pr in planned_rows if pr.get("source_row_key")]
            if len(source_row_keys) != len(planned_rows):
                raise RuntimeError(f"STOP: Some rows are missing source_row_key for doc_id={doc_id}")
            
            key_counts = Counter(source_row_keys)
            duplicates = {k: v for k, v in key_counts.items() if v > 1}
            
            if duplicates:
                logger.info(f"[PRIOR_COMP_SAVER] doc_id={doc_id} ticker={ticker} SKIP_DUPLICATE_PAYLOAD: duplicate source_row_key in planned_rows")
                for k, v in duplicates.items():
                    logger.info(f"[PRIOR_COMP_SAVER] duplicate source_row_key={k} count={v}")
                
                stats["skipped_duplicate_payload"] += 1
                stats["duplicate_source_row_key_count"] += len(duplicates)
                continue

            stats["processed"] += 1
            stats["would_insert_rows"] += len(planned_rows)
            
            if save_mode == "dry_run":
                # --- 保存OFF (Dry-Run のみ) ---
                logger.info(f"[PRIOR_COMP_SAVER] [DRY_RUN] doc_id={doc_id} ticker={ticker} WOULD_INSERT: {len(planned_rows)} rows. UPSERT/UPDATE/DELETE are PROHIBITED.")
                for pr in planned_rows:
                    logger.info(
                        f"  -> WOULD_INSERT: segment={pr.get('segment_name_key')} metric={pr.get('metric')} "
                        f"source_row_key={pr.get('source_row_key')} "
                        f"source_doc_id={pr.get('source_doc_id')} "
                        f"source_disclosure_period={pr.get('source_disclosure_period')}"
                    )
            elif save_mode in ("canary_insert", "realtime_canary_insert"):
                import requests
                import datetime
                if not canary_tickers or ticker not in canary_tickers:
                    raise RuntimeError(f"STOP: ticker {ticker} not in canary_tickers")
                
                if save_mode == "canary_insert":
                    if not canary_doc_ids or doc_id not in canary_doc_ids:
                        raise RuntimeError(f"STOP: doc_id {doc_id} not in canary_doc_ids")
                    if not allowed_source_row_keys:
                        raise RuntimeError("STOP: allowed_source_row_keys must be provided for canary_insert")
                    if not rollback_preview_dir:
                        raise RuntimeError("STOP: rollback_preview_dir must be provided for canary_insert")
                
                if ticker == "9993":
                    raise RuntimeError("STOP: 9993 is blocked")
                if ticker in needs_review_denylist:
                    raise RuntimeError(f"STOP: ticker {ticker} is in needs_review_denylist")
                if len(planned_rows) > max_insert_rows:
                    raise RuntimeError(f"STOP: planned rows ({len(planned_rows)}) exceeds max_insert_rows ({max_insert_rows})")
                
                planned_keys = set(pr["source_row_key"] for pr in planned_rows)
                if len(planned_keys) != len(planned_rows):
                    raise RuntimeError("STOP: duplicate source_row_key in payload")

                if save_mode == "canary_insert":
                    if len(planned_rows) != len(allowed_source_row_keys):
                        raise RuntimeError(f"STOP: planned rows ({len(planned_rows)}) != allowed_source_row_keys ({len(allowed_source_row_keys)})")
                    if planned_keys != set(allowed_source_row_keys):
                        raise RuntimeError("STOP: planned source_row_keys mismatch allowed_source_row_keys")
                
                for pr in planned_rows:
                    if pr.get("source_doc_id") != doc_id:
                        raise RuntimeError("STOP: source_doc_id mismatch")
                    if not pr["source_row_key"].endswith(doc_id):
                        raise RuntimeError("STOP: source_row_key does not end with doc_id")
                    if not pr.get("source_disclosure_date"):
                        raise RuntimeError("STOP: source_disclosure_date is NULL")
                    if not pr.get("source_disclosure_period"):
                        raise RuntimeError("STOP: source_disclosure_period is NULL")
                    if not pr.get("source_doc_id"):
                        raise RuntimeError("STOP: source_doc_id is NULL")
                    if not pr.get("segment_key"):
                        raise RuntimeError("STOP: segment_key is NULL")
                    if pr.get("metric") not in ("sales", "profit"):
                        raise RuntimeError("STOP: metric must be sales/profit")
                    if pr.get("value") is None:
                        raise RuntimeError("STOP: value is NULL")
                
                # Check existing in DB
                existing_check = supabase_select("canonical_segments", params={"source_doc_id": f"eq.{doc_id}", "data_basis": "eq.prior_comparative"})
                stats["existing_rows_before_insert"] = len(existing_check) if existing_check else 0
                if existing_check and len(existing_check) > 0:
                    raise RuntimeError(f"STOP: DB already has {len(existing_check)} rows for doc_id={doc_id}")
                
                for k in planned_keys:
                    ex = supabase_select("canonical_segments", params={"source_row_key": f"eq.{k}"})
                    if ex and len(ex) > 0:
                        stats["db_source_row_key_collisions"] += 1
                if stats["db_source_row_key_collisions"] > 0:
                    raise RuntimeError("STOP: source_row_key collisions detected in DB")
                
                # INSERT
                url = os.environ.get("SUPABASE_URL")
                key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                if not url or not key:
                    raise RuntimeError("STOP: Supabase credentials not found in env")
                
                headers = {
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation"
                }
                
                r = requests.post(f"{url}/rest/v1/canonical_segments", json=planned_rows, headers=headers)
                if r.status_code not in (200, 201):
                    raise RuntimeError(f"STOP: Supabase insert failed: HTTP {r.status_code} - {r.text}")
                
                inserted_data = r.json()
                if len(inserted_data) != len(planned_rows):
                    raise RuntimeError(f"STOP: Inserted rows ({len(inserted_data)}) != planned rows ({len(planned_rows)})")
                
                stats["inserted_rows"] = len(inserted_data)
                stats["inserted_ids"] = [row["id"] for row in inserted_data]
                
                readback = supabase_select("canonical_segments", params={"id": f"in.({','.join(map(str, stats['inserted_ids']))})"})
                stats["readback_rows"] = len(readback) if readback else 0
                if stats["readback_rows"] != len(planned_rows):
                    raise RuntimeError(f"STOP: Readback rows ({stats['readback_rows']}) != planned rows ({len(planned_rows)})")
                
                for k in planned_keys:
                    ex = supabase_select("canonical_segments", params={"source_row_key": f"eq.{k}"})
                    if not ex or len(ex) != 1:
                        raise RuntimeError(f"STOP: Exact source_row_key readback failed for {k}")
                
                now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                
                try:
                    rb_file = os.path.join(rollback_preview_dir, f"prior_comparative_canary_rollback_preview_{ticker}_{doc_id}_{now_str}.sql")
                    ids_str = ", ".join([f"'{i}'" for i in stats["inserted_ids"]])
                    rb_sql = f"DELETE FROM canonical_segments\nWHERE id IN ({ids_str})\n  AND ticker = '{ticker}'\n  AND data_basis = 'prior_comparative'\n  AND source_doc_id = '{doc_id}';"
                    
                    with open(rb_file, "w", encoding="utf-8") as f:
                        f.write(rb_sql)
                    logger.info(f"[PRIOR_COMP_SAVER] [CANARY_INSERT] SUCCESS. Inserted {stats['inserted_rows']} rows. Rollback preview saved to {rb_file}")
                    stats["rollback_preview_path"] = rb_file
                except Exception as ex_rb:
                    logger.error(f"[PRIOR_COMP_SAVER] [CANARY_INSERT] SUCCESS. Inserted {stats['inserted_rows']} rows, but failed to write rollback file: {ex_rb}")
                    stats["rollback_preview_path"] = None

        except Exception as e:
            logger.error(f"[PRIOR_COMP_SAVER] doc_id={doc_id} FAILED: {e}\n{traceback.format_exc()}")
            stats["errors"] += 1
            if "STOP:" in str(e) or "CRITICAL" in str(e):
                raise

    return stats
