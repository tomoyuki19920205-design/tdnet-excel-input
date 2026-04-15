#!/usr/bin/env python3
"""tests/test_state_io.py — tools/state_io の単体テスト"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# テスト対象のインポートパスを解決
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.state_io import atomic_json_save, safe_json_load


# ============================================================
# safe_json_load
# ============================================================

class TestSafeJsonLoad:
    """safe_json_load のテスト群。"""

    def test_file_not_exists_returns_default(self, tmp_path: Path):
        """ファイルが存在しない場合、default を返す。"""
        result = safe_json_load(tmp_path / "no_such_file.json", default={"a": 1})
        assert result == {"a": 1}

    def test_file_not_exists_no_default_returns_empty_dict(self, tmp_path: Path):
        """ファイルが存在せず default 未指定の場合、{} を返す。"""
        result = safe_json_load(tmp_path / "no_such_file.json")
        assert result == {}

    def test_valid_json_returns_data(self, tmp_path: Path):
        """正常な JSON ファイルはそのまま返す。"""
        fp = tmp_path / "ok.json"
        fp.write_text('{"key": "value"}', encoding="utf-8")
        result = safe_json_load(fp)
        assert result == {"key": "value"}

    def test_valid_json_list(self, tmp_path: Path):
        """list を含む JSON も正しく読める。"""
        fp = tmp_path / "list.json"
        fp.write_text('[1, 2, 3]', encoding="utf-8")
        result = safe_json_load(fp, default=[])
        assert result == [1, 2, 3]

    def test_corrupt_json_returns_default(self, tmp_path: Path):
        """壊れた JSON ファイルは default を返す。"""
        fp = tmp_path / "corrupt.json"
        fp.write_text("{broken json", encoding="utf-8")
        result = safe_json_load(fp, default={"seen_doc_ids": []})
        assert result == {"seen_doc_ids": []}

    def test_corrupt_json_creates_backup(self, tmp_path: Path):
        """壊れた JSON は .corrupt.YYYYMMDD_HHMMSS.json に退避される。"""
        fp = tmp_path / "state.json"
        fp.write_text("{broken", encoding="utf-8")
        safe_json_load(fp, default={})

        # 元ファイルは消えている（rename されたため）
        assert not fp.exists()

        # .corrupt ファイルが作成されている
        corrupt_files = list(tmp_path.glob("state.corrupt.*.json"))
        assert len(corrupt_files) == 1
        assert corrupt_files[0].read_text(encoding="utf-8") == "{broken"

    def test_corrupt_json_no_default_returns_empty_dict(self, tmp_path: Path):
        """壊れた JSON で default 未指定時は {} を返す。"""
        fp = tmp_path / "bad.json"
        fp.write_text("not json at all", encoding="utf-8")
        result = safe_json_load(fp)
        assert result == {}


# ============================================================
# atomic_json_save
# ============================================================

class TestAtomicJsonSave:
    """atomic_json_save のテスト群。"""

    def test_save_and_read_back(self, tmp_path: Path):
        """保存後に正しく読み戻せる。"""
        fp = tmp_path / "test.json"
        data = {"key": "value", "count": 42}
        atomic_json_save(fp, data)

        loaded = json.loads(fp.read_text(encoding="utf-8"))
        assert loaded == data

    def test_japanese_content_preserved(self, tmp_path: Path):
        """日本語コンテンツが壊れない。"""
        fp = tmp_path / "jp.json"
        data = {"名前": "片山晃", "状態": "監視中"}
        atomic_json_save(fp, data)

        loaded = json.loads(fp.read_text(encoding="utf-8"))
        assert loaded["名前"] == "片山晃"
        assert loaded["状態"] == "監視中"

    def test_overwrite_existing_file(self, tmp_path: Path):
        """既存ファイルの上書きが正しく動作する。"""
        fp = tmp_path / "overwrite.json"
        atomic_json_save(fp, {"version": 1})
        atomic_json_save(fp, {"version": 2, "extra": True})

        loaded = json.loads(fp.read_text(encoding="utf-8"))
        assert loaded == {"version": 2, "extra": True}

    def test_creates_parent_directories(self, tmp_path: Path):
        """親ディレクトリが存在しない場合も作成する。"""
        fp = tmp_path / "sub" / "dir" / "deep.json"
        atomic_json_save(fp, {"nested": True})

        assert fp.exists()
        loaded = json.loads(fp.read_text(encoding="utf-8"))
        assert loaded == {"nested": True}

    def test_no_orphan_tmp_on_success(self, tmp_path: Path):
        """成功時に .tmp ファイルが残らない。"""
        fp = tmp_path / "clean.json"
        atomic_json_save(fp, {"ok": True})

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_save_list_data(self, tmp_path: Path):
        """list データも保存・読み戻しできる。"""
        fp = tmp_path / "list.json"
        data = [1, "two", {"three": 3}]
        atomic_json_save(fp, data)

        loaded = json.loads(fp.read_text(encoding="utf-8"))
        assert loaded == data
