# ============================================================
# main.py — メインループ（Phase2: DB書き込み版）
# ============================================================
from __future__ import annotations

import calendar
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# プロジェクトルートをPYTHONPATHに追加
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.config import load_config, Config
from src.db import StateDB
from src.downloader import download_document
from src.extractor import extract_financials, extract_forecast_targets
from src.fetcher import fetch_new_disclosures
from src.migration.migration_db import MigrationDB
from src.models import Status, ExtractedFinancials, DisclosureType, ForecastTarget
from src.utils import (
    setup_logger,
    convert_to_excel_unit,
    parse_scale_unit,
    excel_unit_multiplier,
)
from src.year_parser import extract_fiscal_info, parse_reiwa

logger: logging.Logger

JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------------
# R表記 → YYYY-MM-DD 変換
# ------------------------------------------------------------------
def _reiwa_to_fiscal_year_end(r_str: str) -> str | None:
    """R表記 → fiscal_year_end (YYYY-MM-DD), または既に YYYY-MM-DD ならそのまま返す"""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", r_str):
        return r_str
    parsed = parse_reiwa(r_str)
    if parsed is None:
        return None
    ad_year, month = parsed
    last_day = calendar.monthrange(ad_year, month)[1]
    return f"{ad_year:04d}-{month:02d}-{last_day:02d}"


# ------------------------------------------------------------------
# 遅延ログ（publish → update 差分秒）
# ------------------------------------------------------------------
def _calc_latency_sec(published_at: str) -> float | None:
    """published_at(ISO形式)から現在までの差分秒を計算"""
    try:
        # published_atの形式: "2026-02-25 14:30:00" or ISO
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                pub_dt = datetime.strptime(published_at, fmt).replace(tzinfo=JST)
                now_dt = datetime.now(JST)
                return (now_dt - pub_dt).total_seconds()
            except ValueError:
                continue
        return None
    except Exception:
        return None


# ------------------------------------------------------------------
# DB書き込み: 決算短信
# ------------------------------------------------------------------
def _process_financial_statement(
    item,
    doc_path: str,
    xbrl_path: str | None,
    config: Config,
    state_db: StateDB,
    decision_db: MigrationDB,
    run_id: str,
) -> None:
    """決算短信の処理 → DB書き込み"""
    disclosure_id = item.disclosure_id
    code = item.ticker

    # ③ 数値抽出
    financials, extract_error = extract_financials(
        doc_path=doc_path,
        title=item.title,
        xbrl_path=xbrl_path,
    )

    if financials is None:
        logger.error(f"[処理] 数値抽出失敗: {extract_error}")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=Status.PARSE_FAILED, error_detail=extract_error,
        )
        return

    # 年度・四半期チェック
    if not financials.fiscal_year:
        logger.warning(f"[処理] 年度を特定できません")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter=financials.quarter or "",
            status=Status.UNCONFIRMED_YEAR, error_detail="年度を特定できません",
        )
        return

    if not financials.quarter:
        logger.warning(f"[処理] 四半期を特定できません")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year=financials.fiscal_year, quarter="",
            status=Status.PARSE_FAILED, error_detail="四半期を特定できません",
        )
        return

    # R表記 → YYYY-MM-DD
    fiscal_year_end = _reiwa_to_fiscal_year_end(financials.fiscal_year)
    if fiscal_year_end is None:
        logger.error(f"[処理] 年度変換失敗: {financials.fiscal_year}")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year=financials.fiscal_year, quarter=financials.quarter,
            status=Status.PARSE_FAILED,
            error_detail=f"年度変換失敗: {financials.fiscal_year}",
        )
        return

    # ④ 単位変換（書類単位 → DB単位=百万円）
    source_mult = parse_scale_unit(financials.source_unit) if financials.source_unit else 1
    excel_mult = excel_unit_multiplier(config.excel_unit)

    if financials.sales is not None:
        financials.sales = convert_to_excel_unit(financials.sales, source_mult, excel_mult)
    if financials.gross_profit is not None:
        financials.gross_profit = convert_to_excel_unit(financials.gross_profit, source_mult, excel_mult)
    if financials.operating_profit is not None:
        financials.operating_profit = convert_to_excel_unit(financials.operating_profit, source_mult, excel_mult)

    logger.info(
        f"[処理] 抽出結果: 売上={financials.sales}, 粗利={financials.gross_profit}, "
        f"営利={financials.operating_profit} (単位変換: {financials.source_unit}→{config.excel_unit})"
    )
    if financials.field_sources:
        logger.info(f"[処理] field_sources={financials.field_sources}")
    logger.info(f"[処理] 年度={fiscal_year_end}, Q={financials.quarter}")

    # ⑤ DB書き込み（数値のみ — メモ/notesは不変更）
    result = decision_db.upsert_quarterly_result(
        company_code=code,
        fiscal_year_end=fiscal_year_end,
        quarter=financials.quarter,
        sales=financials.sales,
        gross_profit=financials.gross_profit,
        operating_profit=financials.operating_profit,
        actor="tdnet", source="tdnet",
        tdnet_disclosure_id=disclosure_id,
        run_id=run_id,
        field_sources=financials.field_sources or None,
    )
    decision_db.commit()

    # 遅延ログ
    latency = _calc_latency_sec(item.published_at)
    latency_str = f" (遅延: {latency:.1f}秒)" if latency is not None else ""

    # ⑥ StateDB記録
    state_db.record(
        disclosure_id=disclosure_id, code=code,
        year=financials.fiscal_year, quarter=financials.quarter,
        status=Status.SUCCESS,
        new_values={
            "sales": financials.sales,
            "gross_profit": financials.gross_profit,
            "operating_profit": financials.operating_profit,
        },
    )

    logger.info(
        f"[処理] ✅ {result}: {code} {fiscal_year_end} {financials.quarter}{latency_str}"
    )


