import pytest
from unittest.mock import MagicMock, patch
from src.events.tdnet_event_store import update_tdnet_event_fields_by_identity

@pytest.fixture(autouse=True)
def mock_supabase_execute():
    with patch("src.events.tdnet_event_store._supabase_execute") as mock:
        yield mock

@pytest.fixture
def mock_client():
    client = MagicMock()
    return client

def test_dryrun_no_update_called(mock_client, mock_supabase_execute):
    # 1. dry-runではUPDATEが呼ばれないことのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{
        "id": "2ed74c12-624a-4040-8ef9-951b73d2ed69",
        "ticker": "4673",
        "disclosed_at": "2026-07-10T01:00:00+00:00",
        "dedupe_key": "c011d37aa037c16b7ad78fd2e031da012205363c",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        "primary_metric_value": "23.0百万円",
        "primary_metric_yoy": "+5.0%",
        "display_summary": "upward",
        "formatted_message": "upward",
        "raw_payload": '{"extracted":{"change_op_pct": 0.0}}'
    }])
    
    updates = {"primary_metric_value": "7.6億円"}
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="2ed74c12-624a-4040-8ef9-951b73d2ed69",
        ticker="4673",
        disclosed_at="2026-07-10T01:00:00+00:00",
        dedupe_key="c011d37aa037c16b7ad78fd2e031da012205363c",
        pdf_url="https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        updates=updates,
        dry_run=True
    )
    
    assert res["status"] == "success", f"Failed: {res.get('stop_reason')}"
    assert res["dry_run"] is True
    assert res["update_called"] is False
    mock_client.table.return_value.update.assert_not_called()

def test_all_5_conditions_used_in_select(mock_client, mock_supabase_execute):
    # 2. 5条件すべてがSELECTに使用されることのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{"id": "uuid"}])
    updates = {"primary_metric_value": "7.6億円"}
    
    update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates=updates,
        dry_run=True
    )
    
    # mock_client.mock_calls から すべての eq() 呼び出し引数を抽出する
    # call.table().select().eq(name, val) という呼び出し
    eq_calls = [call for call in mock_client.mock_calls if call[0].endswith("eq")]
    assert len(eq_calls) == 5
    
    called_args = {call[1] for call in eq_calls}
    assert ("id", "uuid-123") in called_args
    assert ("ticker", "4673") in called_args
    assert ("disclosed_at", "datetime-123") in called_args
    assert ("dedupe_key", "key-123") in called_args
    assert ("pdf_url", "url-123") in called_args

def test_apply_uses_all_5_conditions_in_update(mock_client, mock_supabase_execute):
    # 3. apply想定時に5条件すべてがUPDATEへ使用されることのテスト
    mock_supabase_execute.side_effect = [
        MagicMock(data=[{
            "id": "uuid-123",
            "ticker": "4673",
            "disclosed_at": "datetime-123",
            "dedupe_key": "key-123",
            "pdf_url": "url-123",
            "primary_metric_value": "23.0百万円"
        }]), # select結果
        MagicMock(data=[{"id": "uuid-123"}]) # update結果
    ]
    updates = {"primary_metric_value": "7.6億円"}
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates=updates,
        dry_run=False
    )
    
    assert res["status"] == "success", f"Failed: {res.get('stop_reason')}"
    assert res["update_called"] is True
    
    # UPDATE処理での eq() 呼び出しを確認
    # select 用の 5 回と、update 用の 5 回で合計 10 回の eq が呼ばれる
    eq_calls = [call for call in mock_client.mock_calls if call[0].endswith("eq")]
    assert len(eq_calls) == 10
    
    # 最後の 5 回（UPDATE用）の引数を検証
    update_eq_calls = eq_calls[5:]
    called_args = {call[1] for call in update_eq_calls}
    assert ("id", "uuid-123") in called_args
    assert ("ticker", "4673") in called_args
    assert ("disclosed_at", "datetime-123") in called_args
    assert ("dedupe_key", "key-123") in called_args
    assert ("pdf_url", "url-123") in called_args

