# ============================================================
# xbrl_profiles.py — XBRL 業態別タグプロファイル
# ============================================================
"""
通常企業だけでなく REIT / 銀行 / 証券 / 保険 の特殊業態にも
対応できる XBRL タグ解決エンジン。

設計思想:
  - タグマップを1ファイルの dict ベタ書きで終わらせない
  - 業態別 tag profile を持てる構造
  - 自動業態推定 → profile 順探索 → fact 選択

フロー:
  1. detect_industry_profile(fact_names) で業態推定
  2. resolve_facts(facts, profile_order) でタグ探索
  3. best fact を採用
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================
# 業態タイプ
# ============================================================

class IndustryType(str, Enum):
    GENERAL = "general"
    BANK = "bank"
    SECURITIES = "securities"
    INSURANCE = "insurance"
    REIT = "reit"


# ============================================================
# XbrlProfile — 業態別タグプロファイル
# ============================================================

@dataclass
class TagMapping:
    """タグ名 → フィールドのマッピング1件"""
    tag_name: str               # e.g. "NetSales"
    field_name: str             # "sales" / "gross_profit" / "operating_profit"
    priority: int = 0           # 優先度 (高い方が優先)
    note: str = ""              # 説明メモ


@dataclass
class XbrlProfile:
    """
    業態別の XBRL タグプロファイル。

    Attributes:
        industry: 業態タイプ
        display_name: 表示名
        sales_tags: 売上系タグ (優先度順)
        profit_tags: 利益系タグ (優先度順)
        gross_profit_tags: 粗利系タグ
        detection_tags: この業態を示すタグ名 (業態推定用)
    """
    industry: str = ""
    display_name: str = ""
    sales_tags: list[TagMapping] = field(default_factory=list)
    profit_tags: list[TagMapping] = field(default_factory=list)
    gross_profit_tags: list[TagMapping] = field(default_factory=list)
    detection_tags: list[str] = field(default_factory=list)

    def get_tag_map(self) -> dict[str, str]:
        """
        従来の _XBRL_TAG_MAP 形式の dict を生成。
        既存コードとの互換性のため。
        """
        result: dict[str, str] = {}
        for tm in self.sales_tags:
            result[tm.tag_name] = tm.field_name
        for tm in self.profit_tags:
            result[tm.tag_name] = tm.field_name
        for tm in self.gross_profit_tags:
            result[tm.tag_name] = tm.field_name
        return result


# ============================================================
# プロファイル定義
# ============================================================

GENERAL_PROFILE = XbrlProfile(
    industry=IndustryType.GENERAL,
    display_name="一般事業会社",
    sales_tags=[
        TagMapping("NetSales", "sales", 100, "純売上高"),
        TagMapping("Revenue", "sales", 90, "収益 (IFRS)"),
        TagMapping("OperatingRevenue", "sales", 80, "営業収益"),
        TagMapping("NetSalesAndOperatingRevenue", "sales", 80, "売上高及び営業収益"),
        TagMapping("NetSalesAndOperatingRevenue2", "sales", 75, "売上高及び営業収益2"),
    ],
    profit_tags=[
        TagMapping("OperatingIncome", "operating_profit", 100, "営業利益"),
        TagMapping("OperatingProfit", "operating_profit", 95, "営業利益 (別名)"),
    ],
    gross_profit_tags=[
        TagMapping("GrossProfit", "gross_profit", 100, "売上総利益"),
    ],
    detection_tags=[],
)

BANK_PROFILE = XbrlProfile(
    industry=IndustryType.BANK,
    display_name="銀行業",
    sales_tags=[
        TagMapping("OrdinaryIncomeBNK", "sales", 100, "経常収益 (銀行)"),
        TagMapping("OrdinaryRevenueBNK", "sales", 95, "経常収益 (銀行別名)"),
        TagMapping("TotalOrdinaryIncomeBNK", "sales", 90, "経常収益合計 (銀行)"),
        TagMapping("OrdinaryIncome", "sales", 50, "経常利益 → 売上FBとして"),
    ],
    profit_tags=[
        TagMapping("OperatingIncomeBNK", "operating_profit", 100, "業務純益"),
        TagMapping("OrdinaryProfitBNK", "operating_profit", 90, "経常利益 (銀行)"),
        TagMapping("OrdinaryIncome", "operating_profit", 80, "経常利益 proxy (銀行のみ)"),
        TagMapping("OperatingIncome", "operating_profit", 50, "営業利益 (一般FB)"),
    ],
    gross_profit_tags=[],
    detection_tags=[
        "OrdinaryIncomeBNK", "OrdinaryRevenueBNK",
        "TotalOrdinaryIncomeBNK", "OperatingIncomeBNK",
        "OrdinaryProfitBNK",
    ],
)

SECURITIES_PROFILE = XbrlProfile(
    industry=IndustryType.SECURITIES,
    display_name="証券業",
    sales_tags=[
        TagMapping("OperatingRevenueSEC", "sales", 100, "営業収益 (証券)"),
        TagMapping("NetOperatingRevenueSEC", "sales", 95, "純営業収益 (証券)"),
        TagMapping("OperatingRevenue", "sales", 50, "営業収益 (一般FB)"),
    ],
    profit_tags=[
        TagMapping("OperatingIncomeSEC", "operating_profit", 100, "営業利益 (証券)"),
        TagMapping("OrdinaryIncomeSEC", "operating_profit", 90, "経常利益 (証券)"),
        TagMapping("OperatingIncome", "operating_profit", 50, "営業利益 (一般FB)"),
    ],
    gross_profit_tags=[],
    detection_tags=[
        "OperatingRevenueSEC", "NetOperatingRevenueSEC",
        "OperatingIncomeSEC", "OrdinaryIncomeSEC",
    ],
)

INSURANCE_PROFILE = XbrlProfile(
    industry=IndustryType.INSURANCE,
    display_name="保険業",
    sales_tags=[
        TagMapping("OrdinaryIncomeINS", "sales", 100, "経常収益 (保険)"),
        TagMapping("OrdinaryRevenueINS", "sales", 95, "経常収益 (保険別名)"),
        TagMapping("NetPremiumsWrittenINS", "sales", 90, "正味収入保険料"),
        TagMapping("OperatingRevenue", "sales", 50, "営業収益 (一般FB)"),
    ],
    profit_tags=[
        TagMapping("OperatingIncomeINS", "operating_profit", 100, "営業利益 (保険)"),
        TagMapping("OrdinaryProfitINS", "operating_profit", 90, "経常利益 (保険)"),
        TagMapping("OperatingIncome", "operating_profit", 50, "営業利益 (一般FB)"),
    ],
    gross_profit_tags=[],
    detection_tags=[
        "OrdinaryIncomeINS", "OrdinaryRevenueINS",
        "NetPremiumsWrittenINS", "OperatingIncomeINS",
        "OrdinaryProfitINS",
    ],
)

REIT_PROFILE = XbrlProfile(
    industry=IndustryType.REIT,
    display_name="REIT / 投資法人",
    sales_tags=[
        TagMapping("OperatingRevenuesREIT", "sales", 100, "営業収益 (REIT)"),
        TagMapping("OperatingRevenueINV", "sales", 95, "営業収益 (投資法人)"),
        TagMapping("OperatingRevenue", "sales", 50, "営業収益 (一般FB)"),
    ],
    profit_tags=[
        TagMapping("OperatingIncomeREIT", "operating_profit", 100, "営業利益 (REIT)"),
        TagMapping("OperatingProfitREIT", "operating_profit", 95, "営業利益 (REIT別名)"),
        TagMapping("OrdinaryIncomeREIT", "operating_profit", 90, "経常利益 (REIT)"),
        TagMapping("OperatingIncome", "operating_profit", 50, "営業利益 (一般FB)"),
    ],
    gross_profit_tags=[],
    detection_tags=[
        "OperatingRevenuesREIT", "OperatingRevenueINV",
        "OperatingIncomeREIT", "OperatingProfitREIT",
        "OrdinaryIncomeREIT",
    ],
)

# プロファイル一覧 (探索優先順)
ALL_PROFILES: list[XbrlProfile] = [
    GENERAL_PROFILE,
    BANK_PROFILE,
    SECURITIES_PROFILE,
    INSURANCE_PROFILE,
    REIT_PROFILE,
]

_PROFILE_MAP: dict[str, XbrlProfile] = {p.industry: p for p in ALL_PROFILES}


def get_profile(industry: str) -> XbrlProfile:
    """業態名からプロファイルを取得"""
    return _PROFILE_MAP.get(industry, GENERAL_PROFILE)


# ============================================================
# 業態推定
# ============================================================

def detect_industry_profile(
    fact_names: list[str] | set[str],
) -> list[XbrlProfile]:
    """
    XBRL fact の概念名一覧から業態を推定し、
    マッチしたプロファイルを優先度順で返す。

    一般事業会社は常に最後のフォールバックとして含まれる。

    Args:
        fact_names: XBRL fact の概念名一覧
            例: {"NetSales", "OperatingIncome", "GrossProfit"}
            例: {"OrdinaryIncomeBNK", "OperatingIncomeBNK"}

    Returns:
        マッチしたプロファイルのリスト (優先度順)
        最低1件 (GENERAL_PROFILE) は必ず含まれる
    """
    name_set = set(fact_names)
    matched: list[tuple[int, XbrlProfile]] = []

    for profile in ALL_PROFILES:
        if profile.industry == IndustryType.GENERAL:
            continue  # general は最後に追加

        hit_count = sum(1 for tag in profile.detection_tags if tag in name_set)
        if hit_count > 0:
            matched.append((hit_count, profile))

    # ヒット数降順でソート
    matched.sort(key=lambda x: x[0], reverse=True)

    result = [p for _, p in matched]
    result.append(GENERAL_PROFILE)  # 常に最後のフォールバック

    return result


# ============================================================
# fact 解決
# ============================================================

@dataclass
class FactMatchResult:
    """fact 解決の結果"""
    profile_used: str = ""          # 使用したプロファイルの業態名
    sales_tag: str | None = None    # マッチした売上タグ
    profit_tag: str | None = None   # マッチした利益タグ
    gross_profit_tag: str | None = None
    unmatched_tags: list[str] = field(default_factory=list)
    match_details: dict[str, Any] = field(default_factory=dict)


def resolve_facts(
    fact_names: list[str] | set[str],
    profile_order: list[XbrlProfile] | None = None,
) -> FactMatchResult:
    """
    fact 概念名一覧からプロファイル順にタグを解決する。

    Args:
        fact_names: XBRL fact の概念名一覧
        profile_order: 探索するプロファイル順
            None の場合は detect_industry_profile で自動判定

    Returns:
        FactMatchResult
    """
    name_set = set(fact_names)

    if profile_order is None:
        profile_order = detect_industry_profile(fact_names)

    result = FactMatchResult()
    unmatched_financial = []

    for profile in profile_order:
        # 売上タグ探索 (優先度順)
        if result.sales_tag is None:
            for tm in sorted(profile.sales_tags, key=lambda t: -t.priority):
                if tm.tag_name in name_set:
                    result.sales_tag = tm.tag_name
                    result.profile_used = profile.industry
                    result.match_details["sales"] = {
                        "tag": tm.tag_name,
                        "profile": profile.industry,
                        "priority": tm.priority,
                        "note": tm.note,
                    }
                    break

        # 利益タグ探索
        if result.profit_tag is None:
            for tm in sorted(profile.profit_tags, key=lambda t: -t.priority):
                if tm.tag_name in name_set:
                    result.profit_tag = tm.tag_name
                    if not result.profile_used:
                        result.profile_used = profile.industry
                    result.match_details["profit"] = {
                        "tag": tm.tag_name,
                        "profile": profile.industry,
                        "priority": tm.priority,
                        "note": tm.note,
                    }
                    break

        # 粗利タグ探索
        if result.gross_profit_tag is None:
            for tm in sorted(profile.gross_profit_tags, key=lambda t: -t.priority):
                if tm.tag_name in name_set:
                    result.gross_profit_tag = tm.tag_name
                    result.match_details["gross_profit"] = {
                        "tag": tm.tag_name,
                        "profile": profile.industry,
                        "priority": tm.priority,
                        "note": tm.note,
                    }
                    break

        # 全て解決したら終了
        if result.sales_tag and result.profit_tag:
            break

    # 未マッチの財務系タグを収集
    all_known_tags = set()
    for profile in ALL_PROFILES:
        for tm in profile.sales_tags + profile.profit_tags + profile.gross_profit_tags:
            all_known_tags.add(tm.tag_name)

    for name in name_set:
        local = name.split(":")[-1] if ":" in name else name
        if local not in all_known_tags:
            # 財務系っぽいが未知のタグ
            lower = local.lower()
            if any(kw in lower for kw in [
                "sales", "revenue", "profit", "income", "loss",
                "operating", "gross", "ordinary",
            ]):
                unmatched_financial.append(name)

    result.unmatched_tags = unmatched_financial

    if not result.profile_used:
        result.profile_used = IndustryType.GENERAL

    return result


# ============================================================
# 互換用: 全プロファイルのマージ済みタグマップ
# ============================================================

def get_merged_tag_map() -> dict[str, str]:
    """
    全プロファイルのタグを1つの dict にマージ。
    既存の _XBRL_TAG_MAP の代替として使用可能。

    優先度: 同じタグ名がある場合は general > 特殊業態
    """
    result: dict[str, str] = {}

    # 特殊業態を先に追加（general で上書きされる）
    for profile in reversed(ALL_PROFILES):
        result.update(profile.get_tag_map())

    return result