# ------------------------------------------------------------------
# DB書き込み: 自社株買い
# ------------------------------------------------------------------
def _process_buyback(
    item,
    doc_path: str,
    config: Config,
    state_db: StateDB,
    run_id: str,
) -> None:
    """自社株買い開示の処理 → event_pipeline に委譲"""
    disclosure_id = item.disclosure_id
    code = item.ticker

    try:
        from src.events.common_models import DocumentMeta
        from src.events.event_pipeline import process_documents

        doc_meta = DocumentMeta(
            doc_id=disclosure_id,
            ticker=code,
            company_name=item.company_name,
            title=item.title,
            disclosure_datetime=item.published_at,
            doc_url=item.doc_url,
            pdf_path=doc_path,
        )

        webhook_url = getattr(config, "discord_webhook_url", "") or ""
        db_path = config.decision_db_path

        result = process_documents(
            docs=[doc_meta],
            db_path=db_path,
            dry_run=False,
            event_types={"buyback"},
            webhook_url=webhook_url,
        )

        logger.info(
            f"[処理] buyback pipeline: "
            f"detected={result.detected} saved={result.saved} "
            f"notified={result.notified} errors={result.errors} "
            f"supabase_saved={result.supabase_saved}"
        )

        final_status = Status.SUCCESS if result.saved > 0 else Status.PARSE_FAILED
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=final_status,
            error_detail=f"buyback: detected={result.detected} saved={result.saved}",
        )

    except Exception as e:
        logger.error(f"[処理] buyback pipeline failed ({code}): {e}", exc_info=True)
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=Status.PARSE_FAILED,
            error_detail=f"buyback exception: {e}",
        )