def test_update_payload_limited_to_allowed_columns(mock_client, mock_supabase_execute):
    # 4. UPDATE payloadが許可5カラムだけになることのテスト
    mock_supabase_execute.side_effect = [
        MagicMock(data=[{
            "id": "uuid-123",
            "ticker": "4673",
            "disclosed_at": "datetime-123",
            "dedupe_key": "key-123",
            "pdf_url": "url-123",
            "primary_metric_value": "23.0百万円",
            "display_summary": "old summary"
        }]), # select結果
        MagicMock(data=[{"id": "uuid-123"}]) # update結果
    ]
    updates = {
        "primary_metric_value": "7.6億円",
        "display_summary": "new summary"
    }
    
    mock_update = mock_client.table.return_value.update
    update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates=updates,
        dry_run=False
    )
    
    mock_update.assert_called_once_with({
        "primary_metric_value": "7.6億円",
        "display_summary": "new summary"
    })

def test_forbidden_columns_rejected(mock_client, mock_supabase_execute):
    # 5-7. id, discord_sent_at, updated_at等の禁止カラムをupdatesへ含めると拒否されることのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{"id": "uuid-123"}])
    for forbidden in ["id", "ticker", "headline", "discord_sent_at", "updated_at", "created_at", "primary_metric_name"]:
        updates = {"primary_metric_value": "7.6億円", forbidden: "some-value"}
        res = update_tdnet_event_fields_by_identity(
            mock_client,
            id="uuid-123",
            ticker="4673",
            disclosed_at="datetime-123",
            dedupe_key="key-123",
            pdf_url="url-123",
            updates=updates,
            dry_run=True
        )
        assert res["status"] == "error"
        assert "Column not allowed" in res["stop_reason"]
        mock_client.table.return_value.update.assert_not_called()

def test_missing_identity_conditions_rejected(mock_client, mock_supabase_execute):
    # 8. 条件が1つでも欠けると拒否されることのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{"id": "uuid-123"}])
    base_cond = {
        "id": "uuid-123",
        "ticker": "4673",
        "disclosed_at": "datetime-123",
        "dedupe_key": "key-123",
        "pdf_url": "url-123",
    }
    
    for missing_key in base_cond.keys():
        cond = base_cond.copy()
        cond[missing_key] = "" # 空にする
        
        res = update_tdnet_event_fields_by_identity(
            mock_client,
            updates={"primary_metric_value": "7.6億円"},
            dry_run=True,
            **cond
        )
        assert res["status"] == "error"
        assert "Missing or empty identity field" in res["stop_reason"]

def test_no_match_aborts_update(mock_client, mock_supabase_execute):
    # 9. 対象0件ではUPDATEされないことのテスト
    mock_supabase_execute.return_value = MagicMock(data=[]) # 空
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates={"primary_metric_value": "7.6億円"},
        dry_run=False
    )
    
    assert res["status"] == "error"
    assert res["matched_rows"] == 0
    assert "Target record not found" in res["stop_reason"]
    mock_client.table.return_value.update.assert_not_called()

def test_multiple_matches_aborts_update(mock_client, mock_supabase_execute):
    # 10. 対象2件以上ではUPDATEされないことのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{"id": "uuid-1"}, {"id": "uuid-2"}])
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates={"primary_metric_value": "7.6億円"},
        dry_run=False
    )
    
    assert res["status"] == "error"
    assert res["matched_rows"] == 2
    assert "Target record is not unique" in res["stop_reason"]
    mock_client.table.return_value.update.assert_not_called()

def test_dryrun_produces_correct_diff(mock_client, mock_supabase_execute):
    # 11. dry-runで更新前後差分が正しく返ることのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{
        "id": "2ed74c12-624a-4040-8ef9-951b73d2ed69",
        "ticker": "4673",
        "disclosed_at": "2026-07-10T01:00:00+00:00",
        "dedupe_key": "c011d37aa037c16b7ad78fd2e031da012205363c",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        "primary_metric_value": "23.0百万円",
        "display_summary": "upward"
    }])
    updates = {
        "primary_metric_value": "7.6億円",
        "display_summary": "new summary"
    }
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="2ed74c12-624a-4040-8ef9-951b73d2ed69",
        ticker="4673",
        disclosed_at="2026-07-10T01:00:00+00:00",
        dedupe_key="c011d37aa037c16b7ad78fd2e031da012205363c",
        pdf_url="https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        updates=updates,
        dry_run=True
    )
    
    assert res["status"] == "success", f"Failed: {res.get('stop_reason')}"
    assert res["before"]["primary_metric_value"] == "23.0百万円"
    assert res["after"]["primary_metric_value"] == "7.6億円"
    assert res["before"]["display_summary"] == "upward"
    assert res["after"]["display_summary"] == "new summary"
    assert "primary_metric_value" in res["changed_columns"]
    assert "display_summary" in res["changed_columns"]

