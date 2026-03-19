# ============================================================
# utils.py — ユーティリティ関数
# ============================================================
from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# JST タイムゾーン
JST = timezone(timedelta(hours=9))


def sha256(text: str) -> str:
    """文字列のSHA256ハッシュを返す（重複排除キー）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_jst() -> datetime:
    """現在時刻（JST）"""
    return datetime.now(JST)


def now_jst_str() -> str:
    """現在時刻（JST）のISO形式文字列"""
    return now_jst().strftime("%Y-%m-%d %H:%M:%S")


def today_yyyymmdd() -> str:
    """今日の日付をYYYYMMDD形式で返す（JST）"""
    return now_jst().strftime("%Y%m%d")


def normalize_number(raw: str) -> int | None:
    """
    数値文字列を正規化して整数に変換する。

    対応パターン:
    - "1,234,567" → 1234567
    - "△1,234" / "▲1,234" → -1234
    - "(1,234)" / "（1,234）" → -1234
    - "-1,234" / "－1,234" → -1234
    - 全角数字 → 半角数字
    - None/空/"—"/"－" → None
    """
    if raw is None:
        return None

    s = str(raw).strip()
    if not s or s in ("—", "－", "-", "―", "‐"):
        return None

    # 全角数字→半角数字
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    # マイナス判定
    negative = False
    if s.startswith("△") or s.startswith("▲"):
        negative = True
        s = s[1:]
    elif s.startswith("(") or s.startswith("（"):
        negative = True
        s = s.lstrip("(（").rstrip(")）")
    elif s.startswith("-") or s.startswith("－") or s.startswith("‐"):
        negative = True
        s = s[1:]

    # カンマ・スペース除去
    s = re.sub(r"[,、\s]", "", s)
    # 全角ドット→半角
    s = s.replace("．", ".")

    try:
        num = float(s)
    except ValueError:
        return None

    # 四捨五入（Pythonのround()はbanker's roundingなのでmath.floorを使用）
    import math
    result = int(math.floor(num + 0.5))
    return -result if negative else result


def parse_scale_unit(text: str) -> int:
    """
    単位文字列 → 乗数
    "百万円" → 1_000_000, "億円" → 100_000_000, "千円" → 1_000
    """
    if "億" in text:
        return 100_000_000
    if "百万" in text:
        return 1_000_000
    if "千" in text:
        return 1_000
    return 1


def excel_unit_multiplier(unit: str) -> int:
    """config の excel_unit → 乗数"""
    mapping = {
        "million_yen": 1_000_000,
        "thousand_yen": 1_000,
        "yen": 1,
    }
    return mapping.get(unit, 1_000_000)


def convert_to_excel_unit(value: int, source_multiplier: int, excel_multiplier: int) -> int:
    """
    書類単位の値をExcel単位に変換する。

    例: 値12345（百万円）→ Excel単位が百万円ならそのまま12345
    例: 値12345000（千円）→ Excel単位が百万円なら12345
    """
    # まず円に換算してからExcel単位に変換
    yen_value = value * source_multiplier
    return int(round(yen_value / excel_multiplier))


def setup_logger(log_path: str, name: str = "tdnet") -> logging.Logger:
    """ファイル + コンソール二重出力のロガーをセットアップ"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # 既にセットアップ済み

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # コンソール出力
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # ファイル出力
    log_dir = Path(log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