# ------------------------------------------------------------------
# DB書き込み: 予想修正
# ------------------------------------------------------------------
def _process_forecast_revision(
    item,
    doc_path: str,
    config: Config,
    state_db: StateDB,
    decision_db: MigrationDB,
    run_id: str,
) -> None:
    """予想修正・差異開示の処理 → DB書き込み"""
    disclosure_id = item.disclosure_id
    code = item.ticker

    # ③ 複数ターゲット抽出
    targets = extract_forecast_targets(pdf_path=doc_path, title=item.title)

    if not targets:
        logger.error(f"[処理] 予想修正の数値抽出に失敗")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=Status.PARSE_FAILED,
            error_detail="予想修正のターゲットを抽出できませんでした",
        )
        return

    logger.info(f"[処理] {len(targets)}件のターゲットを検出")

    success_count = 0
    error_count = 0

    for i, target in enumerate(targets):
        logger.info(
            f"[INFO] forecast_revision "
            f"FY={target.fiscal_year} Q={target.quarter} "
            f"sales={target.sales} op_profit={target.operating_profit} "
            f"source={target.source}"
        )

        if not target.fiscal_year:
            logger.error(f"[ERROR] forecast_revision_parse_failed reason=年度不明 (target {i+1})")
            error_count += 1
            continue

        if not target.quarter:
            logger.error(f"[ERROR] forecast_revision_parse_failed reason=Q不明 (target {i+1})")
            error_count += 1
            continue

        if target.sales is None and target.operating_profit is None:
            logger.error(
                f"[ERROR] forecast_revision_parse_failed "
                f"reason=売上高・営業利益ともに抽出不可 (target {i+1})"
            )
            error_count += 1
            continue

        # R表記 → YYYY-MM-DD
        fiscal_year_end = _reiwa_to_fiscal_year_end(target.fiscal_year)
        if fiscal_year_end is None:
            logger.error(f"[ERROR] 年度変換失敗: {target.fiscal_year}")
            error_count += 1
            continue

        # 単位変換
        source_mult = parse_scale_unit(target.source_unit) if target.source_unit else 1
        excel_mult = excel_unit_multiplier(config.excel_unit)

        converted_sales = None
        if target.sales is not None:
            converted_sales = convert_to_excel_unit(target.sales, source_mult, excel_mult)

        converted_op = None
        if target.operating_profit is not None:
            converted_op = convert_to_excel_unit(target.operating_profit, source_mult, excel_mult)

        converted_gp = None
        if target.gross_profit is not None:
            converted_gp = convert_to_excel_unit(target.gross_profit, source_mult, excel_mult)

        logger.info(
            f"[処理] ターゲット{i+1}: {fiscal_year_end} {target.quarter} "
            f"売上={converted_sales} 営利={converted_op} "
            f"(単位変換: {target.source_unit}→{config.excel_unit})"
        )

        # DB書き込み（数値のみ）
        result = decision_db.upsert_quarterly_result(
            company_code=code,
            fiscal_year_end=fiscal_year_end,
            quarter=target.quarter,
            sales=converted_sales,
            gross_profit=converted_gp,
            operating_profit=converted_op,
            actor="tdnet", source="tdnet",
            tdnet_disclosure_id=disclosure_id,
            run_id=run_id,
        )
        decision_db.commit()

        if result in ("inserted", "updated"):
            logger.info(f"[処理] ✅ ターゲット{i+1} {result}")
            success_count += 1
        else:
            logger.info(f"[処理] ターゲット{i+1} {result}（変更なし）")

    # StateDB記録
    final_status = Status.SUCCESS if success_count > 0 else Status.PARSE_FAILED
    state_db.record(
        disclosure_id=disclosure_id, code=code,
        year=targets[0].fiscal_year if targets else "",
        quarter=",".join(t.quarter for t in targets),
        status=final_status,
        error_detail=f"targets={len(targets)}, success={success_count}, error={error_count}",
    )


