#!/usr/bin/env python3
"""test_buyback_extractor.py — 抽出ロジックと正規化ユーティリティのテスト"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.events.buyback_extractor import (
    normalize_jp_number,
    normalize_share_count,
    normalize_amount_to_million_yen,
    normalize_jp_date,
    normalize_period,
    normalize_percent,
    normalize_method,
    compute_text_hash,
    extract_buyback_event,
    derive_metadata_from_text,
)
from src.events.buyback_models import (
    BUYBACK_DECISION, BUYBACK_STATUS, BUYBACK_RESULT, TREASURY_CANCEL,
)


# ============================================================
# normalize_jp_number
# ============================================================
class TestNormalizeJpNumber:
    def test_full_width(self):
        assert normalize_jp_number("１２３４５") == "12345"

    def test_comma_removal(self):
        assert normalize_jp_number("3,000,000") == "3000000"

    def test_full_width_comma(self):
        assert normalize_jp_number("３，０００") == "3000"

    def test_full_width_space(self):
        assert normalize_jp_number("１２　３４") == "12 34"


# ============================================================
# normalize_share_count
# ============================================================
class TestNormalizeShareCount:
    def test_plain(self):
        assert normalize_share_count("3,000,000株") == 3_000_000

    def test_man(self):
        assert normalize_share_count("300万株") == 3_000_000

    def test_man_decimal(self):
        assert normalize_share_count("300.5万株") == 3_005_000

    def test_sen(self):
        assert normalize_share_count("3,000千株") == 3_000_000

    def test_oku(self):
        assert normalize_share_count("1億株") == 100_000_000

    def test_full_width(self):
        assert normalize_share_count("３００万株") == 3_000_000

    def test_no_match(self):
        assert normalize_share_count("株式なし") is None


# ============================================================
# normalize_amount_to_million_yen
# ============================================================
class TestNormalizeAmount:
    def test_oku(self):
        assert normalize_amount_to_million_yen("50億円") == 5000.0

    def test_oku_decimal(self):
        assert normalize_amount_to_million_yen("50.5億円") == 5050.0

    def test_hyakuman(self):
        assert normalize_amount_to_million_yen("1,200百万円") == 1200.0

    def test_yen(self):
        assert normalize_amount_to_million_yen("3,450,000,000円") == 3450.0

    def test_man(self):
        assert normalize_amount_to_million_yen("5,000万円") == 50.0

    def test_sen(self):
        assert normalize_amount_to_million_yen("500,000千円") == 500.0

    def test_full_width(self):
        assert normalize_amount_to_million_yen("５０億円") == 5000.0

    def test_no_match(self):
        assert normalize_amount_to_million_yen("金額なし") is None


# ============================================================
# normalize_jp_date
# ============================================================
class TestNormalizeJpDate:
    def test_jp_format(self):
        assert normalize_jp_date("2025年4月1日") == "2025-04-01"

    def test_reiwa(self):
        assert normalize_jp_date("令和7年4月1日") == "2025-04-01"

    def test_slash(self):
        assert normalize_jp_date("2025/04/01") == "2025-04-01"

    def test_iso(self):
        assert normalize_jp_date("2025-04-01") == "2025-04-01"

    def test_full_width(self):
        assert normalize_jp_date("２０２５年４月１日") == "2025-04-01"

    def test_no_match(self):
        assert normalize_jp_date("日付なし") is None

    def test_heisei(self):
        assert normalize_jp_date("平成30年12月25日") == "2018-12-25"


# ============================================================
# normalize_period
# ============================================================
class TestNormalizePeriod:
    def test_kara_made(self):
        start, end = normalize_period("2025年4月1日から2025年9月30日まで")
        assert start == "2025-04-01"
        assert end == "2025-09-30"

    def test_ji_shi(self):
        start, end = normalize_period("自 2025年4月1日 至 2025年9月30日")
        assert start == "2025-04-01"
        assert end == "2025-09-30"

    def test_no_match(self):
        start, end = normalize_period("期間なし")
        assert start is None
        assert end is None


# ============================================================
# normalize_percent
# ============================================================
class TestNormalizePercent:
    def test_percent(self):
        assert normalize_percent("2.35%") == 2.35

    def test_full_width_percent(self):
        assert normalize_percent("1.2％") == 1.2

    def test_integer(self):
        assert normalize_percent("5%") == 5.0

    def test_no_match(self):
        assert normalize_percent("割合なし") is None


# ============================================================
# normalize_method
# ============================================================
class TestNormalizeMethod:
    def test_market(self):
        assert normalize_method("東京証券取引所における市場買付") == "market_purchase"

    def test_tostnet(self):
        assert normalize_method("東京証券取引所 ToSTNeT-3 による買付") == "tostnet"

    def test_off_auction(self):
        assert normalize_method("立会外取引") == "off_auction"

    def test_other(self):
        assert normalize_method("特殊な方法") == "other"

    def test_empty(self):
        assert normalize_method("") is None


# ============================================================
# compute_text_hash
# ============================================================
class TestTextHash:
    def test_hash_length(self):
        h = compute_text_hash("test text")
        assert len(h) == 16

    def test_hash_deterministic(self):
        h1 = compute_text_hash("same text")
        h2 = compute_text_hash("same text")
        assert h1 == h2

    def test_hash_different(self):
        h1 = compute_text_hash("text a")
        h2 = compute_text_hash("text b")
        assert h1 != h2


# ============================================================
# フィクスチャ: 現実に近い日本語サンプル
# ============================================================
SAMPLE_DECISION = """\
自己株式取得に係る事項の決定に関するお知らせ

