# ============================================================
# downloader.py — PDF/XBRL取得
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from src.cache.cache_manager import make_cache_key, load_binary, save_binary, get_path as get_cache_path
import shutil

import requests
from lib.pipeline.retry_helper import with_retry

logger = logging.getLogger("tdnet")

_USER_AGENT = "TDnetExcelInput/1.0"
_MAX_SIZE_MB = 30


@dataclass
class DownloadResult:
    """ダウンロード結果の詳細。

    成功時: success=True, path=保存先
    失敗時: success=False, error_class=分類名
    """
    success: bool
    path: str | None = None
    error_class: str = ""    # "not_found" | "forbidden" | "server_error" | "network_error" | "size_exceeded" | ""
    status_code: int = 0


def _classify_http_error(status_code: int) -> str:
    """HTTP ステータスコードからエラー分類を返す。"""
    if status_code == 404:
        return "not_found"
    elif status_code in (401, 403):
        return "forbidden"
    elif status_code == 405:
        return "method_not_allowed"
    elif 400 <= status_code < 500:
        return f"client_error_{status_code}"
    elif 500 <= status_code < 600:
        return "server_error"
    return f"http_{status_code}"



def resolve_cached_document_path(url: str, primary_dir: str, alternate_dirs: list[str] | None = None) -> str | None:
    filename = url.split("/")[-1].split("?")[0]
    if not filename:
        filename = "document.pdf"
        
    primary_path = Path(primary_dir) / filename
    if primary_path.exists():
        return str(primary_path)
        
    if alternate_dirs:
        for alt_dir in alternate_dirs:
            alt_path = Path(alt_dir) / filename
            if alt_path.exists():
                return str(alt_path)
                
    return None

def download_document(url: str, save_dir: str, session: requests.Session | None = None, alternate_paths: list[str] | None = None) -> str | None:
    result = download_document_ex(url, save_dir, session=session, alternate_paths=alternate_paths)
    return result.path if result.success else None

@with_retry(max_tries=3, status_forcelist=(429, 500, 502, 503, 504), backoff_factor=1.0)
def download_document_ex(url: str, save_dir: str, session: requests.Session | None = None, alternate_paths: list[str] | None = None) -> DownloadResult:
    try:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)

        filename = url.split("/")[-1].split("?")[0]
        if not filename:
            filename = "document.pdf"

        local_path = save_path / filename
        cache_type = "xbrl" if filename.lower().endswith(".zip") else "pdf"
        source = f"tdnet_{cache_type}"
        c_key = make_cache_key(source, url=url)

        # A. cache/ を確認
        cached_data = load_binary(cache_type, c_key)
        if cached_data:
            # B. ヒットしたら save_dir 側へ必要に応じてコピーして返す
            if not local_path.exists():
                with open(local_path, "wb") as f:
                    f.write(cached_data)
            logger.info(f"[DL] 既にダウンロード済み (new cache hit): {local_path}")
            return DownloadResult(success=True, path=str(local_path))

        # C. cache/ ミスなら既存 save_dir / alternate_paths を確認
        legacy_cached_path = resolve_cached_document_path(url, save_dir, alternate_paths)
        if legacy_cached_path:
            # D. 既存ファイルがあれば、それを利用し、cache/ にも裏書きする
            logger.info(f"[DL] 既にダウンロード済み (legacy cache hit): {legacy_cached_path}")
            with open(legacy_cached_path, "rb") as f:
                legacy_data = f.read()
            save_binary(cache_type, c_key, legacy_data)
            
            # もし legacy が save_dir と違う場所なら、save_dirにも一応コピーしておく（既存の挙動維持のため）
            if str(legacy_cached_path) != str(local_path) and not local_path.exists():
                with open(local_path, "wb") as f:
                    f.write(legacy_data)
                    
            return DownloadResult(success=True, path=str(local_path))

        # E. どちらにもなければネットワーク取得
        logger.info(f"[DL] ダウンロード中: {url}")
        client = session or requests
        resp = client.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=60,
            stream=True,
        )

        if resp.status_code != 200:
            error_class = _classify_http_error(resp.status_code)
            logger.info(f"[DL] HTTP {resp.status_code} ({error_class}): {url}")
            return DownloadResult(
                success=False, error_class=error_class,
                status_code=resp.status_code,
            )

        # サイズチェック
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_SIZE_MB * 1024 * 1024:
            logger.warning(f"[DL] ファイルサイズ超過: {int(content_length) / 1024 / 1024:.1f}MB")
            return DownloadResult(success=False, error_class="size_exceeded")

        # F. 取得成功後、save_dir と cache/ の両方へ保存
        total = 0
        chunks = []
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                chunks.append(chunk)
                total += len(chunk)

        if total > _MAX_SIZE_MB * 1024 * 1024:
            logger.warning(f"[DL] ストリーミング中にサイズ超過: {total / 1024 / 1024:.1f}MB")
            local_path.unlink(missing_ok=True)
            return DownloadResult(success=False, error_class="size_exceeded")

        # cache へも保存
        full_data = b"".join(chunks)
        save_binary(cache_type, c_key, full_data)

        # G. 既存の戻り値で返す
        return DownloadResult(success=True, path=str(local_path))

    except requests.exceptions.RequestException as e:
        logger.error(f"[DL] ネットワークエラー: {url} - {e}")
        return DownloadResult(success=False, error_class="network_error")
    except Exception as e:
        logger.error(f"[DL] 予期せぬエラー: {url} - {e}")
        return DownloadResult(success=False, error_class="network_error")
