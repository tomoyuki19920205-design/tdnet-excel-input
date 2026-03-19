# ============================================================
# candidate_models.py — 抽出エンジン 中間表現 & 候補モデル
# ============================================================
"""
完成版アーキテクチャの中間表現 (Intermediate Representation) と
候補モデル (Candidate) を定義する。

設計思想:
  - PDF/HTML/XBRL の差異を吸収する共通中間表現
  - 候補に provenance (出自追跡) を持たせる
  - スコアベースで候補を選択できる構造
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================
# 抽出ステージ (Stage-aware Quarantine 用)
# ============================================================

class ExtractionStage(str, Enum):
    """抽出処理のステージ。quarantine でどの段階で失敗したかを記録する。"""
    SOURCE_LOAD = "source_load"
    STRUCTURAL_PARSE = "structural_parse"
    CANDIDATE_DETECT = "candidate_detect"
    SEMANTIC_INTERPRET = "semantic_interpret"
    RECORD_BUILD = "record_build"


# ============================================================
# ソースタイプ
# ============================================================

class SourceType(str, Enum):
    PDF = "pdf"
    HTML = "html"
    XBRL = "xbrl"
    IXBRL = "ixbrl"
    ZIP = "zip"
    UNKNOWN = "unknown"


# ============================================================
# 候補テーブルタイプ
# ============================================================

class CandidateTableType(str, Enum):
    SEGMENT = "segment"
    ORDER = "order"
    PL = "pl"
    FORECAST = "forecast"
    UNKNOWN = "unknown"


# ============================================================
# Provenance (出自追跡)
# ============================================================

@dataclass
class Provenance:
    """
    抽出結果の出自を追跡するための情報。
    どのソースの、どのページ/表/行/列から、
    どのルールで、どのスコアで決まったかを記録する。
    """
    source_path: str = ""
    source_type: str = ""       # SourceType の値
    page_no: int | None = None
    table_no: int | None = None
    row_no: int | None = None
    col_no: int | None = None
    rule_trace: list[str] = field(default_factory=list)
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def add_rule(self, rule: str) -> None:
        """ルール適用履歴を追加"""
        self.rule_trace.append(rule)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_type": self.source_type,
            "page_no": self.page_no,
            "table_no": self.table_no,
            "row_no": self.row_no,
            "col_no": self.col_no,
            "rule_trace": self.rule_trace,
            "score_breakdown": self.score_breakdown,
        }


# ============================================================
# 共通中間表現 (Intermediate Representation)
# ============================================================

@dataclass
class ParsedCell:
    """表のセル1個"""
    text: str = ""
    row: int = 0
    col: int = 0
    rowspan: int = 1
    colspan: int = 1
    is_numeric: bool = False
    numeric_value: float | None = None


@dataclass
class ParsedRow:
    """表の行1行"""
    cells: list[ParsedCell] = field(default_factory=list)
    row_index: int = 0
    raw_text: str = ""

    @property
    def text_joined(self) -> str:
        """セルのテキストをスペース区切りで結合"""
        return " ".join(c.text for c in self.cells if c.text)


@dataclass
class ParsedTable:
    """PDF/HTML/XBRL から抽出した表の中間表現"""
    rows: list[ParsedRow] = field(default_factory=list)
    page_no: int | None = None
    table_no: int | None = None
    caption: str = ""
    nearby_text: str = ""       # 表の前後テキスト

    @property
    def header_rows(self) -> list[ParsedRow]:
        """先頭3行をヘッダー候補として返す"""
        return self.rows[:3] if len(self.rows) >= 3 else self.rows

    @property
    def data_rows(self) -> list[ParsedRow]:
        """ヘッダー以降のデータ行"""
        return self.rows[3:] if len(self.rows) > 3 else []


@dataclass
class ParsedFact:
    """XBRL fact の中間表現"""
    concept_name: str = ""      # e.g. "jppfs_cor:NetSales"
    concept_local: str = ""     # e.g. "NetSales"
    context_ref: str = ""
    value: str = ""
    numeric_value: float | None = None
    unit_ref: str = ""
    scale: str = ""
    sign: str = ""
    decimals: str = ""


# ============================================================
# 候補テーブル (Candidate)
# ============================================================

@dataclass
class CandidateTable:
    """
    セグメント表/受注表/PL表などの候補。
    スコアベースで選択される。
    """
    table_type: str = ""        # CandidateTableType の値
    score: float = 0.0
    parsed_table: ParsedTable | None = None
    provenance: Provenance = field(default_factory=Provenance)
    raw_lines: list[str] = field(default_factory=list)
    start_line: int = 0
    end_line: int = 0


@dataclass
class MetricCandidate:
    """
    数値候補。売上/利益/受注高などの個別メトリクス候補。
    """
    role: str = ""              # "sales" / "operating_profit" / "segment_profit" etc.
    value: float | None = None
    raw_text: str = ""
    score: float = 0.0
    provenance: Provenance = field(default_factory=Provenance)
