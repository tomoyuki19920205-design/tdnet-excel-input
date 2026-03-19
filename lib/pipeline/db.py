"""lib/pipeline/db.py -- Supabase REST API ヘルパー

read / write で config を分離し、write 系は SUPABASE_SERVICE_ROLE_KEY を必須にする。
anon key での write は RLS violation になるため禁止。
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("pipeline.db")

# ── 初期化済みフラグ ──
_env_loaded = False


def load_env(project_root: str | None = None) -> None:
    """プロジェクトの .env / .env.local を環境変数にロード (未設定のみ)。

    優先順位: .env.local > .env (後勝ちではなく setdefault なので先勝ち)。
    .env.local がある場合はそちらを先に読み、同名変数は .env.local が優先。
    """
    global _env_loaded
    if _env_loaded:
        return

    if project_root is None:
        from pathlib import Path
        project_root = str(Path(__file__).resolve().parent.parent.parent)

    # .env.local を先に読む (優先)
    for filename in (".env.local", ".env"):
        env_path = os.path.join(project_root, filename)
        if not os.path.exists(env_path):
            continue
        loaded = 0
        for line in open(env_path, "r", encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k not in os.environ:
                    os.environ[k] = v
                    loaded += 1
        logger.debug(f"[db] loaded {loaded} vars from {filename}")

    _env_loaded = True

    # 起動ログ (秘匿情報なし)
    sr_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    write_type = "service_role" if sr_key else "missing"
    read_type = "service_role" if sr_key else ("anon" if anon_key else "missing")
    logger.info(
        f"[db] config: write_key_type={write_type} read_key_type={read_type}"
    )


def _make_config(url: str, key: str) -> dict[str, str]:
    """共通 config dict を生成。"""
    return {
        "url": url,
        "key": key,
        "rest_url": f"{url}/rest/v1",
        "headers": {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    }


def get_supabase_config() -> dict[str, str]:
    """後方互換: 既存コードが使っている config。write 可能なら service role を返す。"""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    )
    if not url or not key:
        logger.warning("[db] SUPABASE_URL or key not set")
    return _make_config(url, key)


def get_supabase_read_config() -> dict[str, str]:
    """読み取り用 config。anon key でも OK。"""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    )
    if not url or not key:
        logger.warning("[db] SUPABASE_URL or key not set for read")
    return _make_config(url, key)


def get_supabase_write_config() -> dict[str, str] | None:
    """書き込み用 config。SUPABASE_SERVICE_ROLE_KEY 必須。

    Returns:
        config dict, or None if service role key is missing.
    """
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url:
        logger.warning("[db] SUPABASE_URL not set for write")
        return None
    if not key:
        logger.warning(
            "[db] SUPABASE_SERVICE_ROLE_KEY not set — "
            "write operations will be rejected. "
            "anon key fallback is disabled for write safety."
        )
        return None
    return _make_config(url, key)


def _get_write_config(config: dict | None) -> dict | None:
    """write 系ヘルパー用: 明示 config があればそれを使い、なければ write config を取得。"""
    if config:
        return config
    return get_supabase_write_config()


# ============================================================
# INSERT (return=representation で挿入行を返す)
# ============================================================

def supabase_insert(
    table: str,
    payload: dict | list[dict],
    *,
    config: dict | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Supabase REST API で INSERT。service role key 必須。

    Returns:
        {"status": int, "ok": bool, "rows": list[dict], "error": str | None}
    """
    import requests

    cfg = _get_write_config(config)
    if not cfg:
        logger.warning(f"[db] INSERT {table} skipped: no write config (service role key missing)")
        return {"status": 0, "ok": False, "rows": [], "error": "no_write_config"}

    headers = {**cfg["headers"], "Prefer": "return=representation"}
    data = payload if isinstance(payload, list) else [payload]

    try:
        r = requests.post(
            f"{cfg['rest_url']}/{table}",
            json=data if len(data) > 1 else data[0],
            headers=headers,
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"[db] INSERT {table} request failed: {e}")
        return {"status": 0, "ok": False, "rows": [], "error": str(e)}

    ok = r.status_code in (200, 201)
    rows: list[dict] = []
    if ok:
        try:
            body = r.json()
            rows = body if isinstance(body, list) else [body]
        except Exception:
            rows = []

    if not ok:
        logger.warning(
            f"[db] INSERT {table} failed: status={r.status_code} "
            f"body={r.text[:300]}"
        )

    return {"status": r.status_code, "ok": ok, "rows": rows, "error": r.text[:300] if not ok else None}


# ============================================================
# UPSERT
# ============================================================

