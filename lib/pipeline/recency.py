"""lib/pipeline/recency.py -- recency_key / winner 判定ロジック"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .source_priority import get_priority

# ============================================================
# recency_key 生成
# ============================================================
# 勝者判定順:
#   1. source_priority ASC  (低い数値 = 高優先)
#   2. correction_flag DESC (True > False)
#   3. disclosure_datetime DESC
#   4. updated_at DESC
#
# recency_key は文字列ソートで最大値 = 勝者 となるように設計。
# フォーマット: "{inverted_priority:02d}_{correction}_{disclosure_dt}_{updated_at}"


def make_recency_key(
    source: str,
    correction_flag: bool = False,
    disclosure_datetime: str | datetime | None = None,
    updated_at: str | datetime | None = None,
) -> str:
    """recency_key を生成。文字列比較で最大 = 最優先。"""
    # source_priority をインバート (99 - priority) して大きい = 高優先にする
    priority = get_priority(source)
    inverted = 99 - priority

    # correction_flag: True → "1", False → "0"
    corr = "1" if correction_flag else "0"

    # datetime → ISO 文字列
    def _dt_str(v: Any) -> str:
        if v is None:
            return "0000-00-00T00:00:00"
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)

    disc_dt = _dt_str(disclosure_datetime)
    upd_at = _dt_str(updated_at)

    return f"{inverted:02d}_{corr}_{disc_dt}_{upd_at}"


def pick_winner(rows: list[dict], *, recency_key_field: str = "recency_key") -> dict | None:
    """recency_key が最大の行を返す。"""
    if not rows:
        return None
    return max(rows, key=lambda r: r.get(recency_key_field, ""))


def compare_recency(key_a: str, key_b: str) -> int:
    """key_a と key_b を比較。正なら a が勝者、負なら b が勝者、0 なら同等。"""
    if key_a > key_b:
        return 1
    elif key_a < key_b:
        return -1
    return 0
