"""persist_policy.py — 中間データ保存ポリシーの一元管理

通常運用では中間データ（migration_log / quarantine / extracted_facts 等）を
SQLite に永続保存しない。debug 時のみ明示的に保存を許可する。

使用方法:
    from src.persist_policy import should_persist_intermediates

    if should_persist_intermediates():
        db.insert_log(...)  # debug モード時のみ実行
"""
from __future__ import annotations

import os
from typing import Mapping

# 環境変数名
_ENV_KEY = "TDNET_PERSIST_INTERMEDIATES"

# 有効値
_TRUTHY = {"1", "true", "yes", "on"}


def resolve_persist_intermediates(
    cli_flag: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """中間データ保存の可否を解決する。

    優先順位:
        1. cli_flag が明示されていればそれに従う
        2. 環境変数 TDNET_PERSIST_INTERMEDIATES を参照
        3. デフォルト OFF

    Args:
        cli_flag: CLI の --persist-intermediates / --no-persist-intermediates
        env: 環境変数の辞書（テスト用に差し替え可能）

    Returns:
        True = 保存する, False = 保存しない
    """
    # 1. CLI フラグ最優先
    if cli_flag is not None:
        return cli_flag

    # 2. 環境変数
    if env is None:
        env = os.environ
    val = env.get(_ENV_KEY, "").strip().lower()
    if val in _TRUTHY:
        return True

    # 3. デフォルト OFF
    return False


# グローバル状態（プロセス起動時に1回だけ設定）
_global_persist: bool | None = None


def init_persist_policy(cli_flag: bool | None = None) -> bool:
    """プロセス起動時にポリシーを初期化する。

    Returns:
        解決された persist_intermediates 値
    """
    global _global_persist
    _global_persist = resolve_persist_intermediates(cli_flag=cli_flag)
    return _global_persist


def should_persist_intermediates() -> bool:
    """中間データを保存すべきかどうかを返す。

    init_persist_policy() が未呼出の場合はデフォルト値（OFF）で解決する。
    """
    global _global_persist
    if _global_persist is None:
        _global_persist = resolve_persist_intermediates()
    return _global_persist


def reset_persist_policy() -> None:
    """テスト用: グローバル状態をリセットする。"""
    global _global_persist
    _global_persist = None
