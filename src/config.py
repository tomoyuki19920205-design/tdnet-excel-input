# ============================================================
# config.py — YAML設定読み込み
# ============================================================
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ColumnMapping:
    """Excel列マッピング"""
    code: str = "A"
    fiscal_term: str = "M"
    quarter: str = "N"
    sales: str = "O"
    gross_profit: str = "P"
    operating_profit: str = "S"


@dataclass
class Config:
    """アプリケーション設定"""
    excel_path: str = ""
    sheet_name: str = "PL"
    poll_interval_sec: int = 180
    max_scan_rows: int = 150
    q_search_up: int = 20
    q_search_down: int = 40
    excel_unit: str = "million_yen"
    log_path: str = "data/app.log"
    state_db_path: str = "data/state.db"
    decision_db_path: str = "decision_db.db"
    retry_count: int = 5
    columns: ColumnMapping = field(default_factory=ColumnMapping)
    watch_tickers: list[str] = field(default_factory=list)


def load_config(config_path: str | None = None) -> Config:
    """YAML設定ファイルを読み込み、Configオブジェクトを返す。"""
    if config_path is None:
        # プロジェクトルートから config.yaml を探す
        project_root = Path(__file__).resolve().parent.parent
        config_path = str(project_root / "config.yaml")

    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] 設定ファイルが見つかりません: {path}", file=sys.stderr)
        print(f"  config.yaml.example を config.yaml にコピーして編集してください。", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()

    # 基本設定
    cfg.excel_path = raw.get("excel_path", cfg.excel_path)
    cfg.sheet_name = raw.get("sheet_name", cfg.sheet_name)
    cfg.poll_interval_sec = int(raw.get("poll_interval_sec", cfg.poll_interval_sec))
    cfg.max_scan_rows = int(raw.get("max_scan_rows", cfg.max_scan_rows))
    cfg.q_search_up = int(raw.get("q_search_up", cfg.q_search_up))
    cfg.q_search_down = int(raw.get("q_search_down", cfg.q_search_down))
    cfg.excel_unit = raw.get("excel_unit", cfg.excel_unit)
    cfg.retry_count = int(raw.get("retry_count", cfg.retry_count))

    # パスの解決（configファイルからの相対パスをプロジェクトルートからの相対パスに）
    project_root = Path(config_path).resolve().parent
    cfg.log_path = str(project_root / raw.get("log_path", cfg.log_path))
    cfg.state_db_path = str(project_root / raw.get("state_db_path", cfg.state_db_path))

    # decision_db_path
    ddb = raw.get("decision_db_path", cfg.decision_db_path)
    if ddb and not os.path.isabs(ddb):
        cfg.decision_db_path = str(project_root / ddb)
    else:
        cfg.decision_db_path = ddb

    # excel_path は絶対パスならそのまま、相対ならプロジェクトルートから
    excel_p = raw.get("excel_path", cfg.excel_path)
    if excel_p and not os.path.isabs(excel_p):
        cfg.excel_path = str(project_root / excel_p)
    else:
        cfg.excel_path = excel_p or ""

    # 列マッピング
    cols_raw = raw.get("columns", {})
    if cols_raw:
        cfg.columns = ColumnMapping(
            code=cols_raw.get("code", "A"),
            fiscal_term=cols_raw.get("fiscal_term", "M"),
            quarter=cols_raw.get("quarter", "N"),
            sales=cols_raw.get("sales", "O"),
            gross_profit=cols_raw.get("gross_profit", "P"),
            operating_profit=cols_raw.get("operating_profit", "S"),
        )

    # ウォッチリスト
    wt = raw.get("watch_tickers", [])
    cfg.watch_tickers = [str(t) for t in wt] if wt else []

    return cfg
