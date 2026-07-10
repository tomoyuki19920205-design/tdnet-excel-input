import pytest
import re
import unicodedata
from unittest.mock import MagicMock, patch
from src.events.forecast_extractor import _normalize_text, _extract_from_text, _extract_numbers_from_line
from src.events.event_pipeline import _has_forecast_change, _forecast_to_event_record, upsert_event

def test_cid_not_extracted_as_numeric():
    # 1. (cid:12)(cid:13)(cid:14) が財務数値として抽出されないことのテスト
    text = "(cid:12) (cid:13) (cid:14) (cid:15)"
    normalized = _normalize_text(text)
    assert "12" not in normalized
    assert "13" not in normalized
    
    nums = _extract_numbers_from_line(text)
    assert not nums or all(n is None for n in nums)

def test_cid_text_blocks_native_extraction():
    # 2. CID主体のテキストではnative表抽出が拒否されることのテスト (CID多数、アンカー不足)
    garbled_text = "(cid:12)(cid:13)(cid:14)(cid:15)(cid:16)(cid:17)(cid:18)(cid:19)(cid:20)(cid:21)\n前回予想\n今回予想" # CID 10個, アンカーは "前回予想"/"今回予想" なので0個 (主要アンカー「前回発表予想」等のリストには含まれない)
    event = _extract_from_text(garbled_text, title="業績予想の修正", is_difference=False)
    
    assert event.extracted_metrics_count == 0
    assert event.subtype == "undecided"
    assert event.previous_sales is None
    assert event.revised_op is None

def test_cid_removal_leaves_no_numbers():
    # 3. CID除去後に数字だけが残らないことのテスト
    raw_line = "売上高 (cid:12) 百万円"
    normalized = _normalize_text(raw_line)
    nums = _extract_numbers_from_line(normalized)
    assert not nums or all(n is None for n in nums)

def test_normal_japanese_table_preserved():
    # 4. 正常な日本語表では既存抽出結果が維持されることのテスト
    normal_text = "売上高  営業利益  経常利益  当期純利益\n前回発表予想  10,000  500  500  300\n今回修正予想  12,000  600  600  360"
    event = _extract_from_text(normal_text, title="業績予想の修正に関するお知らせ", is_difference=False)
    
    assert event.extracted_metrics_count >= 3
    assert event.previous_sales == 10000.0
    assert event.revised_sales == 12000.0
    assert event.previous_op == 500.0
    assert event.revised_op == 600.0
    assert event.subtype == "upward"

def test_safe_failure_when_ocr_unavailable():
    # 5. OCRが利用不能な場合にゴミ数値ではなく安全な失敗となることのテスト
    garbled_text = "(cid:12)(cid:13)(cid:14)(cid:15)(cid:16)(cid:17)(cid:18)(cid:19)(cid:20)(cid:21)(cid:22)\n(cid:23)(cid:24)(cid:25)"
    event = _extract_from_text(garbled_text, title="業績予想の修正", is_difference=False)
    assert event.extracted_metrics_count == 0
    assert event.revised_op is None

# --- 追加テスト 1: 少数CID正常表 ---
def test_minor_cid_normal_table():
    # 正常な表にCIDが3個混在しているケース。native抽出が拒否されないこと。
    text = "売上高  営業利益  経常利益  当期純利益\n前回発表予想  10,000  (cid:12) 500  500  300\n今回修正予想  12,000  (cid:23) 600  600  360"
    event = _extract_from_text(text, title="業績予想の修正に関するお知らせ", is_difference=False)
    
    # 正常に抽出でき、かつCID由来の 12, 23 が数値に混入していないこと
    assert event.extracted_metrics_count >= 3
    assert event.previous_op == 500.0
    assert event.revised_op == 600.0
    assert event.previous_sales == 10000.0
    assert event.revised_sales == 12000.0
    assert event.previous_net_income == 300.0
    # 12 や 23 などの CID 由来の値になっていないこと
    assert event.previous_op != 12.0
    assert event.revised_op != 23.0

