"""tests/test_db_config.py — db.py read/write config 分離テスト"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


class TestWriteConfigRequiresServiceRoleKey:
    """write config は SUPABASE_SERVICE_ROLE_KEY 必須。"""

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "srv_key_xxx",
    }, clear=False)
    def test_write_config_returns_service_role(self):
        from lib.pipeline.db import get_supabase_write_config
        cfg = get_supabase_write_config()
        assert cfg is not None
        assert cfg["key"] == "srv_key_xxx"
        assert "apikey" in cfg["headers"]
        assert cfg["headers"]["apikey"] == "srv_key_xxx"

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=True)
    def test_write_config_none_when_no_service_role(self):
        """service_role_key がなければ write config は None。"""
        # clear=True で全環境変数クリアし、URL + anon だけセット
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "anon_key_yyy"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        from lib.pipeline.db import get_supabase_write_config
        cfg = get_supabase_write_config()
        assert cfg is None

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
    }, clear=True)
    def test_write_config_none_when_no_keys(self):
        """key が何もなければ write config は None。"""
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        os.environ.pop("SUPABASE_ANON_KEY", None)

        from lib.pipeline.db import get_supabase_write_config
        cfg = get_supabase_write_config()
        assert cfg is None


class TestWriteHelperDoesNotFallbackToAnon:
    """write ヘルパーは anon key へ fallback しない。"""

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=True)
    def test_insert_fails_without_service_role(self):
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "anon_key_yyy"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        from lib.pipeline.db import supabase_insert
        result = supabase_insert("test_table", {"foo": "bar"})
        assert result["ok"] is False
        assert result["error"] == "no_write_config"

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=True)
    def test_upsert_fails_without_service_role(self):
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "anon_key_yyy"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        from lib.pipeline.db import supabase_upsert
        result = supabase_upsert("test_table", {"foo": "bar"})
        assert result["ok"] is False
        assert result["error"] == "no_write_config"

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=True)
    def test_update_fails_without_service_role(self):
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "anon_key_yyy"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        from lib.pipeline.db import supabase_update
        result = supabase_update("test_table", {"foo": "bar"}, params={"id": "eq.1"})
        assert result is False


class TestReadConfig:
    """read config は anon key でも OK。"""

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=True)
    def test_read_config_uses_anon(self):
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "anon_key_yyy"
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

        from lib.pipeline.db import get_supabase_read_config
        cfg = get_supabase_read_config()
        assert cfg is not None
        assert cfg["key"] == "anon_key_yyy"

    @patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "srv_key_xxx",
        "SUPABASE_ANON_KEY": "anon_key_yyy",
    }, clear=False)
    def test_read_config_prefers_service_role(self):
        from lib.pipeline.db import get_supabase_read_config
        cfg = get_supabase_read_config()
        assert cfg["key"] == "srv_key_xxx"


class TestLoadEnv:
    def test_load_env_reads_dotenv(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_DB_VAR_X=hello\n", encoding="utf-8")

        # clear the flag to allow re-loading
        import lib.pipeline.db as db_mod
        db_mod._env_loaded = False
        os.environ.pop("TEST_DB_VAR_X", None)

        db_mod.load_env(str(tmp_path))
        assert os.environ.get("TEST_DB_VAR_X") == "hello"

        # cleanup
        os.environ.pop("TEST_DB_VAR_X", None)
        db_mod._env_loaded = False

    def test_load_env_local_overrides(self, tmp_path):
        """env.local が .env より優先される。"""
        (tmp_path / ".env").write_text("TEST_DB_PRIO=from_env\n", encoding="utf-8")
        (tmp_path / ".env.local").write_text("TEST_DB_PRIO=from_local\n", encoding="utf-8")

        import lib.pipeline.db as db_mod
        db_mod._env_loaded = False
        os.environ.pop("TEST_DB_PRIO", None)

        db_mod.load_env(str(tmp_path))
        assert os.environ.get("TEST_DB_PRIO") == "from_local"

        # cleanup
        os.environ.pop("TEST_DB_PRIO", None)
        db_mod._env_loaded = False