当社は、本日開催の取締役会において、会社法第165条第3項の規定に基づき、
自己株式の取得に係る事項について下記のとおり決議いたしましたので、お知らせいたします。

1. 取得する株式の種類　　当社普通株式
2. 取得し得る株式の総数　3,000,000株（上限）
   （発行済株式総数（自己株式を除く）に対する割合 2.35%）
3. 取得価額の総額　　　　50億円（上限）
4. 取得期間　　　　　　　2025年4月1日から2025年9月30日まで
5. 取得方法　　　　　　　東京証券取引所における市場買付
"""

SAMPLE_STATUS = """\
自己株式の取得状況に関するお知らせ（2025年4月度）

当社は、2025年4月1日から2025年4月30日までの期間における
自己株式の取得状況について、下記のとおりお知らせいたします。

1. 取得した株式の種類　　当社普通株式
2. 取得した株式の数　　　450,000株
3. 取得価額の総額　　　　900百万円
4. 取得方法　　　　　　　東京証券取引所における市場買付
"""

SAMPLE_RESULT = """\
自己株式の取得結果及び取得終了に関するお知らせ

1. 取得した株式の種類　　当社普通株式
2. 取得した株式の総数　  2,800,000株
3. 取得価額の総額　　　  48億円
4. 取得期間　　　　　　  自 2025年4月1日 至 2025年9月30日
5. 取得方法　　　　　　  東京証券取引所における市場買付
6. 発行済株式総数に対する割合  2.19%
"""

SAMPLE_CANCEL = """\
自己株式の消却に関するお知らせ

1. 消却する株式の種類　　当社普通株式
2. 消却する株式の数　　　5,000,000株
   （消却前発行済株式総数に対する割合 3.89%）
3. 消却予定日　　　　　　2025年10月15日
"""

SAMPLE_DISPOSAL = """\
自己株式処分に関するお知らせ

当社は、本日開催の取締役会において、自己株式の処分を行うことを決議しました。
処分する株式数は500,000株です。
"""

SAMPLE_TABLE_FORMAT = """\
自己株式取得に係る事項の決定に関するお知らせ