# ------------------------------------------------------------------
# 開示ルーター
# ------------------------------------------------------------------
def process_disclosure(
    item,
    config: Config,
    state_db: StateDB,
    decision_db: MigrationDB,
    run_id: str,
) -> None:
    """単一の開示を処理する"""
    disclosure_id = item.disclosure_id
    code = item.ticker

    # 冪等性チェック（二重処理禁止）
    if state_db.is_processed(disclosure_id):
        logger.info(f"[処理] {code} {item.title} — 処理済み、スキップ")
        return

    logger.info(f"[処理] === {code} {item.company_name} ===")
    logger.info(f"[処理] タイトル: {item.title}")
    logger.info(f"[処理] タイプ: {item.disclosure_type}")
    logger.info(f"[処理] URL: {item.doc_url}")

    # ① PDFダウンロード
    docs_dir = str(Path(config.state_db_path).parent / "docs")
    doc_path = download_document(item.doc_url, docs_dir)

    if doc_path is None:
        logger.error(f"[処理] ダウンロード失敗")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=Status.DOWNLOAD_FAILED,
            error_detail="ドキュメントのダウンロードに失敗",
        )
        return

    # ② 開示タイプに応じた処理分岐
    if item.disclosure_type == DisclosureType.FORECAST_REVISION:
        _process_forecast_revision(item, doc_path, config, state_db, decision_db, run_id)
    elif item.disclosure_type == DisclosureType.FINANCIAL_STATEMENT:
        xbrl_path = None
        if item.xbrl_url:
            xbrl_path = download_document(item.xbrl_url, docs_dir)
        _process_financial_statement(item, doc_path, xbrl_path, config, state_db, decision_db, run_id)
    elif item.disclosure_type == DisclosureType.BUYBACK:
        # buyback は event_pipeline に委譲する
        _process_buyback(item, doc_path, config, state_db, run_id)
    else:
        logger.warning(f"[処理] 不明な開示タイプ: {item.disclosure_type}")
        state_db.record(
            disclosure_id=disclosure_id, code=code,
            year="", quarter="",
            status=Status.PARSE_FAILED,
            error_detail=f"不明な開示タイプ: {item.disclosure_type}",
        )


# ------------------------------------------------------------------
# メインループ
# ------------------------------------------------------------------
def main_loop(config: Config, state_db: StateDB, decision_db: MigrationDB) -> None:
    """メインポーリングループ"""
    logger.info("=" * 60)
    logger.info("TDnet決算DB自動更新システム 起動（Phase2）")
    logger.info(f"  決算DB: {config.decision_db_path}")
    logger.info(f"  ポーリング間隔: {config.poll_interval_sec}秒")
    logger.info(f"  ウォッチリスト: {config.watch_tickers or '全銘柄'}")
    logger.info("=" * 60)

    while True:
        try:
            # run_id: 1ポーリングサイクル = 1 run
            run_id = f"tdnet-{uuid.uuid4().hex[:8]}"

            # 新着開示を取得
            items = fetch_new_disclosures(
                watch_tickers=config.watch_tickers,
                is_processed_fn=state_db.is_processed,
            )

            if items:
                logger.info(f"[メイン] 新規開示 {len(items)}件 を処理します (run={run_id})")
                for item in items:
                    try:
                        process_disclosure(item, config, state_db, decision_db, run_id)
                    except Exception as e:
                        logger.error(f"[メイン] 処理エラー ({item.ticker}): {e}", exc_info=True)
                        state_db.record(
                            disclosure_id=item.disclosure_id,
                            code=item.ticker,
                            year="", quarter="",
                            status=Status.PARSE_FAILED,
                            error_detail=f"予期しないエラー: {e}",
                        )
            else:
                logger.debug("[メイン] 新規開示なし")

        except KeyboardInterrupt:
            logger.info("[メイン] Ctrl+C を検出、安全停止します")
            break
        except Exception as e:
            logger.error(f"[メイン] ポーリングエラー: {e}", exc_info=True)

        # 次のポーリングまで待機
        try:
            logger.debug(f"[メイン] {config.poll_interval_sec}秒待機...")
            time.sleep(config.poll_interval_sec)
        except KeyboardInterrupt:
            logger.info("[メイン] Ctrl+C を検出、安全停止します")
            break


def main():
    """エントリーポイント"""
    global logger

    # ── playground ガード ──
    if "playground" in os.getcwd().lower():
        raise RuntimeError(
            "playground から実行されました。OneDrive 側から実行してください。"
        )

    # 設定読み込み
    config_path = None
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    config = load_config(config_path)

    # ロガーセットアップ
    logger = setup_logger(config.log_path)

    # DB初期化
    state_db = StateDB(config.state_db_path)
    decision_db = MigrationDB(config.decision_db_path)

    try:
        main_loop(config, state_db, decision_db)
    finally:
        decision_db.close()
        state_db.close()
        logger.info("[メイン] 終了")


if __name__ == "__main__":
    main()
