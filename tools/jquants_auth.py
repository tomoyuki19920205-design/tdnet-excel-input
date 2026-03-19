#!/usr/bin/env python3
"""
jquants_auth.py — J-Quants API 認証ヘルパー

V2 API Key 方式:
  .env の JQUANTS_API_KEY を x-api-key ヘッダーに設定。
  V2 エンドポイント (/v2/equities/bars/daily 等) で使用。

.env に必要な設定:
  JQUANTS_API_KEY=your_api_key
"""
import os
import logging
from pathlib import Path

logger = logging.getLogger("jquants_auth")


def _load_env():
    """プロジェクトルートの.envから環境変数を読み込む。"""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_api_key() -> str:
    """JQUANTS_API_KEY を取得。"""
    _load_env()
    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "J-Quants API KEYが未設定です。\n"
            "手順:\n"
            "  1. https://jpx-jquants.com/ にログイン\n"
            "  2. マイページからAPI KEYをコピー\n"
            "  3. .env に以下を追加:\n"
            "     JQUANTS_API_KEY=コピーしたキー\n"
        )
    logger.info(f"API KEY loaded ({api_key[:8]}...)")
    return api_key


def get_auth_headers() -> dict:
    """V2 API用の認証ヘッダーを返す。"""
    api_key = get_api_key()
    return {"x-api-key": api_key}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        h = get_auth_headers()
        key = h["x-api-key"]
        print(f"OK! x-api-key={key[:8]}... (len={len(key)})")
    except RuntimeError as e:
        print(f"ERROR: {e}")