取得する株式の種類　　当社普通株式
取得し得る株式の総数　1,250,000株（上限）
取得価額の総額　　　　30億円（上限）
取得期間　　　　　　　2025年5月1日から2025年11月30日まで
取得方法　　　　　　　東京証券取引所 ToSTNeT-3 による買付
発行済株式総数に対する割合　1.56%
"""


# ============================================================
# extract_buyback_event テスト — decision
# ============================================================
class TestExtractDecision:
    def test_shares_limit(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.shares_limit == 3_000_000

    def test_amount_limit(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.amount_limit_million_yen == 5000.0

    def test_period(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.start_date == "2025-04-01"
        assert ev.end_date == "2025-09-30"

    def test_method(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.acquisition_method == "market_purchase"

    def test_ratio(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.ratio_to_outstanding == 2.35

    def test_confidence_high(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.extraction_confidence >= 0.8


# ============================================================
# extract_buyback_event テスト — status
# ============================================================
class TestExtractStatus:
    def test_shares_acquired(self):
        ev = extract_buyback_event(SAMPLE_STATUS, BUYBACK_STATUS, ticker="6750")
        assert ev.shares_acquired == 450_000

    def test_amount_acquired(self):
        ev = extract_buyback_event(SAMPLE_STATUS, BUYBACK_STATUS, ticker="6750")
        assert ev.amount_acquired_million_yen == 900.0

    def test_method(self):
        ev = extract_buyback_event(SAMPLE_STATUS, BUYBACK_STATUS, ticker="6750")
        assert ev.acquisition_method == "market_purchase"

    def test_status_period_label(self):
        ev = extract_buyback_event(SAMPLE_STATUS, BUYBACK_STATUS, ticker="6750")
        assert "4月" in (ev.status_period_label or "")


# ============================================================
# extract_buyback_event テスト — result
# ============================================================
class TestExtractResult:
    def test_shares_acquired(self):
        ev = extract_buyback_event(SAMPLE_RESULT, BUYBACK_RESULT, ticker="6750")
        assert ev.shares_acquired == 2_800_000

    def test_amount_acquired(self):
        ev = extract_buyback_event(SAMPLE_RESULT, BUYBACK_RESULT, ticker="6750")
        assert ev.amount_acquired_million_yen == 4800.0

    def test_period(self):
        ev = extract_buyback_event(SAMPLE_RESULT, BUYBACK_RESULT, ticker="6750")
        assert ev.start_date == "2025-04-01"
        assert ev.end_date == "2025-09-30"

    def test_ratio(self):
        ev = extract_buyback_event(SAMPLE_RESULT, BUYBACK_RESULT, ticker="6750")
        assert ev.ratio_to_outstanding == 2.19


# ============================================================
# extract_buyback_event テスト — cancel
# ============================================================
class TestExtractCancel:
    def test_shares_cancelled(self):
        ev = extract_buyback_event(SAMPLE_CANCEL, TREASURY_CANCEL, ticker="6750")
        assert ev.shares_cancelled == 5_000_000

    def test_cancel_date(self):
        ev = extract_buyback_event(SAMPLE_CANCEL, TREASURY_CANCEL, ticker="6750")
        assert ev.cancel_date == "2025-10-15"

    def test_ratio(self):
        ev = extract_buyback_event(SAMPLE_CANCEL, TREASURY_CANCEL, ticker="6750")
        assert ev.ratio_to_outstanding == 3.89


# ============================================================
# テーブル形式テスト
# ============================================================
class TestExtractTableFormat:
    def test_shares_from_table(self):
        ev = extract_buyback_event(SAMPLE_TABLE_FORMAT, BUYBACK_DECISION, ticker="1234")
        assert ev.shares_limit == 1_250_000

    def test_amount_from_table(self):
        ev = extract_buyback_event(SAMPLE_TABLE_FORMAT, BUYBACK_DECISION, ticker="1234")
        assert ev.amount_limit_million_yen == 3000.0

    def test_tostnet_method(self):
        ev = extract_buyback_event(SAMPLE_TABLE_FORMAT, BUYBACK_DECISION, ticker="1234")
        assert ev.acquisition_method == "tostnet"

    def test_ratio_from_table(self):
        ev = extract_buyback_event(SAMPLE_TABLE_FORMAT, BUYBACK_DECISION, ticker="1234")
        assert ev.ratio_to_outstanding == 1.56


# ============================================================
# extracted_json テスト
# ============================================================
class TestExtractedJson:
    def test_has_extracted_json(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        assert ev.extracted_json != ""
        import json
        data = json.loads(ev.extracted_json)
        assert "title_used" in data
        assert "body_head_used" in data

    def test_raw_snippets_in_json(self):
        ev = extract_buyback_event(SAMPLE_DECISION, BUYBACK_DECISION, ticker="6750")
        import json
        data = json.loads(ev.extracted_json)
        assert "raw_shares_text" in data
        assert "raw_amount_text" in data


# ============================================================
# derive_metadata_from_text テスト
# ============================================================
SAMPLE_PDF_HEAD = """\
2026年２月24日
各 位
会 社 名 丸大食品株式会社
代表者名 代表取締役社長 佐藤 勇二
（コード番号 2288 東証プライム）
問合せ先 取締役経理部長 森本 芳史
（TEL 072－661－2518）
2026年３月期第３四半期決算短信〔日本基準〕（連結）
"""


class TestDeriveMetadata:
    def test_derive_ticker(self):
        result = derive_metadata_from_text(SAMPLE_PDF_HEAD)
        assert result["derived_ticker"] == "2288"

    def test_derive_date(self):
        result = derive_metadata_from_text(SAMPLE_PDF_HEAD)
        assert result["derived_disclosure_date"] == "2026-02-24"

    def test_derive_title(self):
        result = derive_metadata_from_text(SAMPLE_PDF_HEAD)
        assert result["derived_title"] is not None
        assert "決算短信" in result["derived_title"]

    def test_empty_text(self):
        result = derive_metadata_from_text("")
        assert result["derived_ticker"] is None
        assert result["derived_disclosure_date"] is None
        assert result["derived_title"] is None

    def test_no_code(self):
        result = derive_metadata_from_text("特に情報がないテキスト。お知らせです。")
        assert result["derived_ticker"] is None


# ============================================================
# treasury_cancel key fields ペナルティ
# ============================================================
SAMPLE_CANCEL_NO_FIELDS = """\
自己株式の消却に関するお知らせ

