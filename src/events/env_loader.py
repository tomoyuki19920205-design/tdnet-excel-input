#!/usr/bin/env python3
"""env_loader.py — プロジェクト共通の環境変数読み込みユーティリティ

プロジェクトルートの .env を python-dotenv で読み込み、
全 CLI / モジュールが統一的に環境変数へアクセスできるようにする。

使い方:
    from src.events.env_loader import load_project_env, require_env

    load_project_env()                        # プロジェクトルートの .env を読み込み
    api_key = require_env("OPENAI_API_KEY")   # 必須変数を取得（未設定なら例外）
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("env_loader")

# src/events/env_loader.py → parent.parent.parent = project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

_loaded = False


def load_project_env(*, override: bool = False) -> Path | None:
    """プロジェクトルートの .env を読み込む。

    - python-dotenv を使用し、引用符・コメント・空行を正しく処理する
    - __file__ 基準でパスを解決するため cwd に依存しない
    - 2回目以降の呼び出しは何もしない（冪等）

    Parameters
    ----------
    override : bool
        True の場合、既存の環境変数を .env の値で上書きする

    Returns
    -------
    Path | None
        読み込んだ .env のパス。見つからなければ None。
    """
    global _loaded
    if _loaded and not override:
        return _ENV_PATH if _ENV_PATH.exists() else None

    if not _ENV_PATH.exists():
        logger.warning(f"[ENV] .env not found: {_ENV_PATH}")
        _loaded = True
        return None

    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(_ENV_PATH), override=override)
        logger.debug(f"[ENV] loaded .env from {_ENV_PATH}")
    except ImportError:
        # python-dotenv がない場合のフォールバック（最小限パーサ）
        logger.warning("[ENV] python-dotenv not installed, using fallback parser")
        _fallback_load(override=override)

    _loaded = True
    return _ENV_PATH


def _fallback_load(*, override: bool = False) -> None:
    """python-dotenv がない環境用の最小限パーサ"""
    with open(_ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            # 引用符を除去
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            # 空値はスキップ（空の OPENAI_API_KEY= をセットしない）
            if not val:
                continue
            if override:
                os.environ[key] = val
            else:
                os.environ.setdefault(key, val)


def require_env(key: str, *, purpose: str = "") -> str:
    """必須の環境変数を取得する。未設定なら詳細なエラーを表示して終了する。

    Parameters
    ----------
    key : str
        環境変数名
    purpose : str
        何に使う変数なのかの説明（エラーメッセージ用）

    Returns
    -------
    str
        設定値（空でない文字列）

    Raises
    ------
    SystemExit
        環境変数が未設定または空の場合
    """
    val = os.environ.get(key, "").strip()
    if val:
        return val

    # 詳細なエラーメッセージ
    env_exists = _ENV_PATH.exists()
    env_has_key = False
    if env_exists:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    rhs = stripped.split("=", 1)[1].strip()
                    if rhs and rhs not in ("''", '""'):
                        env_has_key = True
                    break

    msg_parts = [
        f"[ERROR] {key} が未設定です。",
    ]
    if purpose:
        msg_parts.append(f"  用途: {purpose}")
    msg_parts.append(f"  .env 探索先: {_ENV_PATH}")
    msg_parts.append(f"  .env 存在  : {'あり' if env_exists else 'なし'}")
    if env_exists and not env_has_key:
        msg_parts.append(f"  状態: .env に {key}= の行はありますが、値が空です。値を設定してください。")
    elif not env_exists:
        msg_parts.append(f"  対処: {_ENV_PATH} を作成し、{key}=<値> を記述してください。")

    print("\n".join(msg_parts))
    raise SystemExit(1)


def get_project_root() -> Path:
    """プロジェクトルートのパスを返す"""
    return _PROJECT_ROOT
