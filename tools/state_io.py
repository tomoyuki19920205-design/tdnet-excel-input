#!/usr/bin/env python3
# ============================================================
# tools/state_io.py — 安全な JSON state ファイル I/O
# ============================================================
"""
JSON state ファイルの atomic write / safe load ユーティリティ。

使い方::

    from tools.state_io import atomic_json_save, safe_json_load

    data = safe_json_load(Path("state/my_state.json"), default={"items": []})
    data["items"].append("new")
    atomic_json_save(Path("state/my_state.json"), data)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def atomic_json_save(filepath: Path, data: Any) -> None:
    """同一ディレクトリ内に一意な tmp を作り → flush → fsync → replace。

    同じ保存先に対して複数プロセスが同時に走っても tmp 名が衝突しない。
    例外発生時は tmp ファイルを cleanup する（BaseException で広く捕まえるのは
    KeyboardInterrupt 等でも確実に孤立 tmp を残さないため）。
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=filepath.parent,
        suffix=".tmp",
        prefix=filepath.stem + "_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # replace() は同一ファイルシステム上で atomic
        Path(tmp_path).replace(filepath)
    except BaseException:
        # cleanup: KeyboardInterrupt 含め必ず孤立 tmp を消す
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def safe_json_load(filepath: Path, default: Any = None) -> Any:
    """JSON を安全に読み込む。

    - ファイルが存在しない → default を返す
    - JSON として壊れている → .corrupt.YYYYMMDD_HHMMSS.json に退避して default を返す
    - default 未指定時は {} を返す

    呼び出し側が期待する初期値（dict, list, etc.）をそのまま default に渡せる。
    """
    filepath = Path(filepath)

    if not filepath.exists():
        return default if default is not None else {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("JSON読み込み失敗 (%s): %s", filepath, e)

        # 壊れたファイルを退避して原因追跡を可能にする
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        corrupt = filepath.with_suffix(f".corrupt.{ts}.json")
        try:
            filepath.rename(corrupt)
            log.warning("壊れたファイルを退避: %s → %s", filepath, corrupt)
        except OSError as rename_err:
            log.warning("corrupt退避失敗 (%s): %s", filepath, rename_err)

        return default if default is not None else {}
