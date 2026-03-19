# ============================================================
# downloader.py — PDF/XBRL取得
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests

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


def download_document(url: str, save_dir: str) -> str | None:
    """
    PDFまたはXBRLファイルをダウンロードしてローカルに保存する。

    Args:
        url: ダウンロードURL
        save_dir: 保存先ディレクトリ

    Returns:
        保存先パス（成功時）、None（失敗時）
    """
    result = download_document_ex(url, save_dir)
    return result.path if result.success else None


def download_document_ex(url: str, save_dir: str) -> DownloadResult:
    """ダウンロードの詳細結果を返すバージョン。

    HTTP エラーを分類して DownloadResult に記録する。
    """
    try:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)

        # URLからファイル名を推定
        filename = url.split("/")[-1].split("?")[0]
        if not filename:
            filename = "document.pdf"

        local_path = save_path / filename

        # 既にダウンロード済みならスキップ
        if local_path.exists():
            logger.info(f"[DL] 既にダウンロード済み: {local_path}")
            return DownloadResult(success=True, path=str(local_path))

        logger.info(f"[DL] ダウンロード中: {url}")
        resp = requests.get(
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

        # ストリーミング書き込み
        total = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                total += len(chunk)
                if total > _MAX_SIZE_MB * 1024 * 1024:
                    logger.warning(f"[DL] ファイルサイズ超過（ストリーミング中）")
                    local_path.unlink(missing_ok=True)
                    return DownloadResult(success=False, error_class="size_exceeded")

        logger.info(f"[DL] 保存完了: {local_path} ({total / 1024:.1f}KB)")
        return DownloadResult(success=True, path=str(local_path), status_code=200)

    except requests.exceptions.Timeout:
        logger.error(f"[DL] タイムアウト: {url}")
        return DownloadResult(success=False, error_class="timeout")
    except requests.exceptions.ConnectionError:
        logger.error(f"[DL] 接続エラー: {url}")
        return DownloadResult(success=False, error_class="network_error")
    except Exception as e:
        logger.error(f"[DL] ダウンロード失敗: {url} - {e}")
        return DownloadResult(success=False, error_class="network_error")
