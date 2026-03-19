#!/usr/bin/env python3
"""XBRL プロファイルのテスト"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from src.extractors.xbrl_profiles import (
    IndustryType,
    XbrlProfile,
    TagMapping,
    GENERAL_PROFILE,
    BANK_PROFILE,
    REIT_PROFILE,
    SECURITIES_PROFILE,
    INSURANCE_PROFILE,
    ALL_PROFILES,
    get_profile,
    detect_industry_profile,
    resolve_facts,
    get_merged_tag_map,
)


class TestXbrlProfile:
    def test_general_profile_has_net_sales(self):
        tag_map = GENERAL_PROFILE.get_tag_map()
        assert "NetSales" in tag_map
        assert tag_map["NetSales"] == "sales"

    def test_general_profile_has_operating_income(self):
        tag_map = GENERAL_PROFILE.get_tag_map()
        assert "OperatingIncome" in tag_map
        assert tag_map["OperatingIncome"] == "operating_profit"

    def test_bank_profile_has_bnk_tags(self):
        tag_map = BANK_PROFILE.get_tag_map()
        assert "OrdinaryIncomeBNK" in tag_map
        assert tag_map["OrdinaryIncomeBNK"] == "sales"

    def test_reit_profile_has_reit_tags(self):
        tag_map = REIT_PROFILE.get_tag_map()
        assert "OperatingRevenuesREIT" in tag_map
        assert tag_map["OperatingRevenuesREIT"] == "sales"


class TestGetProfile:
    def test_general(self):
        p = get_profile("general")
        assert p.industry == IndustryType.GENERAL

    def test_bank(self):
        p = get_profile("bank")
        assert p.industry == IndustryType.BANK

    def test_unknown_returns_general(self):
        p = get_profile("unknown_type")
        assert p.industry == IndustryType.GENERAL


class TestDetectIndustryProfile:
    def test_general_company(self):
        facts = {"NetSales", "OperatingIncome", "GrossProfit"}
        profiles = detect_industry_profile(facts)
        # general は常に最後にある
        assert profiles[-1].industry == IndustryType.GENERAL
        # 特殊業態はマッチしない
        assert len(profiles) == 1

    def test_bank_detected(self):
        facts = {"OrdinaryIncomeBNK", "OperatingIncomeBNK", "NetSales"}
        profiles = detect_industry_profile(facts)
        assert profiles[0].industry == IndustryType.BANK
        assert profiles[-1].industry == IndustryType.GENERAL

    def test_reit_detected(self):
        facts = {"OperatingRevenuesREIT", "OperatingIncomeREIT"}
        profiles = detect_industry_profile(facts)
        assert profiles[0].industry == IndustryType.REIT

    def test_securities_detected(self):
        facts = {"OperatingRevenueSEC", "OperatingIncomeSEC"}
        profiles = detect_industry_profile(facts)
        assert profiles[0].industry == IndustryType.SECURITIES

    def test_insurance_detected(self):
        facts = {"OrdinaryIncomeINS", "OperatingIncomeINS"}
        profiles = detect_industry_profile(facts)
        assert profiles[0].industry == IndustryType.INSURANCE


class TestResolveFacts:
    def test_general_resolution(self):
        facts = {"NetSales", "OperatingIncome", "GrossProfit"}
        result = resolve_facts(facts)
        assert result.sales_tag == "NetSales"
        assert result.profit_tag == "OperatingIncome"
        assert result.gross_profit_tag == "GrossProfit"
        assert result.profile_used == IndustryType.GENERAL

    def test_bank_resolution(self):
        facts = {"OrdinaryIncomeBNK", "OperatingIncomeBNK"}
        result = resolve_facts(facts)
        assert result.sales_tag == "OrdinaryIncomeBNK"
        assert result.profit_tag == "OperatingIncomeBNK"
        assert result.profile_used == IndustryType.BANK

    def test_reit_resolution(self):
        facts = {"OperatingRevenuesREIT", "OperatingIncomeREIT"}
        result = resolve_facts(facts)
        assert result.sales_tag == "OperatingRevenuesREIT"
        assert result.profit_tag == "OperatingIncomeREIT"
        assert result.profile_used == IndustryType.REIT

    def test_fallback_to_general(self):
        """特殊タグなし → general fallback"""
        facts = {"NetSales", "OperatingIncome"}
        result = resolve_facts(facts)
        assert result.profile_used == IndustryType.GENERAL

    def test_unknown_tags_collected(self):
        """未知タグが unmatched_tags に記録される"""
        facts = {"NetSales", "OperatingIncome", "SomeUnknownSalesTag"}
        result = resolve_facts(facts)
        assert "SomeUnknownSalesTag" in result.unmatched_tags

    def test_match_details(self):
        facts = {"NetSales", "OperatingIncome"}
        result = resolve_facts(facts)
        assert "sales" in result.match_details
        assert result.match_details["sales"]["tag"] == "NetSales"


class TestGetMergedTagMap:
    def test_contains_general_tags(self):
        tag_map = get_merged_tag_map()
        assert "NetSales" in tag_map
        assert "OperatingIncome" in tag_map

    def test_contains_special_tags(self):
        tag_map = get_merged_tag_map()
        assert "OrdinaryIncomeBNK" in tag_map
        assert "OperatingRevenuesREIT" in tag_map

    def test_backward_compatible_with_existing(self):
        """既存の _XBRL_TAG_MAP と同等のマッピングを含む"""
        tag_map = get_merged_tag_map()
        existing_essentials = {
            "NetSales": "sales",
            "Revenue": "sales",
            "OperatingRevenue": "sales",
            "GrossProfit": "gross_profit",
            "OperatingIncome": "operating_profit",
            "OperatingProfit": "operating_profit",
        }
        for tag, field in existing_essentials.items():
            assert tag in tag_map, f"{tag} not in merged tag map"
            assert tag_map[tag] == field, f"{tag}: expected {field}, got {tag_map[tag]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
