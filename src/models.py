# ============================================================
# models.py — データクラス定義
# ============================================================
from __future__ import annotations

from dataclasses import dataclass, field


# 開示タイプ定数
class DisclosureType:
    FORECAST_REVISION = "forecast_revision"
    FINANCIAL_STATEMENT = "financial_statement"
    DIVIDEND_REVISION = "dividend_revision"
    BUYBACK = "buyback"


@dataclass
class DisclosureItem:
    """TDnetから取得した開示アイテム"""
    disclosure_id: str        # doc_urlのSHA256
    ticker: str               # 企業コード（4桁）
    company_name: str
    title: str
    doc_url: str
    published_at: str
    xbrl_url: str | None = None
    disclosure_type: str = ""  # DisclosureType の値
    source_doc_id: str | None = None


@dataclass
class ForecastTarget:
    """予想修正・差異開示から抽出された1ターゲット分の数値"""
    fiscal_year: str = ""             # "R8/3"
    quarter: str = ""                 # "2Q", "4Q"
    sales: int | None = None          # 売上高
    operating_profit: int | None = None  # 営業利益
    gross_profit: int | None = None   # 粗利益（任意）
    source: str = ""                  # "actualB" | "forecastB"
    source_unit: str = ""             # "百万円", "千円" 等


@dataclass
class ExtractedFinancials:
    """抽出された決算数値"""
    sales: int | None = None             # 累計売上高（Excel単位に変換済み）
    gross_profit: int | None = None      # 累計粗利益（抽出できない場合None）
    selling_general_and_administrative_expenses: int | None = None # 累計販管費
    operating_profit: int | None = None  # 累計営業利益
    cost_of_sales: int | None = None     # 売上原価（計算補完用）
    fiscal_year: str = ""                # "R8/3" 形式
    quarter: str = ""                    # "1Q","2Q","3Q","4Q"
    source_unit: str = ""               # 元書類の単位（例: "百万円"）
    confidence: str = "low"             # "high","medium","low"
    field_sources: dict = field(default_factory=dict)
    # field_sources 例: {"sales": "xbrl", "gross_profit": "pdf_fallback", "operating_profit": "xbrl"}


@dataclass
class OrderMetric:
    """受注系メトリクス（1件分）"""
    metric_name: str = ""        # 'orders_total' / 'backlog_total' / 'carryover_construction_total'
    value: float | None = None   # 正規化後の値（百万円）
    raw_value: float | None = None  # 元の値
    unit: str = ""               # '百万円' / '億円' / '千円'
    confidence: str = "low"      # 'high' / 'medium' / 'low'
    raw_text: str = ""           # 抽出元テキスト


@dataclass
class ExtractedOrderMetrics:
    """抽出された受注系メトリクス（複数指標）"""
    metrics: list[OrderMetric] = None  # type: ignore
    fiscal_year: str = ""
    quarter: str = ""
    source_unit: str = ""
    comparison_columns: list | None = None  # list[ComparisonColumn] — 比較列データ（optional）

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = []


@dataclass
class RowLocation:
    """行特定結果"""
    start_row: int   # A列コード一致行
    term_row: int    # M列年度一致行
    target_row: int  # N列四半期一致行


@dataclass
class WriteResult:
    """Excel書き込み結果"""
    status: str                # success, conflict_detected, etc.
    target_row: int = 0
    old_values: dict | None = None   # {"sales": ..., "gross_profit": ..., "operating_profit": ...}
    new_values: dict | None = None
    detail: str = ""

    @property
    def is_success(self) -> bool:
        return self.status == "success"


# ============================================================
# 決算短信判定キーワード（共通定数）
# ============================================================
# fetcher.classify_disclosure() と extractor._is_tanshin_title() の
# 両方がこの定数を参照する。ズレ防止のため必ずここで一元管理。
FINANCIAL_STATEMENT_KEYWORDS = [
    "決算短信",
    "四半期決算",
    "通期決算",
    "訂正決算短信",
]


# ステータス定数
class Status:
    SUCCESS = "success"
    CODE_NOT_IN_SHEET = "code_not_in_sheet"
    MISSING_TERM_WITHIN_150 = "missing_term_within_150"
    MISSING_QUARTER_NEAR_TERM = "missing_quarter_near_term"
    CONFLICT_DETECTED = "conflict_detected"
    FILE_LOCKED_OR_SAVE_FAILED = "file_locked_or_save_failed"
    PARSE_FAILED = "parse_failed"
    DOWNLOAD_FAILED = "download_failed"
    ACTUAL_NOT_APPLICABLE = "actual_not_applicable"
    UNCONFIRMED_YEAR = "unconfirmed_year"
    ALREADY_PROCESSED = "already_processed"
    # retryable skip: 将来のコード修正で再処理可能なスキップ
    SKIPPED_NOT_TANSHIN = "skipped_not_tanshin"
