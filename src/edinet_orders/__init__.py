# src/edinet_orders/__init__.py
"""
edinet_orders — EDINET有価証券報告書から受注データを抽出・DB保存するモジュール

使用例:
    from src.edinet_orders import extract, save_to_db

    results = extract(survey_data)                  # 抽出のみ
    stats   = save_to_db(results, survey_data)      # DB保存のみ
"""
from .extractor import extract_from_company, extract  # noqa: F401
from .transformer import transform_to_db_row           # noqa: F401
from .saver import save_to_db                          # noqa: F401

__all__ = ["extract_from_company", "extract", "transform_to_db_row", "save_to_db"]
