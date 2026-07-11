import os
import ast
import pytest
from unittest.mock import MagicMock, patch
from src.segment.zip_identity_verifier import verify_zip_identity
from src.segment.segment_zip_resolver import ZipResolveResult
from tests.test_earnings_canonical_sync import make_identity_test_zip


def _make_dummy_zip_with_custom_name(tmp_path, name, content=b"dummy"):
    zip_path = tmp_path / name
    with open(zip_path, "wb") as f:
        f.write(content)
    return zip_path


# ==============================================================================
# Test AA: PYTEST_CURRENT_TEST の有無や値によらず、検証結果が不変であることを確認
# ==============================================================================
def test_aa_pytest_env_insensitivity(tmp_path):
    ok_zip = make_identity_test_zip(
        tmp_path, "20260709590505", "20260709590505", "7601", "2027-02-28", "1Q", "attachment_xbrl"
    )
    
    envs_to_test = ["", "tests/segment/test_something.py::TestA", "tests/other/test.py::TestRealtimeSegmentIdentity"]
    results = []
    
    for val in envs_to_test:
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": val}):
            res = verify_zip_identity(
                zip_path=str(ok_zip),
                requested_disclosure_no="20260709590505",
                expected_ticker="7601",
                expected_period="2027-02-28",
                expected_quarter="1Q",
                trusted_provenance=None
            )
            results.append((res.passed, res.rejection_reason))
            
    # 全環境変数状態で結果が完全に一致すること
    assert len(set(results)) == 1


# ==============================================================================
# Test AB: 一致する basename ファイル名を持つが存在しない ZIP の拒否
# ==============================================================================
def test_ab_missing_zip_rejection():
    res = verify_zip_identity(
        zip_path="C:/non_existent_directory/20260709590505.zip",
        requested_disclosure_no="20260709590505",
        expected_ticker="7601",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=None
    )
    assert res.passed is False
    assert res.rejection_reason == "zip_not_found"


# ==============================================================================
# Test AC: 一致する basename の 0バイト ZIP の拒否
# ==============================================================================
def test_ac_zero_byte_zip_rejection(tmp_path):
    zero_zip = _make_dummy_zip_with_custom_name(tmp_path, "20260709590505.zip", b"")
    res = verify_zip_identity(
        zip_path=str(zero_zip),
        requested_disclosure_no="20260709590505",
        expected_ticker="7601",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=None
    )
    assert res.passed is False
    assert res.rejection_reason == "zero_byte_zip"


# ==============================================================================
# Test AD: 一致する basename の 破損 ZIP の拒否
# ==============================================================================
def test_ad_broken_zip_rejection(tmp_path):
    broken_zip = _make_dummy_zip_with_custom_name(tmp_path, "20260709590505.zip", b"broken header and not zip format")
    res = verify_zip_identity(
        zip_path=str(broken_zip),
        requested_disclosure_no="20260709590505",
        expected_ticker="7601",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=None
    )
    assert res.passed is False
    assert res.rejection_reason == "broken_zip"


# ==============================================================================
# Test AE: 内部 ID 不一致かつ provenance なしの拒否
# ==============================================================================
def test_ae_provenance_missing_rejection(tmp_path):
    # 7601の開示情報で、内部IDが 20260709590450 (9982のID) になっている不整合ZIP
    invalid_zip = make_identity_test_zip(
        tmp_path, "20260709590505", "20260709590450", "7601", "2027-02-28", "1Q", "attachment_xbrl"
    )
    res = verify_zip_identity(
        zip_path=str(invalid_zip),
        requested_disclosure_no="20260709590505",
        expected_ticker="7601",
        expected_period="2027-02-28",
        expected_quarter="1Q",
        trusted_provenance=None
    )
    assert res.passed is False
    assert res.rejection_reason == "provenance_missing"


