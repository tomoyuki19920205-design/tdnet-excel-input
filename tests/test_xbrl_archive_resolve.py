"""tests/test_xbrl_archive_resolve.py — XBRL archive → cache 連携のテスト"""
import os
import json
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase, mock
from dataclasses import dataclass

from lib.backfill.cache import (
    CachePaths, ensure_cache_layout, resolve_xbrl_from_archive, has_xbrl,
)


def _make_cache(tmp: str, filing_id: str = "test_fid") -> CachePaths:
    """テスト用キャッシュを作成。"""
    return ensure_cache_layout(tmp, filing_id)


def _make_archive(tmp: str, ticker: str, count: int = 1) -> Path:
    """テスト用 xbrl_archive を作成。"""
    archive = Path(tmp) / "xbrl_archive"
    archive.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        name = f"{ticker}_2026031{i}_08122026031{i}58{i:04d}.zip"
        p = archive / name
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 100)  # miniature ZIP header
    return archive


class TestResolveXbrlFromArchive(TestCase):
    """resolve_xbrl_from_archive のユニットテスト。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_hit(self):
        """cache 内に xbrl.zip がある場合は cache を返す。"""
        paths = _make_cache(self.tmpdir)
        paths.xbrl_zip.write_bytes(b"PK" + b"\x00" * 50)

        xbrl_path, source = resolve_xbrl_from_archive("1234", paths)
        self.assertIsNotNone(xbrl_path)
        self.assertEqual(source, "cache")

    def test_archive_hit(self):
        """archive に ZIP がある場合は cache にコピーして archive を返す。"""
        paths = _make_cache(self.tmpdir)
        archive = _make_archive(self.tmpdir, "1234")

        xbrl_path, source = resolve_xbrl_from_archive(
            "1234", paths, archive_root=str(archive),
        )
        self.assertIsNotNone(xbrl_path)
        self.assertEqual(source, "archive")
        self.assertTrue(has_xbrl(paths))  # cache にコピーされた

    def test_archive_miss(self):
        """archive に該当 ticker の ZIP がない場合。"""
        paths = _make_cache(self.tmpdir)
        archive = _make_archive(self.tmpdir, "9999")  # 別 ticker

        xbrl_path, source = resolve_xbrl_from_archive(
            "1234", paths, archive_root=str(archive),
        )
        self.assertIsNone(xbrl_path)
        self.assertEqual(source, "none")

    def test_no_archive_dir(self):
        """archive ディレクトリが存在しない場合。"""
        paths = _make_cache(self.tmpdir)

        xbrl_path, source = resolve_xbrl_from_archive(
            "1234", paths, archive_root="/nonexistent/path",
        )
        self.assertIsNone(xbrl_path)
        self.assertEqual(source, "none")

    def test_archive_latest_preferred(self):
        """複数の archive ZIP がある場合、最新（日付降順）が選ばれる。"""
        paths = _make_cache(self.tmpdir)
        archive = _make_archive(self.tmpdir, "1234", count=3)

        xbrl_path, source = resolve_xbrl_from_archive(
            "1234", paths, archive_root=str(archive),
        )
        self.assertEqual(source, "archive")
        self.assertIsNotNone(xbrl_path)

    def test_empty_zip_ignored(self):
        """サイズ 0 の ZIP は無視される。"""
        paths = _make_cache(self.tmpdir)
        archive = Path(self.tmpdir) / "xbrl_archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "1234_20260313_08120001.zip").write_bytes(b"")  # empty

        xbrl_path, source = resolve_xbrl_from_archive(
            "1234", paths, archive_root=str(archive),
        )
        self.assertIsNone(xbrl_path)
        self.assertEqual(source, "none")


class TestDownloadOriginalsArchiveFallback(TestCase):
    """_download_originals の archive fallback テスト。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_archive_fallback_when_no_xbrl_url(self):
        """filing.xbrl_url が空でも archive に ZIP があれば xbrl_path を返す。"""
        from lib.backfill.worker import _download_originals

        paths = _make_cache(self.tmpdir)
        archive = _make_archive(self.tmpdir, "5678")

        @dataclass
        class FakeFiling:
            filing_id: str = "test_fid"
            ticker: str = "5678"
            doc_url: str = ""
            xbrl_url: str = ""

        filing = FakeFiling()
        metrics = {"attempts": {}}

        # Mock resolve_xbrl_from_archive to use our archive dir
        def _mock_resolve(ticker, cache_paths):
            return resolve_xbrl_from_archive(
                ticker, cache_paths, archive_root=str(archive),
            )

        with mock.patch(
            "lib.backfill.cache.resolve_xbrl_from_archive",
            side_effect=_mock_resolve,
        ):
            doc_path, xbrl_path = _download_originals(
                filing, paths, metrics,
                retry_download=1, timeout_download=5, sleep_fn=lambda x: None,
            )

        self.assertIsNotNone(xbrl_path)
        self.assertEqual(metrics["xbrl_source"], "archive")
        self.assertTrue(metrics["xbrl_archive_hit"])
        self.assertTrue(metrics["xbrl_resolved"])

    def test_no_archive_no_url_returns_none(self):
        """archive にも xbrl_url にもない場合は xbrl_path=None。"""
        from lib.backfill.worker import _download_originals

        paths = _make_cache(self.tmpdir)

        @dataclass
        class FakeFiling:
            filing_id: str = "test_fid"
            ticker: str = "0000"
            doc_url: str = ""
            xbrl_url: str = ""

        filing = FakeFiling()
        metrics = {"attempts": {}}

        with mock.patch(
            "lib.backfill.cache.resolve_xbrl_from_archive",
            return_value=(None, "none"),
        ):
            doc_path, xbrl_path = _download_originals(
                filing, paths, metrics,
                retry_download=1, timeout_download=5, sleep_fn=lambda x: None,
            )

        self.assertIsNone(xbrl_path)
        self.assertEqual(metrics["xbrl_source"], "none")
        self.assertFalse(metrics["xbrl_archive_hit"])
        self.assertFalse(metrics["xbrl_resolved"])