def supabase_upsert(
    table: str,
    payload: dict | list[dict],
    *,
    config: dict | None = None,
    on_conflict: str | None = None,
    timeout: int | tuple[int, int] = (10, 60),
    batch_size: int = 100,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Supabase REST API で upsert。service role key 必須。

    バッチ分割 + 502/503/504 リトライ対応。

    Returns:
        {"status": int, "ok": bool, "count": int, "error": str | None}
    """
    import requests
    import time as _time

    cfg = _get_write_config(config)
    if not cfg:
        logger.warning(f"[db] UPSERT {table} skipped: no write config (service role key missing)")
        return {"status": 0, "ok": False, "count": 0, "error": "no_write_config"}

    # headers 構築 (cfg に "headers" がなくてもフォールバック)
    if "headers" in cfg:
        base_headers = cfg["headers"]
    else:
        _key = cfg.get("key", "")
        base_headers = {
            "apikey": _key,
            "Authorization": f"Bearer {_key}",
            "Content-Type": "application/json",
        }
    headers = {**base_headers, "Prefer": "return=minimal"}
    if on_conflict:
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    data = payload if isinstance(payload, list) else [payload]

    # PostgREST upsert: on_conflict は URL query parameter として必須
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict

    # バッチ分割
    batches = [data[i:i + batch_size] for i in range(0, len(data), batch_size)]
    total_batches = len(batches)
    total_written = 0
    last_error: str | None = None
    last_status = 200

    _RETRYABLE_STATUSES = {502, 503, 504}

    for batch_idx, batch in enumerate(batches, 1):
        batch_t0 = _time.monotonic()

        if total_batches > 1:
            logger.info(
                f"[canonical] {table} batch {batch_idx}/{total_batches} "
                f"start rows={len(batch)}"
            )

        success = False
        for attempt in range(1, max_retries + 1):
            try:
                r = requests.post(
                    f"{cfg['rest_url']}/{table}",
                    json=batch if len(batch) > 1 else batch[0],
                    headers=headers,
                    params=params,
                    timeout=timeout,
                )
            except requests.exceptions.Timeout as e:
                logger.warning(
                    f"[db] UPSERT {table} batch {batch_idx}/{total_batches} "
                    f"timeout (attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    _time.sleep(2 ** attempt)
                    continue
                last_error = f"timeout: {e}"
                last_status = 0
                break
            except Exception as e:
                logger.warning(f"[db] UPSERT {table} request failed: {e}")
                last_error = str(e)
                last_status = 0
                break

            last_status = r.status_code

            if r.status_code in (200, 201):
                elapsed = _time.monotonic() - batch_t0
                total_written += len(batch)
                success = True
                if total_batches > 1:
                    logger.info(
                        f"[canonical] {table} batch {batch_idx}/{total_batches} "
                        f"done status={r.status_code} elapsed={elapsed:.1f}s "
                        f"written_so_far={total_written}"
                    )
                break
            elif r.status_code in _RETRYABLE_STATUSES:
                logger.warning(
                    f"[db] UPSERT {table} batch {batch_idx}/{total_batches} "
                    f"status={r.status_code} (attempt {attempt}/{max_retries}) — retrying"
                )
                if attempt < max_retries:
                    _time.sleep(2 ** attempt)
                    continue
                last_error = f"status={r.status_code} after {max_retries} retries: {r.text[:200]}"
                break
            else:
                # 4xx or other non-retryable error
                last_error = r.text[:300]
                logger.warning(
                    f"[db] UPSERT {table} batch {batch_idx}/{total_batches} "
                    f"failed: status={r.status_code} body={r.text[:300]}"
                )
                break

        if not success and last_error:
            # バッチ失敗 — 残りを中断せず続行するか、ここで止めるか
            # conservative: 中断して残りは未処理として返す
            logger.warning(
                f"[db] UPSERT {table} aborting at batch {batch_idx}/{total_batches} "
                f"after error: {last_error}"
            )
            break

    ok = total_written == len(data)
    if not ok and total_written > 0:
        logger.info(
            f"[db] UPSERT {table} partial: {total_written}/{len(data)} rows written"
        )

    return {
        "status": last_status,
        "ok": ok,
        "count": total_written,
        "error": last_error,
    }


# ============================================================
# SELECT (read — anon key OK)
# ============================================================

def supabase_select(
    table: str,
    *,
    params: dict | None = None,
    config: dict | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Supabase REST API で SELECT。anon key でも利用可能。"""
    import requests

    cfg = config or get_supabase_read_config()

    try:
        r = requests.get(
            f"{cfg['rest_url']}/{table}",
            params=params or {},
            headers=cfg["headers"],
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"[db] SELECT {table} request failed: {e}")
        return []

    if r.status_code == 200:
        return r.json()
    logger.warning(f"[db] SELECT {table} failed: {r.status_code} {r.text[:200]}")
    return []


# ============================================================
# UPDATE (write — service role key 必須)
# ============================================================

def supabase_update(
    table: str,
    payload: dict,
    *,
    params: dict,
    config: dict | None = None,
    timeout: int = 30,
) -> bool:
    """Supabase REST API で UPDATE。service role key 必須。"""
    import requests

    cfg = _get_write_config(config)
    if not cfg:
        logger.warning(f"[db] UPDATE {table} skipped: no write config (service role key missing)")
        return False

    try:
        r = requests.patch(
            f"{cfg['rest_url']}/{table}",
            json=payload,
            params=params,
            headers={**cfg["headers"], "Prefer": "return=minimal"},
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"[db] UPDATE {table} request failed: {e}")
        return False

    if r.status_code in (200, 204):
        return True
    logger.warning(f"[db] UPDATE {table} failed: {r.status_code} {r.text[:200]}")
    return False