# ==============================================================================
# Test AF: PYTEST_CURRENT_TEST が TestRealtimeSegmentIdentity であっても不正 ZIP は拒否
# ==============================================================================
def test_af_pytest_test_name_bypass_removal(tmp_path):
    broken_zip = _make_dummy_zip_with_custom_name(tmp_path, "20260709590505.zip", b"broken")
    
    with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "tests/test_earnings_canonical_sync.py::TestRealtimeSegmentIdentity"}):
        res = verify_zip_identity(
            zip_path=str(broken_zip),
            requested_disclosure_no="20260709590505",
            expected_ticker="7601",
            expected_period="2027-02-28",
            expected_quarter="1Q",
            trusted_provenance=None
        )
        assert res.passed is False
        assert res.rejection_reason == "broken_zip"


# ==============================================================================
# Test AG: sequential 経路で、開示1件につき resolve_xbrl_zip の呼び出しが最大1回
# ==============================================================================
@patch("src.events.earnings_production_pipeline._sync_canonical_segments")
@patch("src.events.earnings_production_pipeline._sync_canonical_financials")
@patch("src.events.earnings_production_pipeline._find_cached_xbrl")
@patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
@patch("src.events.earnings_production_pipeline.load_json")
@patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
def test_ag_sequential_resolve_once(m_resolve, m_load, m_save, m_cache, m_sync_fin, m_sync_seg, tmp_path):
    from src.events.earnings_production_pipeline import run_earnings_production
    from tests.test_earnings_canonical_sync import DummyDoc
    import sqlite3
    
    conn = sqlite3.connect(":memory:")
    from src.events.earnings_summary_storage import ensure_earnings_summary_table
    ensure_earnings_summary_table(conn)
    
    m_cache.return_value = "C:/xbrl_archive/20260709590505.zip"
    m_save.return_value = {"action": "inserted"}
    
    m_resolve.return_value = ZipResolveResult(
        zip_path="C:/xbrl_archive/20260709590505.zip",
        source="tdnet_cache",
        status="FOUND_CACHE",
        error_reason="",
        cache_hit=True,
        downloaded=False,
        requested_disclosure_no="20260709590505",
        zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
        trusted_provenance=None,
        resolution_kind="exact_cache"
    )
    
    m_load.return_value = {
        "earnings": {"sales_current": 100, "op_current": 10},
        "company_name": "Test Poplar",
        "fiscal_year": "2027",
        "quarter": "1Q",
    }
    
    doc = DummyDoc("7601", "2027年2月期 第1四半期決算短信［日本基準］(連結)", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
    doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
    
    with patch.dict(os.environ, {"USE_SUBPROCESS_WORKER": "0"}):
        run_earnings_production([doc], conn, webhook_url="")
        
    assert m_resolve.call_count == 1


# ==============================================================================
# Test AH: subprocess 経路で、開示1件につき resolve_xbrl_zip の呼び出しが最大1回
# ==============================================================================
@patch("src.events.earnings_production_pipeline._sync_canonical_segments")
@patch("src.events.earnings_production_pipeline._sync_canonical_financials")
@patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
@patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
def test_ah_subprocess_resolve_once(m_resolve, m_save, m_sync_fin, m_sync_seg, tmp_path):
    from src.events.earnings_production_pipeline import run_earnings_production
    from tests.test_earnings_canonical_sync import DummyDoc
    import sqlite3
    
    conn = sqlite3.connect(":memory:")
    from src.events.earnings_summary_storage import ensure_earnings_summary_table
    ensure_earnings_summary_table(conn)
    
    m_save.return_value = {"action": "inserted"}
    m_resolve.return_value = ZipResolveResult(
        zip_path="C:/xbrl_archive/20260709590450.zip",
        source="tdnet_cache",
        status="FOUND_CACHE",
        error_reason="",
        cache_hit=True,
        downloaded=False,
        requested_disclosure_no="20260709590450",
        zip_sha256="719f1592f98cd05c2a60601726e3635f5495af899c188693f85ce487dae0a5b5",
        trusted_provenance=None,
        resolution_kind="exact_cache"
    )
    
    with patch("src.events.earnings_subprocess_runner.run_earnings_subprocess_dry_run") as m_run, \
         patch("src.events.earnings_subprocess_runner.build_save_ready_payload") as m_payload, \
         patch("src.events.earnings_subprocess_runner.validate_save_ready_payload") as m_valid, \
         patch("src.events.earnings_subprocess_runner.build_save_call_plan") as m_plan, \
         patch("src.events.earnings_subprocess_runner.validate_save_call_plan") as m_cp_valid, \
         patch("src.events.earnings_subprocess_runner.build_discord_call_plan") as m_discord_plan, \
         patch("src.events.tdnet_event_store.save_event_to_supabase") as m_supa, \
         patch("src.events.earnings_production_pipeline._find_cached_xbrl") as m_cache:
         
        m_run.return_value = {"results": [{"ticker": "9982", "status": "ok"}]}
        m_valid.return_value = (True, "")
        m_cp_valid.return_value = (True, "")
        m_cache.return_value = "C:/xbrl_archive/20260709590450.zip"
        
        m_payload.return_value = {"extracted": {"period": "2027-02-28", "guidance": {}}}
        m_plan.return_value = {
            "earnings_summary_args": {
                "ticker": "9982", "title": "決算短信", "quarter": "1Q", "sales_value": 10, "op_value": 2,
                "fingerprint": "1", "company_name": "T", "fiscal_year": "2027", "disclosure_date": "2026-07-10"
            },
            "tdnet_event_payload": {
                "source_doc_id": "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c",
                "source_url": "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
            }
        }
        m_discord_plan.return_value = {"discord_message": "test"}
        m_supa.return_value = {"action": "inserted"}
        
        doc = DummyDoc("9982", "決算短信", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
        doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590450.pdf"
        
        with patch.dict(os.environ, {"USE_SUBPROCESS_WORKER": "1", "EARNINGS_SUBPROCESS_ENABLE_REAL_SAVE": "1", "EARNINGS_SUBPROCESS_ALLOWLIST": "9982"}):
            run_earnings_production([doc], conn, webhook_url="")
            
        assert m_resolve.call_count == 1


# ==============================================================================
# Test AI: canonical_retry 経路で、開示1件につき resolve_xbrl_zip の呼び出しが最大1回
# ==============================================================================
@patch("src.events.earnings_production_pipeline._sync_canonical_segments")
@patch("src.events.earnings_production_pipeline._sync_canonical_financials")
@patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events")
@patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
def test_ai_canonical_retry_resolve_once(m_resolve, m_save, m_sync_fin, m_sync_seg, tmp_path):
    from src.events.earnings_production_pipeline import run_earnings_production
    from tests.test_earnings_canonical_sync import DummyDoc
    import sqlite3
    
    conn = sqlite3.connect(":memory:")
    from src.events.earnings_summary_storage import ensure_earnings_summary_table
    ensure_earnings_summary_table(conn)
    
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    
    m_save.return_value = {"action": "dedup_skipped"}
    m_resolve.return_value = ZipResolveResult(
        zip_path="C:/xbrl_archive/20260709590505.zip",
        source="tdnet_cache",
        status="FOUND_CACHE",
        error_reason="",
        cache_hit=True,
        downloaded=False,
        requested_disclosure_no="20260709590505",
        zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
        trusted_provenance=None,
        resolution_kind="exact_cache"
    )
    
    doc = DummyDoc("7601", "決算短信", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
    doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
    
    with patch("src.events.tdnet_event_store._get_supabase", return_value=mock_client), \
         patch("src.events.earnings_production_pipeline._find_cached_xbrl", return_value="C:/xbrl_archive/20260709590505.zip"), \
         patch("src.events.earnings_production_pipeline.load_json") as m_load:
         
        m_load.return_value = {
            "earnings": {"sales_current": 100, "op_current": 10},
            "company_name": "Test Poplar", "fiscal_year": "2027", "quarter": "1Q",
        }
        
        with patch.dict(os.environ, {"USE_SUBPROCESS_WORKER": "0"}):
            run_earnings_production([doc], conn, webhook_url="")
            
        assert m_resolve.call_count == 1


# ==============================================================================
# Test AJ: resolver の返す同一オブジェクトが verifier と extractor へ伝播していることを確認
# ==============================================================================
@patch("src.segment.zip_identity_verifier.verify_zip_identity")
@patch("src.events.earnings_production_pipeline._sync_canonical_segments")
@patch("src.segment.segment_zip_resolver.resolve_xbrl_zip")
def test_aj_resolver_object_propagation(m_resolve, m_sync_seg, m_verify, tmp_path):
    from src.events.earnings_production_pipeline import run_earnings_production
    from tests.test_earnings_canonical_sync import DummyDoc
    import sqlite3
    
    conn = sqlite3.connect(":memory:")
    from src.events.earnings_summary_storage import ensure_earnings_summary_table
    ensure_earnings_summary_table(conn)
    
    m_prov = MagicMock() # ダミーの TrustedProvenance オブジェクト
    
    m_resolve.return_value = ZipResolveResult(
        zip_path="C:/xbrl_archive/20260709590505.zip",
        source="tdnet_cache",
        status="FOUND_CACHE",
        error_reason="",
        cache_hit=True,
        downloaded=False,
        requested_disclosure_no="20260709590505",
        zip_sha256="c2349e5d0d17ac367cf104ad693382f05c086d4e81b2832cab0dc799a53d20f4",
        trusted_provenance=m_prov,
        resolution_kind="exact_cache"
    )
    
    m_verify.return_value.passed = True
    
    doc = DummyDoc("7601", "決算短信", "b1d3fde97cd38cbc6b530102c4dae7da067ace852914d372344462709495123c")
    doc.doc_url = "https://www.release.tdnet.info/inbs/140120260709590505.pdf"
    
    with patch("src.events.earnings_production_pipeline._find_cached_xbrl", return_value="C:/xbrl_archive/20260709590505.zip"), \
         patch("src.events.earnings_production_pipeline.load_json") as m_load, \
         patch("src.events.earnings_production_pipeline._save_earnings_to_tdnet_events") as m_save, \
         patch("src.events.earnings_production_pipeline._sync_canonical_financials"):
         
        m_save.return_value = {"action": "inserted"}
        m_load.return_value = {
            "earnings": {"sales_current": 100, "op_current": 10},
            "company_name": "Test Poplar", "fiscal_year": "2027", "quarter": "1Q",
        }
        
        with patch.dict(os.environ, {"USE_SUBPROCESS_WORKER": "0"}):
            run_earnings_production([doc], conn, webhook_url="")
            
    # 同一の provenance オブジェクトが _sync_canonical_segments に伝播していること
    assert m_sync_seg.call_args[1]["trusted_provenance"] is m_prov


# ==============================================================================
# Test AK & AL: AST (抽象構文木) 解析によるコード構造の厳格な検証
# ==============================================================================
def test_ak_al_static_code_structure():
    pipeline_path = os.path.join("src", "events", "earnings_production_pipeline.py")
    with open(pipeline_path, "r", encoding="utf-8") as f:
        source = f.read()
        
    tree = ast.parse(source)
    
    # ── Test AL: 環境変数やテスト名により resolver をスキップ（バイパス）する条件分岐の有無監査 ──
    # 本番コード内に "PYTEST_CURRENT_TEST" または "use_legacy_zip_resolve" に依存する
    # 条件分岐が一切存在しないこと（監査結果 0件 の静的検証）
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ("use_legacy_zip_resolve", "PYTEST_CURRENT_TEST"):
            # L.1601 など、テスト用 Mock 記述以外に本番コード（earnings_production_pipeline.py）内に残存していないか検証
            raise AssertionError(f"Deprecated symbol '{node.id}' found at line {node.lineno}")
            
    # ── Test AK: すべての _sync_canonical_segments 呼び出しにおいて trusted_provenance 引数が指定されているか検証 ──
    class SyncCallVisitor(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "_sync_canonical_segments":
                # キーワード引数に 'trusted_provenance' が含まれていることを確認
                kwargs = [k.arg for k in node.keywords if k.arg is not None]
                if "trusted_provenance" not in kwargs:
                    raise AssertionError(
                        f"Call to '_sync_canonical_segments' at line {node.lineno} is missing "
                        f"'trusted_provenance' keyword argument."
                    )
            self.generic_visit(node)
            
    SyncCallVisitor().visit(tree)
