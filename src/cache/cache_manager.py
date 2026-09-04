import os
import json
import hashlib
import time
from pathlib import Path
from logging import getLogger
from typing import Optional, Any
import sqlite3
from lib.runtime_paths import runtime_path

logger = getLogger(__name__)

CACHE_ROOT = Path("cache")
PDF_DIR = CACHE_ROOT / "pdf"
XBRL_DIR = CACHE_ROOT / "xbrl"
HTML_DIR = CACHE_ROOT / "html"
PARSED_DIR = CACHE_ROOT / "parsed"

def _get_dir_by_type(cache_type: str) -> Path:
    if cache_type == "pdf":
        return runtime_path(PDF_DIR)
    if cache_type == "xbrl":
        return runtime_path(XBRL_DIR)
    if cache_type == "html":
        return runtime_path(HTML_DIR)
    if cache_type == "parsed":
        return runtime_path(PARSED_DIR)
    raise ValueError(f"invalid cache type: {cache_type}")

def _extract_tdnet_id_from_url(url: str) -> str:
    """TDNET_IDをURLから抽出（例: 140120260618574005）"""
    filename = url.split("/")[-1].split("?")[0]
    return filename.replace(".pdf", "").replace(".zip", "").replace(".html", "")

def make_cache_key(source: str, url: str = "", doc_id: str = "") -> str:
    """
    source例: tdnet_pdf, tdnet_xbrl, tdnet_doc_html, tdnet_parsed:earnings_pipeline_v4_2c_001
    """
    target_id = doc_id
    if not target_id and url:
        # doc_id がない場合は URL から TDNET_ID の抽出を試みる
        extracted = _extract_tdnet_id_from_url(url)
        if extracted and len(extracted) > 10:
            target_id = extracted
        else:
            # それでも取れない場合は URL の SHA256 ハッシュを fallback_key とする
            target_id = hashlib.sha256(url.encode("utf-8")).hexdigest()

    raw = f"{source}:{target_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def get_path(cache_type: str, key: str) -> Path:
    dir_path = _get_dir_by_type(cache_type)
    if cache_type == "parsed":
        return dir_path / f"{key}.json"
    if cache_type == "html":
        return dir_path / f"{key}.html"
    if cache_type == "pdf":
        return dir_path / f"{key}.pdf"
    if cache_type == "xbrl":
        return dir_path / f"{key}.zip"
    return dir_path / key

def exists(cache_type: str, key: str) -> bool:
    return get_path(cache_type, key).exists()

def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise e

def _get_conn(conn: Optional[sqlite3.Connection]) -> Optional[sqlite3.Connection]:
    if conn: return conn
    try:
        from src.config import config
        if config.state_db_path:
            return sqlite3.connect(config.state_db_path, timeout=5)
    except Exception:
        pass
    return None

def save_binary(cache_type: str, key: str, content: bytes, conn: Optional[sqlite3.Connection] = None):
    # 保存条件: 0 byte は保存しない
    if not content:
        return
        
    path = get_path(cache_type, key)
    atomic_write(path, content)
    logger.debug(f"[CACHE_WRITE] cache_type={cache_type} key={key} size={len(content)}")
    
    db_conn = _get_conn(conn)
    if db_conn:
        _init_cache_stats(db_conn)
        _log_cache_write(db_conn, key, cache_type, str(path), len(content))
        if not conn: db_conn.close()

def load_binary(cache_type: str, key: str, conn: Optional[sqlite3.Connection] = None) -> Optional[bytes]:
    path = get_path(cache_type, key)
    # Reading cache must not implicitly open SQLite (including WAL/SHM).
    # A caller may explicitly supply its existing connection for statistics.
    db_conn = conn
    
    if not path.exists():
        logger.debug(f"[CACHE_MISS] cache_type={cache_type} key={key}")
        if db_conn:
            _bump_cache_miss(db_conn, key, cache_type)
            if not conn: db_conn.close()
        return None
        
    try:
        with open(path, "rb") as f:
            data = f.read()
        logger.debug(f"[CACHE_HIT] cache_type={cache_type} key={key}")
        if conn:
            _bump_cache_hit(conn, key)
        return data
    except Exception as e:
        logger.warning(f"Failed to read cache {path}: {e}")
        return None

def save_json(key: str, data: Any, conn: Optional[sqlite3.Connection] = None):
    # 保存条件: dict/list として空のものは保存しない（抽出失敗扱い）
    if not data:
        return
        
    if isinstance(data, dict) and not any(data.values()):
        return
        
    path = get_path("parsed", key)
    content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    atomic_write(path, content)
    logger.debug(f"[CACHE_WRITE] cache_type=parsed key={key}")
    
    db_conn = _get_conn(conn)
    if db_conn:
        _init_cache_stats(db_conn)
        _log_cache_write(db_conn, key, "parsed", str(path), len(content))
        if not conn: db_conn.close()

def load_json(key: str, conn: Optional[sqlite3.Connection] = None) -> Optional[Any]:
    path = get_path("parsed", key)
    db_conn = conn
    if not path.exists():
        logger.debug(f"[CACHE_MISS] cache_type=parsed key={key}")
        if db_conn:
            _bump_cache_miss(db_conn, key, "parsed")
            if not conn: db_conn.close()
        return None
        
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.debug(f"[CACHE_HIT] cache_type=parsed key={key}")
        if db_conn:
            _bump_cache_hit(db_conn, key)
            if not conn: db_conn.close()
        return data
    except Exception as e:
        logger.warning(f"Failed to read json cache {path}: {e}")
        return None

# DB stats logic
def _init_cache_stats(conn: sqlite3.Connection):
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_stats (
                cache_key TEXT PRIMARY KEY,
                cache_type TEXT,
                hit_count INTEGER DEFAULT 0,
                miss_count INTEGER DEFAULT 0,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_path TEXT,
                size_bytes INTEGER
            );
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to init cache_stats: {e}")

def _bump_cache_hit(conn: sqlite3.Connection, key: str):
    try:
        conn.execute("""
            INSERT INTO cache_stats(cache_key, hit_count, last_used_at)
            VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(cache_key)
            DO UPDATE SET
                hit_count = hit_count + 1,
                last_used_at = CURRENT_TIMESTAMP
        """, (key,))
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to bump cache hit for {key}: {e}")

def _bump_cache_miss(conn: sqlite3.Connection, key: str, cache_type: str):
    try:
        _init_cache_stats(conn)
        conn.execute("""
            INSERT INTO cache_stats(cache_key, cache_type, miss_count, last_used_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(cache_key)
            DO UPDATE SET
                miss_count = miss_count + 1,
                last_used_at = CURRENT_TIMESTAMP
        """, (key, cache_type))
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to bump cache miss for {key}: {e}")

def _log_cache_write(conn: sqlite3.Connection, key: str, cache_type: str, path: str, size: int):
    try:
        conn.execute("""
            INSERT INTO cache_stats(cache_key, cache_type, created_at, last_path, size_bytes)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(cache_key)
            DO UPDATE SET
                last_path = excluded.last_path,
                size_bytes = excluded.size_bytes,
                created_at = CURRENT_TIMESTAMP
        """, (key, cache_type, path, size))
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to log cache write for {key}: {e}")