当社は自己株式の消却に関するお知らせです。
株式を消却いたします。ただし詳細は追って発表いたします。
"""


class TestCancelPenalty:
    def test_cancel_no_key_fields_low_confidence(self):
        """shares_cancelled と cancel_date が両方取れない場合は confidence 低下"""
        ev = extract_buyback_event(SAMPLE_CANCEL_NO_FIELDS, TREASURY_CANCEL, ticker="1234")
        assert ev.extraction_confidence < 0.6

    def test_cancel_with_key_fields_high_confidence(self):
        ev = extract_buyback_event(SAMPLE_CANCEL, TREASURY_CANCEL, ticker="6750")
        assert ev.extraction_confidence >= 0.8


# ============================================================
# period 優先順位テスト
# ============================================================
class TestPeriodPriority:
    def test_kara_made_priority_over_loose_date(self):
        """「から〜まで」形式が正しく抽出される"""
        text = "取得期間 2025年4月1日から2025年9月30日まで"
        start, end = normalize_period(text)
        assert start == "2025-04-01"
        assert end == "2025-09-30"

    def test_ji_shi_priority(self):
        """「自〜至」形式が最優先"""
        text = "自 2025年4月1日 至 2025年9月30日"
        start, end = normalize_period(text)
        assert start == "2025-04-01"
        assert end == "2025-09-30"

    def test_ji_shi_over_kara(self):
        """「自〜至」が「から〜まで」より優先"""
        text = "参考: 2025年3月4日から 自 2025年4月1日 至 2025年9月30日"
        start, end = normalize_period(text)
        assert start == "2025-04-01"
        assert end == "2025-09-30"