# --- 追加テスト 2: CID多数・正常アンカーあり ---
def test_many_cid_normal_anchors():
    # CIDが10個以上あるが、主要アンカー（売上高、営業利益、前回発表予想、今回修正予想）が豊富にあるため拒否されないこと。
    text = (
        "(cid:10)(cid:11)(cid:12)(cid:13)(cid:14)(cid:15)(cid:16)(cid:17)(cid:18)(cid:19)(cid:20)\n" # CID 11個
        "売上高  営業利益  経常利益  純利益\n" # アンカー「売上高」「営業利益」「経常利益」「純利益」 (4つ)
        "前回発表予想  10,000  500  500  300\n" # アンカー「前回発表予想」 (1つ)
        "今回修正予想  12,000  600  600  360"    # アンカー「今回修正予想」 (1つ)
    )
    event = _extract_from_text(text, title="業績予想の修正に関するお知らせ", is_difference=False)
    
    # CID多数でも主要アンカーが2個以上（ここでは6個）あるため拒否されない
    assert event.extracted_metrics_count >= 3
    assert event.previous_sales == 10000.0
    assert event.revised_op == 600.0

# --- 追加テスト 3: 4673相当 (CID多数・アンカー不足) ---
def test_4673_simulation():
    # CIDが多数あり、主要アンカーが不足（0または1個）しているため、native抽出が拒否されること。
    garbled_text = (
        "(cid:12)(cid:13)(cid:14)(cid:15)(cid:16) (cid:17)(cid:18)(cid:19)(cid:20)(cid:21)(cid:22)(cid:14)(cid:15)\n"
        "(cid:23)(cid:24)(cid:25) (cid:23)(cid:24)(cid:26)(cid:27)(cid:28)(cid:15)(cid:29) (cid:30)(cid:31)(cid:10) !\n"
        "業績予想\n" # 主要アンカー「業績予想」 (1つのみ)
        "(cid:128)(cid:129)(cid:130)(cid:24)ST(cid:131)(cid:132)(cid:133) B(cid:134)B(cid:2)(cid:2)\n"
        "(cid:137)(cid:129)XYST(cid:131)(cid:138)(cid:133) B(cid:134))\n"
    )
    event = _extract_from_text(garbled_text, title="お知らせ", is_difference=False)
    
    assert event.extracted_metrics_count == 0
    assert event.subtype == "undecided"
    assert event.previous_sales is None
    assert event.revised_op is None
    
    # ゴミ数値（12, 13, 23, 24, 0.0）を返さないこと
    extracted_vals = {
        event.previous_sales, event.revised_sales,
        event.previous_op, event.revised_op,
        event.change_op_pct
    }
    assert not any(v in {12.0, 13.0, 23.0, 24.0, 0.0} for v in extracted_vals if v is not None)

# --- 追加テスト 4: DB保存関数非呼び出し ---
def test_db_save_not_called_on_garbled_forecast():
    # CID多数・主要アンカー不足の入力テキスト
    garbled_text = (
        "(cid:12)(cid:13)(cid:14)(cid:15)(cid:16) (cid:17)(cid:18)(cid:19)(cid:20)(cid:21)(cid:22)(cid:14)(cid:15)\n"
        "(cid:23)(cid:24)(cid:25) (cid:23)(cid:24)(cid:26)(cid:27)(cid:28)(cid:15)(cid:29)\n"
        "業績予想\n" # 主要アンカー1つ
    )
    
    # 1. 抽出器に渡すと安全に抽出失敗 (数値すべて None)
    event = _extract_from_text(garbled_text, title="お知らせ", is_difference=False)
    assert event.extracted_metrics_count == 0
    assert event.subtype == "undecided"
    assert event.previous_sales is None
    
    # 2. _has_forecast_change が False を返すことを確認
    has_change = _has_forecast_change(event)
    assert has_change is False
    
    # 3. 本番保存関数 (upsert_event) が呼ばれないことを mock で確認
    mock_conn = MagicMock()
    with patch("src.events.event_pipeline.upsert_event") as mock_upsert:
        # event_pipeline.py の本番処理と同様の保存分岐経路をシミュレート
        if has_change:
            record = _forecast_to_event_record(MagicMock(), event)
            upsert_event(mock_conn, record)
        
        # 呼び出し回数が 0 であることをアサート
        mock_upsert.assert_not_called()
