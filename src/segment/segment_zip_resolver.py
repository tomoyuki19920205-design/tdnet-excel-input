import os
import glob
import time
import requests
import logging
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from src.jquants.adapter import get_file_url

logger = logging.getLogger(__name__)

@dataclass
class ZipResolveResult:
    zip_path: Optional[str]
    source: str
    status: str
    error_reason: str
    cache_hit: bool
    downloaded: bool

def resolve_xbrl_zip(
    doc_id: str,
    ticker: str,
    expected_quarter: str,
    local_archive_dir: str = "data/xbrl_archive",
    cache_dir: str = "data/tdnet_cache",
    allow_jquants_fetch: bool = True,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    timeout_seconds: float = 15.0
) -> ZipResolveResult:
    """
    Finds or downloads an XBRL zip file for a given doc_id.
    1. Checks local_archive_dir (data/xbrl_archive)
    2. Checks cache_dir (data/tdnet_cache)
    3. Fetches from J-Quants API if allow_jquants_fetch is True
    """
    # 1. Check local_archive
    archive_pattern = os.path.join(local_archive_dir, f"{ticker}_*", f"xbrl_{doc_id}.zip")
    matches = glob.glob(archive_pattern)
    if matches:
        return ZipResolveResult(
            zip_path=matches[0],
            source="local_archive",
            status="FOUND_LOCAL_ARCHIVE",
            error_reason="",
            cache_hit=True,
            downloaded=False
        )

    # Alternate naming convention in archive
    archive_pattern2 = os.path.join(local_archive_dir, f"{ticker}_*", f"*_{doc_id}.zip")
    matches2 = glob.glob(archive_pattern2)
    for m in matches2:
        if m.endswith(f"{doc_id}.zip"):
            return ZipResolveResult(
                zip_path=m,
                source="local_archive",
                status="FOUND_LOCAL_ARCHIVE",
                error_reason="",
                cache_hit=True,
                downloaded=False
            )
            
    # 2. Check tdnet_cache
    cache_path_dir = os.path.join(cache_dir, str(doc_id))
    cache_zip = os.path.join(cache_path_dir, "xbrl.zip")
    if os.path.exists(cache_zip) and os.path.getsize(cache_zip) > 0:
        return ZipResolveResult(
            zip_path=cache_zip,
            source="tdnet_cache",
            status="FOUND_CACHE",
            error_reason="",
            cache_hit=True,
            downloaded=False
        )
        
    # 3. Fetch from J-Quants
    if not allow_jquants_fetch:
        return ZipResolveResult(
            zip_path=None,
            source="not_found",
            status="SKIPPED_NO_FETCH_ALLOWED",
            error_reason="Not found locally and fetch disabled",
            cache_hit=False,
            downloaded=False
        )
        
    for attempt in range(max_retries):
        try:
            url_data = get_file_url(doc_id, 'x', timeout_sec=timeout_seconds)
            if not url_data or 'xbrl' not in url_data:
                return ZipResolveResult(
                    zip_path=None,
                    source="not_found",
                    status="JQUANTS_URL_NOT_FOUND",
                    error_reason="No xbrl URL returned from J-Quants",
                    cache_hit=False,
                    downloaded=False
                )
                
            url = url_data['xbrl']
            r = requests.get(url, stream=True, timeout=timeout_seconds)
            if r.status_code == 200:
                os.makedirs(cache_path_dir, exist_ok=True)
                with open(cache_zip, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return ZipResolveResult(
                    zip_path=cache_zip,
                    source="jquants_download",
                    status="DOWNLOADED_FROM_JQUANTS",
                    error_reason="",
                    cache_hit=False,
                    downloaded=True
                )
            elif r.status_code == 429:
                if attempt < max_retries - 1:
                    time.sleep(backoff_base_seconds * (2 ** attempt))
                    continue
                else:
                    return ZipResolveResult(
                        zip_path=None,
                        source="fetch_failed",
                        status="JQUANTS_RATE_LIMIT_RETRY_EXHAUSTED",
                        error_reason="Rate limit exceeded max retries",
                        cache_hit=False,
                        downloaded=False
                    )
            else:
                return ZipResolveResult(
                    zip_path=None,
                    source="fetch_failed",
                    status="JQUANTS_FETCH_FAILED",
                    error_reason=f"HTTP {r.status_code}",
                    cache_hit=False,
                    downloaded=False
                )
                
        except Exception as e:
            if '429' in str(e):
                if attempt < max_retries - 1:
                    time.sleep(backoff_base_seconds * (2 ** attempt))
                    continue
                else:
                    return ZipResolveResult(
                        zip_path=None,
                        source="fetch_failed",
                        status="JQUANTS_RATE_LIMIT_RETRY_EXHAUSTED",
                        error_reason="Rate limit exceeded max retries",
                        cache_hit=False,
                        downloaded=False
                    )
            return ZipResolveResult(
                zip_path=None,
                source="fetch_failed",
                status="JQUANTS_FETCH_FAILED",
                error_reason=str(e),
                cache_hit=False,
                downloaded=False
            )
            
    return ZipResolveResult(
        zip_path=None,
        source="fetch_failed",
        status="JQUANTS_FETCH_FAILED",
        error_reason="Unknown error during fetch",
        cache_hit=False,
        downloaded=False
    )
