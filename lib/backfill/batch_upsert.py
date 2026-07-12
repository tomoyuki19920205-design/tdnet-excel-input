"""lib/backfill/batch_upsert.py — main スレッドでの batch upsert

worker は DB に触らず、ここで explicit transaction 単位で upsert する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("backfill.upsert")


@dataclass
class BatchUpsertStats:
    total_records: int = 0
    total_batches: int = 0
    succeeded_batches: int = 0
    failed_batches: int = 0
    inserted: int = 0
    updated: int = 0
    no_change: int = 0

    @property
    def average_batch_size(self) -> float:
        return self.total_records / max(self.total_batches, 1)


def _has_earnings_summaries_table(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'earnings_summaries' LIMIT 1"
    ).fetchone())


def _has_documents_table(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents' LIMIT 1"
    ).fetchone())


def normalize_and_validate_rec(
    conn,
    rec: dict,
    *,
    earnings_summaries_available: bool | None = None,
    documents_available: bool | None = None,
) -> tuple[bool, str, str]:
    """
    レコードの period を通期決算期末日型に正規化し、チェックする。
    Returns:
        (is_ok, reason, applied_source)
    """
    ticker = rec.get("ticker", "").strip()
    original_period = rec.get("period", "").strip()
    quarter = rec.get("quarter", "").strip()
    tdnet_doc_id = rec.get("tdnet_doc_id", "")
    disclosure_date = rec.get("disclosure_date", "")

    # Ticker の正規化 (4桁)
    if len(ticker) > 4:
        ticker = ticker[:4]
    rec["ticker"] = ticker

    if not ticker or not original_period or not quarter:
        return False, "missing_required_fields", "none"

    if quarter not in ("1Q", "2Q", "3Q", "FY"):
        return False, f"invalid_quarter:{quarter}", "none"

    role = rec.get("_segment_period_role", "")
    verified = (
        rec.get("_identity_verified") is True
        and rec.get("_identity_verdict") in {"exact_document_id_match", "official_linked_xbrl_match"}
        and bool(rec.get("_requested_disclosure_no"))
        and bool(rec.get("_internal_document_id"))
        and bool(rec.get("_canonical_expected_period"))
        and rec.get("_canonical_expected_quarter") in ("1Q", "2Q", "3Q", "FY")
        and bool(rec.get("_resolved_zip_sha256"))
        and rec.get("_verified_xbrl_same_zip") is True
        and rec.get("_worker_version") == "v4"
        and rec.get("source") == "backfill_xbrl"
        and rec.get("extractor_route") == "xbrl"
    )
    if rec.get("_identity_verified") is True and not verified:
        return False, "verified_xbrl_provenance_incomplete", "none"
    if verified and not ("7921" in ticker and quarter == "3Q"):
        expected_period = rec["_canonical_expected_period"]
        expected_quarter = rec["_canonical_expected_quarter"]
        if role == "current":
            if original_period != expected_period or quarter != expected_quarter:
                return False, "verified_current_period_contract_mismatch", "none"
            return True, "ok", "verified_current_xbrl"
        if role == "previous":
            if not original_period or original_period >= expected_period or quarter != expected_quarter:
                return False, "verified_previous_period_contract_mismatch", "none"
            return True, "ok", "verified_previous_xbrl"
        return False, "verified_unknown_period_role", "none"

    if earnings_summaries_available is None:
        earnings_summaries_available = _has_earnings_summaries_table(conn)
    if documents_available is None:
        documents_available = _has_documents_table(conn)

    cur = conn.cursor()
    resolved_period = None
    applied_source = "none"

    # 優先順位 1: 同日・同quarterの earnings_summaries から解決
    if earnings_summaries_available and disclosure_date:
        clean_date = disclosure_date.replace("-", "")
        hyphen_date = f"{clean_date[:4]}-{clean_date[4:6]}-{clean_date[6:8]}" if len(clean_date) == 8 else disclosure_date
        
        # まず title からパースを試みる
        res_title = cur.execute(
            "SELECT title FROM earnings_summaries WHERE ticker = ? AND quarter = ? AND disclosure_date IN (?, ?, ?) LIMIT 1",
            (ticker, quarter, disclosure_date, clean_date, hyphen_date)
        ).fetchone()
        if res_title and res_title[0]:
            from src.year_parser import extract_fiscal_info
            fy, _ = extract_fiscal_info(res_title[0])
            if fy:
                resolved_period = fy
                applied_source = "earnings_summaries_direct_title"

        # もし title から解決できなければ、fiscal_year (年) から組み立てる
        if not resolved_period:
            res_fy = cur.execute(
                "SELECT fiscal_year FROM earnings_summaries WHERE ticker = ? AND quarter = ? AND disclosure_date IN (?, ?, ?) LIMIT 1",
                (ticker, quarter, disclosure_date, clean_date, hyphen_date)
            ).fetchone()
            if res_fy and res_fy[0]:
                fy_month_res = cur.execute(
                    "SELECT fiscal_year FROM earnings_summaries WHERE ticker = ? AND quarter = 'FY' LIMIT 1",
                    (ticker,)
                ).fetchone()
                if not fy_month_res or not fy_month_res[0]:
                    fy_month_res = cur.execute(
                        "SELECT fiscal_year_end FROM segment_financials WHERE company_code = ? LIMIT 1",
                        (ticker,)
                    ).fetchone()
                
                if fy_month_res and fy_month_res[0]:
                    try:
                        fy_m = int(fy_month_res[0].split("-")[1]) if "-" in fy_month_res[0] else 2
                        y = int(res_fy[0])
                        import calendar
                        last_day = calendar.monthrange(y, fy_m)[1]
                        resolved_period = f"{y}-{fy_m:02d}-{last_day:02d}"
                        applied_source = "earnings_summaries_direct_fy_reconstruct"
                    except Exception:
                        pass

    if not resolved_period and quarter == "FY":
        resolved_period = original_period
        applied_source = "fy_default"

    # 優先順位 2: documents の title から year_parser で抽出
    if not resolved_period and tdnet_doc_id:
        if not documents_available:
            if "7921" in ticker and quarter == "3Q":
                return False, "7921_quarter_skew_unverifiable_without_documents", "none"
            return False, "documents_unavailable_for_period_resolution", "none"
        doc_res = cur.execute(
            "SELECT title FROM documents WHERE tdnet_doc_id = ? LIMIT 1",
            (tdnet_doc_id,)
        ).fetchone()
        if doc_res and doc_res[0]:
            title = doc_res[0]
            # 7921のquarter誤判定の特別検知
            if "7921" in ticker and "3Q" in quarter and "第３四半期" not in title:
                return False, f"7921_quarter_skew_warning:title={title}", "none"

            from src.year_parser import extract_fiscal_info
            fy, _ = extract_fiscal_info(title)
            if fy:
                resolved_period = fy
                applied_source = "year_parser_title"

    # 解決できなかった場合、quarter="FY" なら元periodを信用するが、それ以外は元periodが通期末日形式（R表記等から変換された形式）かチェック
    if not resolved_period:
        if quarter == "FY":
            resolved_period = original_period
            applied_source = "fy_default"
        elif earnings_summaries_available:
            # 優先順位 3: 最終フォールバックとして、同 ticker の直近の他Qレコードなどから推定
            fy_month_res = cur.execute(
                "SELECT fiscal_year FROM earnings_summaries WHERE ticker = ? AND quarter = 'FY' LIMIT 1",
                (ticker,)
            ).fetchone()
            if fy_month_res and fy_month_res[0]:
                try:
                    # 期末月を合致させて年を推定する
                    fy_month = int(fy_month_res[0].split("-")[1])
                    orig_year = int(original_period.split("-")[0])
                    orig_month = int(original_period.split("-")[1])
                    
                    # 1Q=9ヶ月、2Q=6ヶ月、3Q=3ヶ月後に期末
                    expected_shift = {"1Q": 9, "2Q": 6, "3Q": 3}.get(quarter, 0)
                    
                    # 月数を足して月末にする
                    y = orig_year
                    m = orig_month + expected_shift
                    if m > 12:
                        y += (m - 1) // 12
                        m = (m - 1) % 12 + 1
                    
                    import calendar
                    last_day = calendar.monthrange(y, m)[1]
                    resolved_period = f"{y}-{m:02d}-{last_day:02d}"
                    applied_source = "calendar_arithmetic_fallback"
                except Exception:
                    pass

            if not resolved_period:
                return False, "unresolved_fiscal_year_end", "none"

    # 日付の検証: resolved_period が四半期末日型 (例: 2026-05-31) になっていないことを確認
    if resolved_period != original_period:
        try:
            from datetime import datetime
            orig_d = datetime.strptime(original_period, "%Y-%m-%d")
            res_d = datetime.strptime(resolved_period, "%Y-%m-%d")
            month_diff = (res_d.year - orig_d.year) * 12 + res_d.month - orig_d.month
            
            # 月差が異常に乖離していないか
            expected_diff = {"1Q": 9, "2Q": 6, "3Q": 3}.get(quarter, 0)
            
            if expected_diff > 0:
                # 判定ルール1: 当期レコード (月差が期待値と一致)
                if abs(month_diff - expected_diff) <= 2:
                    rec["period"] = resolved_period
                # 判定ルール2: 前期比較レコード (月差が期待値 + 12ヶ月と一致)
                elif abs(month_diff - (expected_diff + 12)) <= 2:
                    try:
                        res_y = res_d.year - 1
                        import calendar
                        last_day = calendar.monthrange(res_y, res_d.month)[1]
                        resolved_period = f"{res_y}-{res_d.month:02d}-{last_day:02d}"
                        rec["period"] = resolved_period
                        applied_source += "_previous_year_adjusted"
                    except Exception as e:
                        return False, f"previous_adjustment_error:{e}", "none"
                else:
                    return False, f"period_gap_too_large:orig={original_period},res={resolved_period},diff={month_diff}", "none"
            else:
                # expected_diff が 0 (FYなど) の場合
                if quarter == "FY" and resolved_period != original_period:
                    # FYは勝手に前年へ落とす補正は行わず、厳密に一致する場合のみ
                    return False, f"fy_period_mismatch:orig={original_period},res={resolved_period}", "none"
                rec["period"] = resolved_period
                
        except Exception as e:
            return False, f"date_parse_error:{e}", "none"
    else:
        rec["period"] = resolved_period

    # 最終検証: quarter != FY の場合に period が四半期末日のままになっていないか
    if earnings_summaries_available and quarter != "FY" and rec["period"] == original_period:
        fy_month_res = cur.execute(
            "SELECT fiscal_year FROM earnings_summaries WHERE ticker = ? AND quarter = 'FY' LIMIT 1",
            (ticker,)
        ).fetchone()
        if fy_month_res and fy_month_res[0]:
            try:
                res_m = int(rec["period"].split("-")[1])
                fy_m = int(fy_month_res[0].split("-")[1])
                if res_m != fy_m:
                    return False, f"period_is_quarter_end:period={rec['period']},expected_fy_month={fy_m}", "none"
            except Exception:
                pass

    return True, "ok", applied_source


def dry_run_upsert_segments(records: list[dict], decision_db) -> BatchUpsertStats:
    stats = BatchUpsertStats(total_records=len(records))
    print("\n  ==========================================================================")
    print("    SEGMENT BACKFILL DRY RUN VERIFICATION REPORT")
    print("  ==========================================================================")
    
    earnings_summaries_available = _has_earnings_summaries_table(decision_db._conn)
    documents_available = _has_documents_table(decision_db._conn)
    seen_keys = set()
    for rec in records:
        orig_period = rec.get("period", "")
        # documents からタイトルや情報を引くために本番DBを参照
        is_ok, reason, source_info = normalize_and_validate_rec(
            decision_db._conn,
            rec,
            earnings_summaries_available=earnings_summaries_available,
            documents_available=documents_available,
        )
        
        # 重複チェック (同一ticker/period/quarter/segment_name)
        key = (rec["ticker"], rec["period"], rec["quarter"], rec["segment_name"])
        if is_ok:
            if key in seen_keys:
                is_ok = False
                reason = "duplicate_record_skipped"
                source_info = "seen_cache_dedup"
            else:
                seen_keys.add(key)
        
        status_str = "SAVE_SCHEDULED" if is_ok else f"SKIP ({reason})"
        if is_ok:
            stats.inserted += 1
        else:
            stats.failed_batches += 1

        company_name = "Unknown"
        title = "No Document Title Found"
        cur = decision_db._conn.cursor()
        
        if earnings_summaries_available:
            summary_row = cur.execute(
                "SELECT company_name FROM earnings_summaries WHERE ticker = ? LIMIT 1",
                (rec["ticker"],)
            ).fetchone()
            if summary_row and summary_row[0]:
                company_name = summary_row[0]
            
        if documents_available:
            doc_row = cur.execute(
                "SELECT title FROM documents WHERE tdnet_doc_id = ? LIMIT 1",
                (rec.get("tdnet_doc_id", ""),)
            ).fetchone()
            if doc_row and doc_row[0]:
                title = doc_row[0]

        print(
            f"  [VERIFY] Ticker: {rec['ticker']:<5} | Company: {company_name:<10} | "
            f"Q: {rec['quarter']:<3} | OrigPeriod: {orig_period:<10} -> SavedPeriod: {rec['period']:<10} | "
            f"Source: {rec['source']:<12} (KeySource: {source_info:<12}) | "
            f"Status: {status_str:<22}\n"
            f"           Title: {title[:75]}"
        )
        print("  " + "-" * 115)
        
    print(f"\n  [SUMMARY] Total input segment records: {stats.total_records}")
    print(f"            Scheduled to insert (SQLite): {stats.inserted}")
    print(f"            Scheduled to sync (Supabase): {stats.inserted}")
    print(f"            Skipped/Invalid:             {stats.failed_batches}")
    print("  ==========================================================================")
    return stats


def batch_upsert_segments(
    records: list[dict],
    decision_db,
    *,
    batch_size: int = 200,
    source: str = "backfill",
    actor: str = "backfill",
    xbrl_cleanup_keys: dict | None = None,
) -> BatchUpsertStats:
    """segment_records を batch 単位で explicit transaction upsert する。"""
    stats = BatchUpsertStats(total_records=len(records))

    if not records:
        return stats

    earnings_summaries_available = _has_earnings_summaries_table(decision_db._conn)
    documents_available = _has_documents_table(decision_db._conn)

    # ── _xbrl_cleanup_meta の自動検出 ──
    _auto_cleanup = xbrl_cleanup_keys
    for _r in records:
        _meta = _r.get("_xbrl_cleanup_meta")
        if _meta:
            _auto_cleanup = _meta
            break

    chunks = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    stats.total_batches = len(chunks)

    _cleanup_done = False

    for batch_idx, chunk in enumerate(chunks):
        try:
            decision_db._conn.execute("BEGIN")

            if _auto_cleanup and not _cleanup_done:
                from lib.backfill.segment_partial_check import (
                    cleanup_old_xbrl_rows,
                    cleanup_aggregate_pdf_rows,
                )
                _deleted_xbrl = cleanup_old_xbrl_rows(
                    decision_db._conn,
                    ticker=_auto_cleanup["ticker"],
                    fiscal_year_end=_auto_cleanup["fiscal_year_end"],
                    quarter=_auto_cleanup["quarter"],
                    tdnet_doc_id=_auto_cleanup["tdnet_doc_id"],
                    reason="pdf_v4_adopted",
                )
                _deleted_agg = cleanup_aggregate_pdf_rows(
                    decision_db._conn,
                    ticker=_auto_cleanup["ticker"],
                    fiscal_year_end=_auto_cleanup["fiscal_year_end"],
                    quarter=_auto_cleanup["quarter"],
                    tdnet_doc_id=_auto_cleanup["tdnet_doc_id"],
                )
                _cleanup_done = True
                logger.info(
                    "[upsert] cleanup: ticker=%s fy=%s quarter=%s tdnet_doc_id=%s "
                    "xbrl_deleted=%d aggregate_deleted=%d",
                    _auto_cleanup["ticker"],
                    _auto_cleanup["fiscal_year_end"],
                    _auto_cleanup["quarter"],
                    _auto_cleanup["tdnet_doc_id"],
                    _deleted_xbrl,
                    _deleted_agg,
                )

            batch_inserted = 0
            batch_updated = 0
            batch_no_change = 0

            for rec in chunk:
                # _xbrl_cleanup_meta は DB スキーマ外のサイドバンドキー → 除去
                rec.pop("_xbrl_cleanup_meta", None)
                
                # 正規化と検証
                is_ok, reason, _ = normalize_and_validate_rec(
                    decision_db._conn,
                    rec,
                    earnings_summaries_available=earnings_summaries_available,
                    documents_available=documents_available,
                )
                if not is_ok:
                    logger.warning(
                        "[upsert_skip] Ticker:%s | OrigPeriod:%s | Q:%s | Reason:%s | tdnet_doc_id:%s",
                        rec.get("ticker"), rec.get("period"), rec.get("quarter"), reason, rec.get("tdnet_doc_id")
                    )
                    continue

                result = decision_db.upsert_segment(
                    company_code=rec["ticker"],
                    fiscal_year_end=rec["period"],
                    quarter=rec["quarter"],
                    segment_name=rec["segment_name"],
                    segment_order=rec.get("segment_order", 0),
                    segment_sales=rec.get("segment_sales"),
                    segment_profit=rec.get("segment_profit"),
                    unit_raw=rec.get("unit_raw"),
                    unit_multiplier=rec.get("unit_multiplier"),
                    raw_profit_label=rec.get("raw_profit_label", ""),
                    data_source=rec.get("source", source),
                    actor=actor,
                    source=source,
                    segment_name_norm=rec.get("segment_name_norm"),
                    extractor_route=rec.get("extractor_route"),
                    source_doc_type=rec.get("source_doc_type"),
                    disclosure_date=rec.get("disclosure_date"),
                    tdnet_doc_id=rec.get("tdnet_doc_id"),
                    row_type=rec.get("row_type"),
                )
                if result == "inserted":
                    batch_inserted += 1
                elif result == "updated":
                    batch_updated += 1
                else:
                    batch_no_change += 1

            decision_db._conn.execute("COMMIT")
            stats.succeeded_batches += 1
            stats.inserted += batch_inserted
            stats.updated += batch_updated
            stats.no_change += batch_no_change

            logger.info(
                f"[upsert] batch {batch_idx + 1}/{len(chunks)}: "
                f"inserted={batch_inserted} updated={batch_updated} "
                f"no_change={batch_no_change}"
            )

        except Exception as e:
            try:
                decision_db._conn.execute("ROLLBACK")
            except Exception:
                pass
            stats.failed_batches += 1
            logger.error(
                f"[upsert] batch {batch_idx + 1}/{len(chunks)} FAILED: {e} "
                f"({len(chunk)} records lost)"
            )

    return stats
