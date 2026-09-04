"""lib/backfill/edinet_xbrl_cache.py — EDINET XBRL ZIP キャッシュ管理

EDINET からダウンロードした XBRL ZIP を
`data/edinet_cache/{doc_id}/xbrl.zip` に保存・管理する。

API key 未設定時でも cache hit は使えるため、
手動配置や別経路で入ったファイルも活用可能。
"""
from __future__ import annotations
from lib.runtime_paths import runtime_path

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("backfill.edinet.cache")

JST = timezone(timedelta(hours=9))


class EdinetXbrlCache:
    """EDINET XBRL ZIP のファイルキャッシュ。"""

    def __init__(self, cache_root: str | None = None) -> None:
        self._root = runtime_path(cache_root or os.environ.get(
            "EDINET_CACHE_DIR", "data/edinet_cache"
        ))

    @property
    def root(self) -> Path:
        return self._root

    def get_cache_dir(self, doc_id: str) -> Path:
        """doc_id 別の cache ディレクトリ。"""
        return self._root / doc_id

    def get_xbrl_zip_path(self, doc_id: str) -> Path:
        """XBRL ZIP の cache パス。"""
        return self.get_cache_dir(doc_id) / "xbrl.zip"

    def has_xbrl_zip(self, doc_id: str) -> bool:
        """cache 済みかどうか。"""
        p = self.get_xbrl_zip_path(doc_id)
        return p.exists() and p.stat().st_size > 0

    def load_cached_xbrl_zip(self, doc_id: str) -> Optional[Path]:
        """cache 済みなら Path を返す。なければ None。"""
        if self.has_xbrl_zip(doc_id):
            return self.get_xbrl_zip_path(doc_id)
        return None

    def save_xbrl_zip(self, doc_id: str, data: bytes) -> Path:
        """XBRL ZIP を cache に保存。metadata も記録。"""
        d = self.get_cache_dir(doc_id)
        d.mkdir(parents=True, exist_ok=True)
        p = self.get_xbrl_zip_path(doc_id)
        p.write_bytes(data)

        # metadata 保存
        meta = {
            "edinet_doc_id": doc_id,
            "downloaded_at": datetime.now(JST).isoformat(),
            "source": "edinet",
            "size_bytes": len(data),
        }
        meta_path = d / "metadata.json"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            f"[edinet_cache] saved: doc_id={doc_id} "
            f"size={len(data):,d} path={p}"
        )
        return p

    def get_cache_metadata(self, doc_id: str) -> dict:
        """cache metadata を読み込む。"""
        meta_path = self.get_cache_dir(doc_id) / "metadata.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}
