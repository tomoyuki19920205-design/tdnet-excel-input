# ============================================================
# parse_models.py — Excelパーサー用データクラス
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SegmentPair:
    """セグメント1ペア分（売上＋利益）"""
    segment_name: str          # セグメント名（欠損時 UNKNOWN_1 等）
    segment_order: int         # Excel列順（0始まり）
    segment_sales: float | None = None
    segment_profit: float | None = None


@dataclass
class QuarterlyRecord:
    """1四半期分の業績レコード"""
    company_code: str
    fiscal_year_end: str       # YYYY-MM-DD（末日）
    quarter: str               # 1Q / 2Q / 3Q / 4Q
    row_number: int            # Excel上の行番号

    # PL数値（O〜S列）
    sales: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None
    sga: float | None = None
    operating_profit: float | None = None

    # Z列メモ
    note: str | None = None

    # セグメント（AA列〜）
    segments: list[SegmentPair] = field(default_factory=list)


@dataclass
class CompanyBlock:
    """企業ブロック（1社分）"""
    company_code: str
    row_start: int             # ブロック開始行番号
    row_end: int               # ブロック終了行番号

    # C〜L列 補助メモ
    memo_c: str | None = None
    memo_d: str | None = None
    memo_e: str | None = None
    memo_f: str | None = None
    memo_g: str | None = None
    memo_h: str | None = None
    memo_i: str | None = None
    memo_j: str | None = None
    memo_k: str | None = None
    memo_l: str | None = None

    records: list[QuarterlyRecord] = field(default_factory=list)


@dataclass
class LogEntry:
    """ログエントリ"""
    log_level: str             # ERROR / SKIP / WARN
    log_type: str              # SKIP_DISTANCE, SKIP_BLOCK_NO_CODE, etc.
    message: str
    sheet_name: str = ""
    row_start: int | None = None
    row_end: int | None = None
    company_code: str | None = None
    fiscal_year: str | None = None
    quarter: str | None = None


@dataclass
class ParseResult:
    """Excel全体パース結果"""
    blocks: list[CompanyBlock] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    total_rows_scanned: int = 0