def test_identical_values_not_marked_changed(mock_client, mock_supabase_execute):
    # 12. 値が同一のカラムはchanged扱いにならないことのテスト
    mock_supabase_execute.return_value = MagicMock(data=[{
        "id": "2ed74c12-624a-4040-8ef9-951b73d2ed69",
        "ticker": "4673",
        "disclosed_at": "2026-07-10T01:00:00+00:00",
        "dedupe_key": "c011d37aa037c16b7ad78fd2e031da012205363c",
        "pdf_url": "https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        "primary_metric_value": "23.0百万円",
        "display_summary": "upward"
    }])
    updates = {
        "primary_metric_value": "23.0百万円", # 既存と同一
        "display_summary": "new summary" # 既存と異なる
    }
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="2ed74c12-624a-4040-8ef9-951b73d2ed69",
        ticker="4673",
        disclosed_at="2026-07-10T01:00:00+00:00",
        dedupe_key="c011d37aa037c16b7ad78fd2e031da012205363c",
        pdf_url="https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        updates=updates,
        dry_run=True
    )
    
    assert "display_summary" in res["changed_columns"]
    assert "primary_metric_value" in res["unchanged_columns"]
    assert "primary_metric_value" not in res["changed_columns"]

def test_no_insert_upsert_delete_called(mock_client, mock_supabase_execute):
    # 13. INSERT / UPSERT / DELETEが呼ばれないことのテスト
    mock_supabase_execute.side_effect = [
        MagicMock(data=[{
            "id": "2ed74c12-624a-4040-8ef9-951b73d2ed69",
            "ticker": "4673",
            "disclosed_at": "2026-07-10T01:00:00+00:00",
            "dedupe_key": "c011d37aa037c16b7ad78fd2e031da012205363c",
            "pdf_url": "https://www.release.tdnet.info/inbs/140120260701585716.pdf",
            "primary_metric_value": "23.0百万円"
        }]), # select結果
        MagicMock(data=[{"id": "2ed74c12-624a-4040-8ef9-951b73d2ed69"}]) # update結果
    ]
    updates = {"primary_metric_value": "7.6億円"}
    update_tdnet_event_fields_by_identity(
        mock_client,
        id="2ed74c12-624a-4040-8ef9-951b73d2ed69",
        ticker="4673",
        disclosed_at="2026-07-10T01:00:00+00:00",
        dedupe_key="c011d37aa037c16b7ad78fd2e031da012205363c",
        pdf_url="https://www.release.tdnet.info/inbs/140120260701585716.pdf",
        updates=updates,
        dry_run=False
    )
    
    mock_client.table.return_value.insert.assert_not_called()
    mock_client.table.return_value.upsert.assert_not_called()
    mock_client.table.return_value.delete.assert_not_called()

def test_unaffected_rows_error(mock_client, mock_supabase_execute):
    # 15. affected rowsが1件以外なら成功扱いにしないことのテスト (0件の場合)
    mock_supabase_execute.side_effect = [
        MagicMock(data=[{
            "id": "uuid-123",
            "ticker": "4673",
            "disclosed_at": "datetime-123",
            "dedupe_key": "key-123",
            "pdf_url": "url-123",
            "primary_metric_value": "23.0百万円"
        }]), # select結果
        MagicMock(data=[]) # update結果 (0件)
    ]
    updates = {"primary_metric_value": "7.6億円"}
    
    res = update_tdnet_event_fields_by_identity(
        mock_client,
        id="uuid-123",
        ticker="4673",
        disclosed_at="datetime-123",
        dedupe_key="key-123",
        pdf_url="url-123",
        updates=updates,
        dry_run=False
    )
    
    assert res["status"] == "error"
    assert res["affected_rows"] == 0
    assert "Affected rows is not exactly 1" in res["stop_reason"]
