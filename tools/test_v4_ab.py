#!/usr/bin/env python3
"""
test_v4_ab.py — [V4_AB] ログ出力の手動確認スクリプト

目的:
    _process_single() をキャッシュ済みPDFで直接実行し、
    [V4_AB] ログが出ることを確認する。

使い方:
    .venv\\Scripts\\python tools\\test_v4_ab.py
    .venv\\Scripts\\python tools\\test_v4_ab.py --pdf data/セグメントサンプル20件/6264マルマエ.pdf
    .venv\\Scripts\\python tools\\test_v4_ab.py --pdf data/セグメントサンプル20件/7826フルヤ金属.pdf

注意:
    - DB書き込みはテンポラリファイル（test_v4_ab_tmp.db）に行う
    - 終了後にテンポラリDBを削除する
    - 本番の decision_db.db には一切触れない
    - extract_financials は成功固定のモックに差し替え済み（PL抽出失敗でも必ずセグメントブロックまで進む）
"""
from __future__ import annotations

import logging
import os
import sys
import argparse
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_v4_ab")

JST = timezone(timedelta(hours=9))



def _make_mock_item(pdf_path: str, ticker: str = "6264") -> SimpleNamespace:
    """_process_single() が要求する item オブジェクトのモック。"""
    return SimpleNamespace(
        disclosure_id=f"test_{ticker}_{datetime.now(JST).strftime('%H%M%S')}",
        ticker=ticker,
        company_name=f"テスト企業_{ticker}",
        title="連結決算短信〈日本基準〉（通期）",
        doc_url=pdf_path,   # downloader モックがそのまま返す
        xbrl_url=None,
        published_at=datetime.now(JST).isoformat(),
        disclosure_type=None,
    )


def _make_mock_financials() -> SimpleNamespace:
    """extract_financials() の成功固定モック戻り値。

    _process_single() が参照する全フィールドを網羅する。
    fiscal_year は _reiwa_to_fiscal_year_end() が変換できる "R6/03" 形式。
    """
    return SimpleNamespace(
        fiscal_year="R6/03",
        quarter="4Q",
        sales=100_000,
        gross_profit=30_000,
        operating_profit=10_000,
        source_unit="百万円",
        field_sources=None,
    )


def run(pdf_path: str, ticker: str) -> None:
    # ① pdf 絶対パス化
    pdf_path = str(Path(pdf_path).resolve())

    tmp_db = os.path.join(_PROJECT_ROOT, "test_v4_ab_tmp.db")
    tmp_state_db = os.path.join(_PROJECT_ROOT, "test_v4_ab_state_tmp.db")

    # 前回分が残っていれば削除
    for f in (tmp_db, tmp_state_db):
        if os.path.exists(f):
            os.remove(f)

    state_db = None
    decision_db = None

    try:
        from src.config import Config
        from src.db import StateDB
        from src.migration.migration_db import MigrationDB
        from tools.tdnet_ingest import _process_single

        config = Config()
        state_db = StateDB(tmp_state_db)
        decision_db = MigrationDB(tmp_db)

        item = _make_mock_item(pdf_path, ticker)
        run_id = "test_v4_ab_run"

        logger.info("=" * 55)
        logger.info(f"  TEST START ticker={ticker}")
        logger.info(f"  pdf={pdf_path}")
        logger.info("=" * 55)

        # ② download_document モック: ローカルPDFをそのまま返す
        import src.downloader as _dl
        _orig_download = _dl.download_document

        def _mock_download(url, *args, **kwargs):
            if os.path.isfile(url):
                return url
            return _orig_download(url, *args, **kwargs)

        _dl.download_document = _mock_download

        # ③ extract_financials モック: PL失敗を完全回避し、セグメントブロックまで進める
        import src.extractor as _ex
        _orig_extract = _ex.extract_financials

        def _mock_extract_financials(*args, **kwargs):
            logger.info("[MOCK] extract_financials → 成功固定モックを返します")
            return _make_mock_financials(), None

        _ex.extract_financials = _mock_extract_financials
        # tdnet_ingest が直接 from src.extractor import extract_financials している場合のパッチ
        import tools.tdnet_ingest as _ti
        _orig_ti_extract = getattr(_ti, "extract_financials", None)
        _ti.extract_financials = _mock_extract_financials  # type: ignore[attr-defined]

        try:
            result = _process_single(
                item,
                config,
                state_db,
                decision_db,
                run_id,
                dry_run=False,  # dry_run=True だと L289 で早期リターンしセグメントに届かない
                dump_dir=None,
            )
        finally:
            # モンキーパッチ復元
            _dl.download_document = _orig_download
            _ex.extract_financials = _orig_extract
            if _orig_ti_extract is not None:
                _ti.extract_financials = _orig_ti_extract  # type: ignore[attr-defined]

        logger.info("=" * 55)
        logger.info(f"  TEST RESULT: {result}")
        logger.info("=" * 55)

    finally:
        # ④ DB close を明示してからファイル削除
        try:
            if decision_db is not None:
                decision_db.close()
        except Exception:
            pass
        try:
            if state_db is not None:
                state_db.close()
        except Exception:
            pass

        for f in (tmp_db, tmp_state_db):
            try:
                if os.path.exists(f):
                    os.remove(f)
                    logger.info(f"  Cleaned up: {f}")
            except Exception as e:
                logger.warning(f"  Cleanup failed ({f}): {e}")


def main():
    parser = argparse.ArgumentParser(description="[V4_AB] ログ確認テスト")
    parser.add_argument(
        "--pdf",
        default=os.path.join(
            _PROJECT_ROOT, "data", "セグメントサンプル20件", "6264マルマエ.pdf"
        ),
        help="テスト用PDFパス（相対パス可）",
    )
    parser.add_argument("--ticker", default="6264", help="ティッカーコード")
    args = parser.parse_args()

    # ① main でも絶対パス化（エラーメッセージを分かりやすくするため）
    pdf_abs = str(Path(args.pdf).resolve())
    if not os.path.isfile(pdf_abs):
        print(f"[ERROR] PDFが見つかりません: {pdf_abs}")
        sys.exit(1)

    run(pdf_abs, args.ticker)


if __name__ == "__main__":
    main()

