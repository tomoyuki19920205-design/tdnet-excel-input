"""src/events/earnings_subprocess_runner.py   DRY RUN 専用 subprocessワーカー runner

Phase 3-1d: メタデータ生成ルール確定版。
source_doc_id / xbrl_doc_id / archive_date / zip_path の安全な導出ルールを実装。

## title の扱いについて (重要設計方针)
worker に渡す title は、必ず親プロセス側から渡された doc.title を使う。
Supabase の source_title / display_title を runner が内部で参照・補完してはならない。

理由:
- 212A: display_title が誤り (第2四半期決算説明資料)、source_title が正しい (第1四半期決算短信)
- 3320: source_title が誤り (別開示のFYタイトル汚染)、display_title が正しい (1Q)
→ どちらか一方を常に正とすることが不可能であることが実証されている。
→ 正しい title は「現在処理中の TDNET 開示 item.title」のみ。

## source_doc_id
1401系 18桁数値。doc_url / source_url / pdf_url から正規表現で抽出。
sha256 は使わない。

## xbrl_doc_id
0812系 18桁数値。優先順位:
  1. zip_path ファイル名に含まれる 0812系 ID
  2. xbrl_url に含まれる 0812系 ID
  3. source_doc_id の 1401 → 0812 変換 (reason: converted_from_source_doc_id)
sha256 は使わない。

## archive_date
優先順位:
  1. zip_path ファイル名の第2要素 (ticker_YYYYMMDD_docid.zip)
  2. disclosed_at / published_at から JST 日付抽出
  3. xbrl_doc_id の中の日付列 (reason: doc_id_date_fallback)
  4. datetime.now() (reason: now_fallback) ← 最後の手段

## zip_path 選択ルール
  1. xbrl_doc_id 完全一致
  2. source_doc_id 末尾 14桁一致
  3. ticker + archive_date 一致
  4. 複数候補なら ambiguous_zip_match
  5. 見つからなければ から file_not_found

## 呼び出し方法 (本線接続後の想定)
runner は以下の構造体リストを受け取る:

    docs = [
        {
            "ticker":        "6387",
            "company_name":  "サムコ",
            "title":         item.title,       # TDNET開示 item.title を直接渡す
            "source_title":  item.title,       # 同上 (互換性フィールド)
            "disclosed_at":  "2026-06-12T06:30:00+00:00",
            "source_url":    "https://...",
            "pdf_url":       "https://...",
            "source_doc_id": "140120260612...",  # 1401系 18桁
            "xbrl_doc_id":   "081220260612...",  # 0812系 18桁
            "archive_date":  "20260612",
            "zip_path":      "/path/to/ticker_date_docid.zip",
            "event_type":    "earnings",
        },
        ...
    ]
    result = run_earnings_subprocess_dry_run(docs, worker_count=4, timeout_sec=30)

## 実行モード
- run_earnings_subprocess_dry_run(): 並列実行版 (本体)
- build_worker_input(doc, zip_path): doc → worker stdin JSON を構築
- run_one_worker(input_json, timeout_sec): subprocess 1件実行
- kill_process_tree(proc): Windows/Unix 互換のプロセスツリー kill
- parse_worker_stdout(stdout): stdout JSON をパース
- summarize_worker_results(results): 集計サマリーを返す

## メタデータ抽出ヘルパー
- extract_source_doc_id_from_url(url): 1401系 18桁抽出
- extract_xbrl_doc_id_from_zip_path(zip_path): 0812系 18桁抽出
- extract_archive_date_from_zip_path(zip_path): archive_date 抽出
- derive_xbrl_doc_id(source_doc_id, xbrl_url, zip_path): 優先順位決定
- derive_archive_date(zip_path, disclosed_at, xbrl_doc_id): 優先順位決定
- find_zip_for_doc(xbrl_dir, ticker, xbrl_doc_id, source_doc_id, archive_date): 安全な ZIP 選択
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ── 環境設定 ────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# worker スクリプトのパス
_WORKER_SCRIPT = _PROJECT_ROOT / "tools" / "parse_tanshin_worker.py"

# Python 実行ファイル (venv を優先)
def _find_python() -> str:
    candidates = [
        _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows venv
        _PROJECT_ROOT / ".venv" / "bin" / "python",           # Unix venv
        Path(sys.executable),                                  # 現在の Python
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable

_PYTHON_EXE = _find_python()

# ── ロガー ──────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── 必須フィールド定義 ───────────────────────────────────────────────────────
_REQUIRED_DOC_FIELDS = [
    "ticker",
    "company_name",
    "title",            # ← 必ず親プロセスから渡すこと。runner が内部で補完しない。
    "source_title",
    "disclosed_at",
    "source_url",
    "pdf_url",
    "source_doc_id",
    "xbrl_doc_id",
    "archive_date",
    "zip_path",
    "event_type",
]


# ── プロセスツリー kill ───────────────────────────────────────────────────────
def kill_process_tree(proc: subprocess.Popen) -> None:
    """Windows/Unix 互換でプロセスツリーを強制終了する。

    Windows では psutil を使って子プロセスも含めて kill する。
    Unix では os.killpg でプロセスグループ全体を kill する。
    """
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        logger.debug("[runner] kill_process_tree(pid=%d): psutil kill 完了", proc.pid)
    except ImportError:
        # psutil なし: Unix系フォールバック
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            logger.warning("[runner] kill_process_tree fallback failed: %s", e)
            try:
                proc.kill()
            except Exception:
                pass


# ============================================================
# メタデータ抽出ヘルパー (Phase 3-1d 追加修正)
# ============================================================

def is_valid_source_doc_id(value: str | None) -> bool:
    """1401系18桁IDか判定する。"""
    if not value:
        return False
    return bool(re.fullmatch(r"^1401\d{14}$", value))

def is_valid_xbrl_doc_id(value: str | None) -> bool:
    """0812系18桁IDか判定する。"""
    if not value:
        return False
    return bool(re.fullmatch(r"^0812\d{14}$", value))

def extract_source_doc_id_from_url(url: str | None) -> str | None:
    """PDF URLから 1401系18桁IDを抽出する。"""
    if not url:
        return None
    m = re.search(r"1401(\d{14})", url)
    if m:
        return "1401" + m.group(1)
    return None

def extract_xbrl_doc_id_from_text(value: str | None) -> str | None:
    """zip_path または xbrl_url から 0812系18桁IDを抽出する。"""
    if not value:
        return None
    m = re.search(r"0812(\d{14})", value)
    if m:
        return "0812" + m.group(1)
    return None

def extract_archive_date_from_zip_path(zip_path: str | None) -> str | None:
    """ZIPファイル名から archive_date (YYYYMMDD) を抽出する。"""
    if not zip_path:
        return None
    basename = Path(zip_path).stem
    parts = basename.split("_")
    # ticker_YYYYMMDD_0812XXXXXXXXXX... 形式
    if len(parts) >= 2:
        candidate = parts[1]
        if re.fullmatch(r"^20\d{6}$", candidate):
            return candidate
    return None

def extract_date_from_disclosed_at(disclosed_at: str | None) -> str | None:
    """disclosed_at / published_at から JST基準の日付 YYYYMMDD を抽出する。"""
    if not disclosed_at:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", disclosed_at)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return None

def derive_source_doc_id(doc: dict) -> tuple[str, str]:
    """source_doc_id を安全に決める。失敗時は ValueError を送出する。"""
    val = str(doc.get("source_doc_id") or "").strip()
    if is_valid_source_doc_id(val):
        return val, "doc_field"

    for key in ["pdf_url", "source_url", "doc_url"]:
        url = str(doc.get(key) or "")
        ext = extract_source_doc_id_from_url(url)
        if ext:
            return ext, f"extracted_from_{key}"

    raise ValueError("invalid_source_doc_id")

def derive_xbrl_doc_id(doc: dict, source_doc_id: str) -> tuple[str, str]:
    """xbrl_doc_id を安全に決める。失敗時は ValueError を送出する。"""
    val = str(doc.get("xbrl_doc_id") or "").strip()
    if is_valid_xbrl_doc_id(val):
        return val, "doc_field"

    ext_zp = extract_xbrl_doc_id_from_text(str(doc.get("zip_path") or ""))
    if ext_zp:
        return ext_zp, "zip_path"

    ext_xu = extract_xbrl_doc_id_from_text(str(doc.get("xbrl_url") or ""))
    if ext_xu:
        return ext_xu, "xbrl_url"

    if is_valid_source_doc_id(source_doc_id):
        return "0812" + source_doc_id[4:], "converted_from_source_doc_id"

    raise ValueError("invalid_xbrl_doc_id")

def derive_archive_date(doc: dict, zip_path: str, xbrl_doc_id: str) -> tuple[str, str]:
    """archive_date を安全に決める。失敗時は ValueError を送出する。"""
    val = str(doc.get("archive_date") or "").strip()
    if re.fullmatch(r"^20\d{6}$", val):
        return val, "doc_field"

    ext_zp = extract_archive_date_from_zip_path(zip_path)
    if ext_zp:
        return ext_zp, "zip_path"

    for key in ["disclosed_at", "published_at"]:
        d_val = str(doc.get(key) or "")
        ext_d = extract_date_from_disclosed_at(d_val)
        if ext_d:
            return ext_d, "disclosed_at"

    if is_valid_xbrl_doc_id(xbrl_doc_id):
        # 0812 + YYYYMMDD + ...
        if len(xbrl_doc_id) >= 12:
            candidate = xbrl_doc_id[4:12]
            if re.fullmatch(r"^20\d{6}$", candidate):
                return candidate, "xbrl_doc_id_date_fallback"

    raise ValueError("invalid_archive_date")

def find_zip_for_doc(doc: dict, source_doc_id: str, xbrl_doc_id: str, archive_date: str) -> tuple[str, str]:
    """workerに渡す zip_path を安全に決める。失敗時は ValueError を送出する。"""
    doc_zip = str(doc.get("zip_path") or "").strip()
    if doc_zip:
        p = Path(doc_zip)
        if p.is_file():
            if is_valid_xbrl_doc_id(xbrl_doc_id) and xbrl_doc_id in p.name:
                return doc_zip, "explicit_zip_path"
            # If xbrl_doc_id doesn't match, we fall back to searching

    base = _PROJECT_ROOT / "data" / "xbrl_archive"
    if not base.is_dir():
        raise ValueError("file_not_found")

    ticker = str(doc.get("ticker") or "").strip()
    if not ticker:
        raise ValueError("file_not_found")

    # 2. xbrl_doc_id 完全一致
    if is_valid_xbrl_doc_id(xbrl_doc_id):
        cands = sorted(base.glob(f"{ticker}_*_{xbrl_doc_id}.zip"))
        if len(cands) == 1:
            return str(cands[0]), "xbrl_doc_id_exact"
        if len(cands) > 1:
            raise ValueError("ambiguous_zip_match")

    # 3. source_doc_id 末尾 14桁一致 & archive_date 一致 (古いファイルの代用を完全禁止)
    if is_valid_source_doc_id(source_doc_id):
        s14 = source_doc_id[-14:]
        cands = sorted(base.glob(f"{ticker}_*.zip"))
        # archive_date があればそれで絞り込む
        if archive_date:
            cands = [c for c in cands if f"_{archive_date}_" in c.name]
        matched = [c for c in cands if s14 in c.name]
        if len(matched) == 1:
            return str(matched[0]), "source_doc_id_base14"
        if len(matched) > 1:
            raise ValueError("ambiguous_zip_match")



    raise ValueError("file_not_found")

# ── worker 入力 JSON 構築 ──────────────────────────────────────────────────────────────────────────────────
def build_worker_input(doc: dict) -> dict:
    """doc → worker stdin JSON を構築する。失敗時は ValueError を送出する。"""
    # 1. title
    title = str(doc.get("title") or "").strip()
    if not title:
        raise ValueError("invalid_required_field: title is empty")

    # 2. disclosed_at (空でも許可する)
    disclosed_at = str(doc.get("disclosed_at") or doc.get("published_at") or "").strip()

    # 3. source_doc_id
    source_doc_id, sid_reason = derive_source_doc_id(doc)

    # 4. xbrl_doc_id
    xbrl_doc_id, xid_reason = derive_xbrl_doc_id(doc, source_doc_id)

    # 5. zip_path (一次解決)
    doc_zip = str(doc.get("zip_path") or "").strip()

    # 6. archive_date
    archive_date, ad_reason = derive_archive_date(doc, doc_zip, xbrl_doc_id)

    # 7. zip_path (最終決定)
    zip_path, zip_reason = find_zip_for_doc(doc, source_doc_id, xbrl_doc_id, archive_date)

    return {
        "zip_path":      zip_path,
        "ticker":        str(doc.get("ticker") or ""),
        "company_name":  str(doc.get("company_name") or ""),
        "title":         title,
        "source_title":  title,
        "disclosed_at":  disclosed_at,
        "source_url":    str(doc.get("doc_url") or doc.get("source_url") or ""),
        "pdf_url":       str(doc.get("pdf_url") or doc.get("source_url") or doc.get("doc_url") or ""),
        "source_doc_id": source_doc_id,
        "xbrl_doc_id":   xbrl_doc_id,
        "archive_date":  archive_date,
        "event_type":    str(doc.get("event_type") or "earnings"),
        "_meta": {
            "sid_reason": sid_reason,
            "xid_reason": xid_reason,
            "ad_reason":  ad_reason,
            "zip_reason": zip_reason,
        },
    }



# ── stdout パース ────────────────────────────────────────────────────────────
def parse_worker_stdout(stdout: str) -> dict:
    """worker の stdout を JSON としてパースする。

    失敗時は status="json_parse_error" のダミー dict を返す。
    """
    stdout = stdout.strip()
    if not stdout:
        return {
            "status": "error",
            "error_type": "json_parse_error",
            "error_message": "worker stdout is empty",
            "quarter": "",
            "fiscal_year": "",
            "formatted_message_length": 0,
            "notification_compare_json": {"compare": {}, "current": {}},
        }
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error_type": "json_parse_error",
            "error_message": f"stdout JSON parse failed: {e} | raw={stdout[:120]}",
            "quarter": "",
            "fiscal_year": "",
            "formatted_message_length": 0,
            "notification_compare_json": {"compare": {}, "current": {}},
        }


# ── 1件 worker 実行 ───────────────────────────────────────────────────────────
def run_one_worker(
    input_json: dict,
    timeout_sec: float = 30.0,
) -> tuple[dict, float]:
    """worker を 1件実行し (result_dict, elapsed_ms) を返す。

    返り値の result_dict には常に以下が含まれる:
      - status: "ok" / "error"
      - error_type: "" / "timeout" / "json_parse_error" / "missing_required_field" / ...
      - elapsed_ms: float

    timeout 時は kill_process_tree を呼んで孤立プロセスを防ぐ。
    """
    ticker = input_json.get("ticker", "?")
    t_start = time.perf_counter()

    # worker が存在しない場合の早期エラー
    if not _WORKER_SCRIPT.exists():
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        logger.error("[runner] worker script not found: %s", _WORKER_SCRIPT)
        return {
            "status": "error",
            "error_type": "worker_not_found",
            "error_message": f"worker not found: {_WORKER_SCRIPT}",
            "ticker": ticker,
            "quarter": "", "fiscal_year": "",
            "formatted_message_length": 0,
            "notification_compare_json": {"compare": {}, "current": {}},
            "elapsed_ms": elapsed_ms,
        }, elapsed_ms

    stdin_bytes = json.dumps(input_json, ensure_ascii=False).encode("utf-8")
    proc: subprocess.Popen | None = None

    try:
        proc = subprocess.Popen(
            [_PYTHON_EXE, str(_WORKER_SCRIPT), "--input-json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = proc.communicate(
            input=stdin_bytes,
            timeout=timeout_sec,
        )
        elapsed_ms = (time.perf_counter() - t_start) * 1000

        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        if stderr_str.strip():
            logger.debug("[runner][%s] worker stderr:\n%s", ticker, stderr_str[:500])

        result = parse_worker_stdout(stdout_str)
        result["elapsed_ms"] = elapsed_ms
        result.setdefault("ticker", ticker)

        status = result.get("status", "")
        q = result.get("quarter", "")
        logger.info(
            "[runner] %-6s status=%-8s quarter=%-4s fm=%-4d elapsed=%.0fms",
            ticker, status, q,
            result.get("formatted_message_length", 0),
            elapsed_ms,
        )
        return result, elapsed_ms

    except subprocess.TimeoutExpired:
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        logger.warning("[runner] TIMEOUT ticker=%s (%.0f ms)", ticker, elapsed_ms)
        if proc is not None:
            kill_process_tree(proc)
        return {
            "status": "error",
            "error_type": "timeout",
            "error_message": f"worker timeout after {timeout_sec}s",
            "ticker": ticker,
            "quarter": "", "fiscal_year": "",
            "formatted_message_length": 0,
            "notification_compare_json": {"compare": {}, "current": {}},
            "elapsed_ms": elapsed_ms,
        }, elapsed_ms

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        logger.error("[runner] CRASH ticker=%s: %s", ticker, e)
        if proc is not None:
            try:
                kill_process_tree(proc)
            except Exception:
                pass
        return {
            "status": "error",
            "error_type": "worker_crash",
            "error_message": str(e),
            "ticker": ticker,
            "quarter": "", "fiscal_year": "",
            "formatted_message_length": 0,
            "notification_compare_json": {"compare": {}, "current": {}},
            "elapsed_ms": elapsed_ms,
        }, elapsed_ms


# ── 結果サマリー ─────────────────────────────────────────────────────────────
def summarize_worker_results(
    results: list[dict],
    elapsed_sec: float,
) -> dict[str, Any]:
    """results リストを集計して summary dict を返す。"""
    total   = len(results)
    success = sum(1 for r in results if r.get("status") == "ok")
    errors  = sum(1 for r in results if r.get("status") != "ok")

    def count_error_type(t: str) -> int:
        return sum(1 for r in results if r.get("error_type") == t)

    ncj_compare = sum(
        1 for r in results
        if isinstance((r.get("notification_compare_json") or {}).get("compare"), dict)
        and (r.get("notification_compare_json") or {}).get("compare")
    )
    ncj_current = sum(
        1 for r in results
        if isinstance((r.get("notification_compare_json") or {}).get("current"), dict)
        and (r.get("notification_compare_json") or {}).get("current")
    )

    timeout_tickers = [r.get("ticker", "") for r in results if r.get("error_type") == "timeout"]
    error_tickers   = [r.get("ticker", "") for r in results if r.get("status") != "ok"]

    per_item_ms = (elapsed_sec * 1000 / total) if total > 0 else 0.0

    return {
        "total_count":                  total,
        "success_count":                success,
        "error_count":                  errors,
        "timeout_count":                count_error_type("timeout"),
        "json_parse_error_count":       count_error_type("json_parse_error"),
        "invalid_required_field_count": count_error_type("invalid_required_field"),
        "missing_required_field_count": count_error_type("missing_required_field"),
        "file_not_found_count":         count_error_type("file_not_found"),
        "worker_crash_count":           count_error_type("worker_crash"),
        "ncj_compare_exists":           ncj_compare,
        "ncj_current_exists":           ncj_current,
        "elapsed_sec":                  round(elapsed_sec, 3),
        "per_item_ms":                  round(per_item_ms, 1),
        "timeout_tickers":              timeout_tickers,
        "error_tickers":                error_tickers,
        "results":                      results,
    }


# ── メイン: 並列 DRY RUN ─────────────────────────────────────────────────────
def run_earnings_subprocess_dry_run(
    docs: list[dict],
    worker_count: int = 4,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """docs を worker_count 並列で処理し、集計サマリーを返す。

    ## title の保証
    本関数は内部で SQLite / Supabase から title を補完・推定しない。
    docs の各要素に "title" キーが必ず存在することを前提とする。
    title が空文字の場合は build_worker_input → worker が invalid_required_field を返す。

    ## 副作用ゼロ
    DB への INSERT / UPDATE、Supabase 書き込み、Discord 通知は行わない。
    worker のみが解析を担当し、runner は結果を集計して返すだけ。

    Parameters
    ----------
    docs : list[dict]
        解析対象のドキュメントリスト。各要素に "title" (item.title 相当) を含むこと。
    worker_count : int
        並列実行するワーカー数 (デフォルト=4)。
    timeout_sec : float
        1ワーカーあたりのタイムアウト秒数 (デフォルト=30)。

    Returns
    -------
    dict
        summarize_worker_results() の返り値。
        results[] に各ドキュメントの解析結果を含む。
    """
    if not docs:
        logger.warning("[runner] docs が空です。0件を返します。")
        return summarize_worker_results([], 0.0)

    logger.info(
        "[runner] DRY RUN 開始: %d件 workers=%d timeout=%.0fs",
        len(docs), worker_count, timeout_sec,
    )

    # doc の必須フィールド事前検証 (title 空文字 / missing を早期検出)
    results: list[dict] = []
    runnable: list[tuple[int, dict, dict]] = []  # (original_index, doc, input_json)

    for i, doc in enumerate(docs):
        ticker = str(doc.get("ticker", f"[{i}]"))
        
        try:
            input_json = build_worker_input(doc)
            runnable.append((i, doc, input_json))
        except ValueError as e:
            msg = str(e)
            if msg.startswith("invalid_required_field"):
                err_type = "invalid_required_field"
            else:
                err_type = msg
                
            results.append({
                "ticker":                   ticker,
                "status":                   "error",
                "error_type":               err_type,
                "error_message":            msg,
                "worker_started":           False,
                "quarter":                  "",
                "fiscal_year":              "",
                "source_url":               doc.get("source_url", ""),
                "source_doc_id":            doc.get("source_doc_id", ""),
                "formatted_message_length": 0,
                "ncj_compare_exists":       False,
                "ncj_current_exists":       False,
                "elapsed_ms":               0.0,
                "_meta":                    {},
            })
            logger.warning("[runner] %s: validation failed: %s", ticker, msg)
            continue

    logger.info(
        "[runner] preflight 通過: %d件 / スキップ: %d件",
        len(runnable), len(results),
    )

    # 並列実行
    t_run_start = time.perf_counter()

    def _run_task(item: tuple[int, dict, dict]) -> dict:
        idx, doc, input_json = item
        ticker = doc.get("ticker", "?")
        result, _ = run_one_worker(input_json, timeout_sec=timeout_sec)
        result.setdefault("ticker", ticker)
        result.setdefault("source_url", doc.get("source_url", ""))
        result.setdefault("source_doc_id", doc.get("source_doc_id", ""))
        # ncj_compare/current_exists を正規化
        ncj = result.get("notification_compare_json") or {}
        result["ncj_compare_exists"] = bool(isinstance(ncj.get("compare"), dict) and ncj.get("compare"))
        result["ncj_current_exists"] = bool(isinstance(ncj.get("current"), dict) and ncj.get("current"))
        return result

    actual_workers = min(worker_count, len(runnable)) if runnable else 1
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {executor.submit(_run_task, item): item for item in runnable}
        for future in as_completed(futures):
            try:
                r = future.result()
            except Exception as e:
                item = futures[future]
                _, doc, _ = item
                ticker = doc.get("ticker", "?")
                logger.error("[runner] future.result() 例外 ticker=%s: %s", ticker, e)
                r = {
                    "ticker":                   ticker,
                    "status":                   "error",
                    "error_type":               "worker_crash",
                    "error_message":            str(e),
                    "quarter":                  "",
                    "fiscal_year":              "",
                    "source_url":               doc.get("source_url", ""),
                    "source_doc_id":            doc.get("source_doc_id", ""),
                    "formatted_message_length": 0,
                    "ncj_compare_exists":       False,
                    "ncj_current_exists":       False,
                    "elapsed_ms":               0.0,
                }
            results.append(r)

    elapsed_sec = time.perf_counter() - t_run_start
    summary = summarize_worker_results(results, elapsed_sec)
    logger.info(
        "[runner] 完了: total=%d success=%d error=%d timeout=%d elapsed=%.2fs per_item=%.0fms",
        summary["total_count"], summary["success_count"], summary["error_count"],
        summary["timeout_count"], summary["elapsed_sec"], summary["per_item_ms"],
    )
    return summary


# ── Phase 3-2d: save-ready payload 生成 helper ───────────────────────────────
# 副作用ゼロ。保存・通知・Supabase・SQLite は一切呼ばない。
# ─────────────────────────────────────────────────────────────────────────────

_YYYYMMDD_RE = re.compile(r"^\d{8}$")


def _derive_archive_date_yyyymmdd(worker_result: dict, doc: dict) -> tuple[str, str]:
    """archive_date を YYYYMMDD 形式で返す (理由も返す)。

    優先順:
    1. disclosed_at / published_at から YYYYMMDD を取れるなら使う
    2. worker_result["archive_date"] / extracted_payload が YYYYMMDD なら使う
    3. zip_path / archive filename に YYYYMMDD があれば使う
    4. pdf_url / source_url に YYYYMMDD があれば使う
    5. source_doc_id や pdf_url に 20\d{6} が含まれる場合のみ、その8桁を使う
    6. 取得不能なら ("", "not_found")
    """
    # 1. disclosed_at / published_at から日付抽出
    for key in ("disclosed_at", "published_at"):
        dt_str = (doc.get(key) or "").strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", dt_str)
        if m:
            return m.group(1) + m.group(2) + m.group(3), key

    # 2. worker_result / extracted_payload から
    for key in ("archive_date",):
        wr_ad = (worker_result.get(key) or "").strip()
        if wr_ad and _YYYYMMDD_RE.match(wr_ad):
            return wr_ad, "worker_result"
    ep = worker_result.get("extracted_payload")
    if ep and isinstance(ep, dict):
        ep_ad = (ep.get("archive_date") or "").strip()
        if ep_ad and _YYYYMMDD_RE.match(ep_ad):
            return ep_ad, "extracted_payload"

    # 3. zip_path ファイル名から
    zip_path = (doc.get("zip_path") or "").strip()
    if zip_path:
        from pathlib import Path
        stem = Path(zip_path).stem
        m = re.search(r"(20\d{6})", stem)
        if m:
            return m.group(1), "zip_filename"

    # 4 & 5. pdf_url / source_url / source_doc_id に 20\d{6} があるか
    for key in ("pdf_url", "source_url", "source_doc_id"):
        val = (doc.get(key) or "").strip()
        m = re.search(r"(20\d{6})", val)
        if m:
            return m.group(1), key

    return "", "not_found"


def _build_extracted(worker_result: dict, doc: dict) -> dict:
    """worker_result から extracted dict を構築する。副作用ゼロ。

    worker_result["extracted_payload"] を優先使用。
    なければ top-level フィールドから再構築。
    最低限 ticker / quarter / fiscal_year / source_doc_id / xbrl_doc_id を保証する。
    """
    ep = worker_result.get("extracted_payload")
    if ep and isinstance(ep, dict):
        result = dict(ep)
        # xbrl_doc_id / company_name / title が欠けていれば doc から補完
        if not result.get("xbrl_doc_id"):
            result["xbrl_doc_id"] = doc.get("xbrl_doc_id", "")
        if not result.get("company_name"):
            result["company_name"] = worker_result.get("company_name") or doc.get("company_name", "")
        if not result.get("title"):
            result["title"] = doc.get("title", "")
        return result

    # fallback: top-level から再構築
    return {
        "ticker":               worker_result.get("ticker", doc.get("ticker", "")),
        "company_name":         doc.get("company_name", ""),
        "title":                doc.get("title", ""),
        "source_doc_id":        worker_result.get("source_doc_id", doc.get("source_doc_id", "")),
        "xbrl_doc_id":          doc.get("xbrl_doc_id", ""),
        "fiscal_year":          worker_result.get("fiscal_year", ""),
        "quarter":              worker_result.get("quarter", ""),
        "sales_current":        worker_result.get("sales_current"),
        "sales_yoy":            worker_result.get("sales_yoy"),
        "op_current":           worker_result.get("op_current"),
        "op_yoy":               worker_result.get("op_yoy"),
        "primary_metric_name":  worker_result.get("primary_metric_name", ""),
        "primary_metric_value": worker_result.get("primary_metric_value"),
        "primary_metric_yoy":   worker_result.get("primary_metric_yoy"),
        "has_yoy":              worker_result.get("has_yoy", False),
    }


def build_discord_message_preview(payload: dict) -> str:
    """save-ready payload から Discord 通知文字列を生成する。副作用ゼロ。送信しない。"""
    ticker       = payload.get("ticker", "")
    company_name = payload.get("company_name", "")
    quarter      = payload.get("quarter", "")
    fiscal_year  = payload.get("fiscal_year", "")
    fmt_msg      = payload.get("formatted_message", "")

    extracted = (payload.get("raw_payload") or {}).get("extracted") or {}
    sales_yoy = extracted.get("sales_yoy")
    op_yoy    = extracted.get("op_yoy")

    yoy_parts = []
    if sales_yoy is not None:
        yoy_parts.append(f"売上{sales_yoy:+.1%}")
    if op_yoy is not None:
        yoy_parts.append(f"営業利益{op_yoy:+.1%}")
    yoy_str = " / ".join(yoy_parts) if yoy_parts else "前年比データなし"

    return (
        f"【決算短信】{company_name} ({ticker}) {quarter} {fiscal_year}期\n"
        f"{yoy_str}\n"
        f"{fmt_msg[:200]}"
    ).strip()


def validate_save_ready_payload(payload: dict) -> tuple[bool, str]:
    """save-ready payload のバリデーション。副作用ゼロ。

    Returns:
        (True, "") if valid
        (False, skip_reason) if invalid
    """
    # archive_date YYYYMMDD チェック
    ad = payload.get("archive_date", "")
    if not (ad and _YYYYMMDD_RE.match(ad)):
        return False, f"invalid_archive_date: {ad!r}"

    # source_doc_id 1401系 18桁
    sid = payload.get("source_doc_id", "")
    if not (sid.startswith("1401") and len(sid) == 18):
        return False, f"invalid_source_doc_id: {sid!r}"

    # xbrl_doc_id 0812系 18桁
    xid = payload.get("xbrl_doc_id", "")
    if not (xid.startswith("0812") and len(xid) == 18):
        return False, f"invalid_xbrl_doc_id: {xid!r}"

    # suffix14 一致チェック
    if sid[4:] != xid[4:]:
        return False, f"suffix14_mismatch: sid={sid} xid={xid}"

    # quarter 空
    if not payload.get("quarter"):
        return False, "quarter_empty"

    # notification_compare_json の current.label
    ncj = payload.get("notification_compare_json") or {}
    current_label = (ncj.get("current") or {}).get("label", "")
    if not current_label:
        return False, "current_label_empty"

    # raw_payload.extracted が空
    raw_extracted = (payload.get("raw_payload") or {}).get("extracted") or {}
    if not raw_extracted:
        return False, "empty_extracted"

    return True, ""


def build_save_ready_payload(worker_result: dict, doc: dict) -> dict:
    """worker result + doc → save-ready payload。副作用ゼロ。保存・通知はしない。

    Parameters
    ----------
    worker_result : dict
        run_earnings_subprocess_dry_run() が返す results[] の 1要素。
        status=="ok" のものを渡すこと。
    doc : dict
        runner に渡した doc dict（title は必ず doc.title 相当を使うこと）。

    Returns
    -------
    dict
        保存前の payload。validate_save_ready_payload() でバリデーション済み前提。

    title の扱い (重要):
        payload["title"] / payload["source_title"] には必ず doc["title"] を使う。
        Supabase の source_title / display_title は使わない。
        SQLite の title も使わない。
        文字化け時の固定 fallback も使わない。
    """
    quarter      = worker_result.get("quarter", "")
    fiscal_year  = worker_result.get("fiscal_year", "")
    fmt_msg      = worker_result.get("formatted_message", "")

    ncj          = worker_result.get("notification_compare_json") or {}
    current_block = ncj.get("current") or {}
    compare_block = ncj.get("compare") or {}

    # archive_date を YYYYMMDD で取得
    archive_date, archive_date_reason = _derive_archive_date_yyyymmdd(worker_result, doc)

    # extracted を正しく充填
    extracted = _build_extracted(worker_result, doc)

    # source_url / pdf_url は worker_result 優先（doc の doc_url も fallback）
    source_url = worker_result.get("source_url") or doc.get("doc_url", "") or doc.get("source_url", "")
    pdf_url    = worker_result.get("pdf_url")    or doc.get("doc_url", "") or doc.get("pdf_url", "")

    raw_payload = {
        "extracted": extracted,
        "notification_compare_json": {
            "current": current_block,
            "compare": compare_block,
        },
        "source_doc_id": doc.get("source_doc_id", ""),
        "xbrl_doc_id":   doc.get("xbrl_doc_id", ""),
        "worker_meta": {
            "elapsed_ms":           worker_result.get("elapsed_ms", 0),
            "quarter":              quarter,
            "fiscal_year":          fiscal_year,
            "archive_date":         archive_date,
            "archive_date_reason":  archive_date_reason,
            "worker_version":       "subprocess_v1",
        },
        "subprocess_worker": True,
    }

    payload: dict[str, Any] = {
        # EventRecord 必須フィールド相当
        "ticker":       doc["ticker"],
        "company_name": extracted.get("company_name", "") or doc.get("company_name", ""),
        # title は必ず doc.title を使う（Supabase汚染禁止）
        "title":        doc["title"],
        "source_title": doc["title"],
        "disclosed_at": doc.get("disclosed_at", ""),
        "source_url":   source_url,
        "pdf_url":      pdf_url,
        "source_doc_id": worker_result.get("source_doc_id") or doc.get("source_doc_id", ""),
        "xbrl_doc_id":   worker_result.get("xbrl_doc_id") or doc.get("xbrl_doc_id", ""),
        "archive_date":  archive_date,
        "archive_date_reason": archive_date_reason,
        "quarter":       quarter,
        "fiscal_year":   fiscal_year,
        "formatted_message": fmt_msg,
        "primary_metric":    worker_result.get("primary_metric_name", ""),
        "raw_payload":       raw_payload,
        "notification_compare_json": {
            "current": current_block,
            "compare": compare_block,
        },
        "event_type": "earnings",
    }

    # discord_message_preview（送信しない）
    payload["discord_message_preview"] = build_discord_message_preview(payload)

    return payload


# ── Phase 3-2e: mock call plan 生成 helper ──────────────────────────────────
# 副作用ゼロ。保存・通知関数は一切呼ばない。
# save_earnings_summary / save_event_to_supabase / send_earnings_discord は呼ばない。
# ─────────────────────────────────────────────────────────────────────────────

# earnings_summaries INSERT に必要なカラム (earnings_summary_storage._INSERT_COLS 相当)
_EARNINGS_SUMMARY_COLS = [
    "ticker", "company_name", "fiscal_year", "quarter", "title",
    "disclosure_date", "sales_value", "sales_yoy", "op_value", "op_yoy",
    "segment_summary_json", "overall_reason_summary", "segment_reason_summary",
    "summary_short", "summary_full",
    "fingerprint", "source_url", "archive_path",
    "notified_at", "created_at",
    "guidance_sales", "guidance_op", "guidance_eps",
    "guidance_sales_yoy", "guidance_op_yoy", "guidance_eps_yoy",
    "outlook_summary",
]

# save_earnings_summary(conn, data) の data に必須なフィールド
_EARNINGS_SUMMARY_REQUIRED = [
    "ticker", "fiscal_year", "quarter", "title",
    "disclosure_date", "fingerprint",
]

# save_event_to_supabase(event: EventRecord) の EventRecord 必須フィールド
_EVENT_RECORD_REQUIRED = [
    "source_doc_id", "ticker", "company_name", "disclosure_datetime",
    "title", "event_type", "subtype",
    "summary_text", "raw_payload_json", "extracted_payload_json", "fingerprint",
]


def normalize_title(title: str) -> str:
    """タイトルを正規化して意味的重複判定に使用する。

    NFKC 正規化 + 連続空白圧縮 + 前後空白除去。
    全角・半角スペース差、全角英数字差を吸収する。
    """
    import re as _re, unicodedata as _ud
    t = _ud.normalize("NFKC", title or "")
    t = _re.sub(r"\s+", " ", t).strip()
    return t


def find_semantic_duplicate(
    conn,
    ticker: str,
    fiscal_year: str,
    quarter: str,
    disclosure_date: str,
    title: str,
) -> dict | None:
    """earnings_summaries から意味的重複レコードを検索する（読み取り専用）。

    重複判定キー:
        ticker + fiscal_year + quarter + disclosure_date (YYYY-MM-DD) + normalized_title

    fingerprint の一致は必要条件としない。

    Returns:
        dict: 重複が見つかった場合はそのレコード情報
        None: 重複なし
    """
    # disclosure_date を YYYY-MM-DD に正規化 (YYYYMMDD / YYYY-MM-DD 両対応)
    import re as _re2
    _disc = str(disclosure_date or "")
    if _re2.match(r"^\d{8}$", _disc):
        _disc = f"{_disc[:4]}-{_disc[4:6]}-{_disc[6:8]}"
    else:
        _disc = _disc[:10]  # YYYY-MM-DD の先頭10文字だけ使う

    import sqlite3 as _sqlite3
    _orig_row_factory = conn.row_factory
    try:
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ticker, fiscal_year, quarter, title, disclosure_date,
                   fingerprint, created_at, source_url
            FROM earnings_summaries
            WHERE ticker = ?
              AND fiscal_year = ?
              AND quarter = ?
              AND disclosure_date = ?
            ORDER BY created_at DESC
            """,
            (ticker, str(fiscal_year), str(quarter), _disc),
        ).fetchall()
    finally:
        conn.row_factory = _orig_row_factory

    _target_title = normalize_title(title)
    for row in rows:
        if normalize_title(row["title"]) == _target_title:
            return dict(row)
    return None


def build_save_call_plan(payload: dict) -> dict:
    """payload → save_earnings_summary / save_event_to_supabase に渡す予定の引数を組み立てる。

    副作用ゼロ。実際の保存は行わない。
    引数の候補 dict を返すだけ。
    """
    extracted = (payload.get("raw_payload") or {}).get("extracted") or {}
    raw_p     = payload.get("raw_payload") or {}
    ncj       = payload.get("notification_compare_json") or {}
    current   = ncj.get("current") or {}
    guidance  = extracted.get("guidance") or {}

    # ── save_earnings_summary(conn, data) の data 相当 ──────────────────────
    # fingerprint を旧方式互換に統一 (Phase 3-2j-fix1)
    # 旧方式: SHA256("earnings_v2:{ticker}:{title}:{doc_id}")[:32]
    # doc_id 優先順位: source_doc_id > source_url > xbrl_doc_id > ""
    import hashlib
    _fp_doc_id = (
        payload.get("source_doc_id", "") or
        payload.get("source_url", "") or
        payload.get("xbrl_doc_id", "") or
        ""
    )
    _fp_raw = f"earnings_v2:{payload.get('ticker', '')}:{payload.get('title', '')}:{_fp_doc_id}"
    fingerprint = hashlib.sha256(_fp_raw.encode()).hexdigest()[:32]

    earnings_summary_args: dict = {
        "ticker":            payload.get("ticker", ""),
        "company_name":      payload.get("company_name", ""),
        "fiscal_year":       payload.get("fiscal_year", ""),
        "quarter":           payload.get("quarter", ""),
        "title":             payload.get("title", ""),          # doc.title のみ
        "disclosure_date":   payload.get("archive_date", ""),   # YYYYMMDD
        "sales_value":       extracted.get("sales_current"),
        "sales_yoy":         extracted.get("sales_yoy"),
        "op_value":          extracted.get("op_current"),
        "op_yoy":            extracted.get("op_yoy"),
        "segment_summary_json": None,
        "overall_reason_summary": None,
        "segment_reason_summary": None,
        "summary_short":     (payload.get("formatted_message") or "")[:200],
        "summary_full":      payload.get("formatted_message", ""),
        "fingerprint":       fingerprint,
        "source_url":        payload.get("source_url", ""),
        "archive_path":      payload.get("xbrl_doc_id", ""),    # xbrl_doc_id を archive 識別子として
        "notified_at":       None,
        "created_at":        None,  # 保存時に自動設定
        "guidance_sales":    guidance.get("sales_forecast"),
        "guidance_op":       guidance.get("op_forecast"),
        "guidance_eps":      guidance.get("eps_forecast"),
        "guidance_sales_yoy": guidance.get("sales_yoy"),
        "guidance_op_yoy":   guidance.get("op_yoy"),
        "guidance_eps_yoy":  guidance.get("eps_yoy"),
        "outlook_summary":   None,
    }
    missing_summary = [f for f in _EARNINGS_SUMMARY_REQUIRED if not earnings_summary_args.get(f)]

    # ── save_event_to_supabase(event: EventRecord) の EventRecord フィールド相当 ──
    import json as _json
    ad = payload.get("archive_date", "")
    fallback_dt = payload.get("disclosed_at") or (f"{ad[:4]}-{ad[4:6]}-{ad[6:8]} 15:00:00+09:00" if len(ad) >= 8 else "") or ""

    tdnet_event_payload: dict = {
        "source_doc_id":        payload.get("source_doc_id", ""),
        "ticker":               payload.get("ticker", ""),
        "company_name":         payload.get("company_name", ""),
        "disclosure_datetime":  fallback_dt,
        "title":                payload.get("title", ""),        # doc.title のみ
        "doc_url":              payload.get("source_url", ""),
        "event_type":           "earnings",
        "subtype":              payload.get("quarter", ""),      # FY / 1Q / 2Q / 3Q
        "importance":           60,
        "summary_text":         (payload.get("formatted_message") or "")[:500],
        "raw_payload_json":     _json.dumps(raw_p, ensure_ascii=False, default=str),
        "extracted_payload_json": _json.dumps(extracted, ensure_ascii=False, default=str),
        "fingerprint":          fingerprint,
        "status":               "active",
    }
    missing_event = [f for f in _EVENT_RECORD_REQUIRED if not tdnet_event_payload.get(f)]

    return {
        "earnings_summary_args":        earnings_summary_args,
        "earnings_summary_missing":     missing_summary,
        "earnings_summary_ready":       len(missing_summary) == 0,
        "tdnet_event_payload":          tdnet_event_payload,
        "tdnet_event_missing":          missing_event,
        "tdnet_event_ready":            len(missing_event) == 0,
    }


def build_discord_call_plan(payload: dict) -> dict:
    """payload → send_earnings_discord に渡す予定の message を組み立てる。

    副作用ゼロ。実際の送信は行わない。
    send_earnings_discord(webhook_url, message, dry_run=False) の引数候補を返す。
    """
    discord_message = payload.get("discord_message_preview", "")
    if not discord_message:
        discord_message = build_discord_message_preview(payload)

    missing = []
    if not discord_message:
        missing.append("discord_message")

    return {
        "would_call": "send_earnings_discord",
        "args": {
            "webhook_url": "${DISCORD_WEBHOOK_URL}",  # 実際のURLは環境変数から
            "message": discord_message,
            "dry_run": False,  # 実際の呼び出し時のみ False にする
        },
        "discord_message": discord_message,
        "discord_missing": missing,
        "discord_ready": len(missing) == 0 and len(discord_message) > 0,
    }


def validate_save_call_plan(
    call_plan: dict,
    *,
    require_discord: bool = True,
) -> tuple[bool, str]:
    """save_call_plan のバリデーション。副作用ゼロ。

    Args:
        call_plan: build_save_call_plan() の戻り値。
        require_discord: True の場合、discord_ready が必須。
            False の場合、discord_ready は診断値として保持するが
            call_plan_ready の必須条件に含めない。
            既存の呼び出しとの互换性のためデフォルトは True。
    """
    if not call_plan.get("earnings_summary_ready"):
        missing = call_plan.get("earnings_summary_missing", [])
        return False, f"earnings_summary_missing: {missing}"
    if not call_plan.get("tdnet_event_ready"):
        missing = call_plan.get("tdnet_event_missing", [])
        return False, f"tdnet_event_missing: {missing}"
    if require_discord:
        discord = call_plan.get("discord_plan", {})
        if not discord.get("discord_ready"):
            missing = discord.get("discord_missing", [])
            return False, f"discord_missing: {missing}"
    return True, ""


# ── Phase 3-2f: stub / fake 呼び出しルーティング ────────────────────────────
# 副作用ゼロ。本物の保存・通知関数は一切呼ばない。
# save_earnings_summary / save_event_to_supabase / send_earnings_discord は呼ばない。
# ─────────────────────────────────────────────────────────────────────────────


def fake_save_earnings_summary(save_plan: dict, ticker: str = "") -> dict:
    """save_earnings_summary の stub。SQLite には一切書き込まない。

    本物の save_earnings_summary(conn, data) の代わりに呼ぶ。
    conn も data も要求しない。
    引数の検証だけ行い、"stub_inserted" を返す。
    """
    args = save_plan.get("earnings_summary_args", {})
    missing = save_plan.get("earnings_summary_missing", [])
    ready = save_plan.get("earnings_summary_ready", False)

    result = {
        "stub_call": "fake_save_earnings_summary",
        "ticker": args.get("ticker", ticker),
        "fiscal_year": args.get("fiscal_year", ""),
        "quarter": args.get("quarter", ""),
        "title": args.get("title", ""),
        "disclosure_date": args.get("disclosure_date", ""),
        "fingerprint": args.get("fingerprint", ""),
        "sales_value": args.get("sales_value"),
        "op_value": args.get("op_value"),
        "ready": ready,
        "missing": missing,
        "result": "stub_inserted" if ready else f"stub_skipped: missing={missing}",
        # 保証: SQLite INSERT は一切行わない
        "sqlite_written": False,
    }
    logger.debug("[STUB] fake_save_earnings_summary ticker=%s result=%s", result["ticker"], result["result"])
    return result


def fake_save_event_to_supabase(save_plan: dict, ticker: str = "") -> dict:
    """save_event_to_supabase の stub。Supabase には一切書き込まない。

    本物の save_event_to_supabase(event: EventRecord) の代わりに呼ぶ。
    EventRecord も Supabase client も使わない。
    引数の検証だけ行い、{"action": "stub_dry_run"} を返す。
    """
    payload = save_plan.get("tdnet_event_payload", {})
    missing = save_plan.get("tdnet_event_missing", [])
    ready = save_plan.get("tdnet_event_ready", False)

    result = {
        "stub_call": "fake_save_event_to_supabase",
        "ticker": payload.get("ticker", ticker),
        "source_doc_id": payload.get("source_doc_id", ""),
        "event_type": payload.get("event_type", ""),
        "subtype": payload.get("subtype", ""),
        "title": payload.get("title", ""),
        "summary_text_length": len(payload.get("summary_text", "")),
        "raw_payload_json_length": len(payload.get("raw_payload_json", "")),
        "extracted_payload_json_length": len(payload.get("extracted_payload_json", "")),
        "fingerprint": payload.get("fingerprint", ""),
        "ready": ready,
        "missing": missing,
        "action": "stub_dry_run" if ready else f"stub_skipped: missing={missing}",
        # 保証: Supabase insert / upsert は一切行わない
        "supabase_written": False,
    }
    logger.debug("[STUB] fake_save_event_to_supabase ticker=%s action=%s", result["ticker"], result["action"])
    return result


def fake_send_earnings_discord(discord_plan: dict, ticker: str = "") -> dict:
    """send_earnings_discord の stub。webhook には一切送信しない。

    本物の send_earnings_discord(webhook_url, message, dry_run) の代わりに呼ぶ。
    webhook_url も requests.post も使わない。
    message が空でないことだけ検証し、True を返す。
    """
    message = discord_plan.get("discord_message", "")
    ready = discord_plan.get("discord_ready", False)
    missing = discord_plan.get("discord_missing", [])

    result = {
        "stub_call": "fake_send_earnings_discord",
        "ticker": ticker,
        "message_length": len(message),
        "message_preview": message[:80],
        "ready": ready,
        "missing": missing,
        "sent": False,          # 実際には送信していない
        "result": True if ready else False,
        # 保証: requests.post / Discord webhook は一切呼ばない
        "discord_sent": False,
    }
    logger.debug("[STUB] fake_send_earnings_discord ticker=%s message_length=%d", ticker, result["message_length"])
    return result


def run_stub_call_path(
    payload: dict,
    save_plan: dict,
    discord_plan: dict,
    ticker: str = "",
) -> dict:
    """payload + call plan → stub 関数を順番に呼ぶ。副作用ゼロ。

    呼び出し順序:
    1. fake_save_earnings_summary  (SQLite 相当)
    2. fake_save_event_to_supabase (Supabase 相当)
    3. fake_send_earnings_discord  (Discord 相当)

    Returns
    -------
    dict
        stub_earnings_summary / stub_tdnet_event / stub_discord それぞれの結果と、
        all_stub_calls_ok フラグ
    """
    stub_earnings = fake_save_earnings_summary(save_plan, ticker=ticker)
    stub_event    = fake_save_event_to_supabase(save_plan, ticker=ticker)
    stub_discord  = fake_send_earnings_discord(discord_plan, ticker=ticker)

    all_ok = (
        stub_earnings.get("result") == "stub_inserted"
        and stub_event.get("action") == "stub_dry_run"
        and stub_discord.get("result") is True
    )

    return {
        "ticker": ticker,
        "stub_earnings_summary": stub_earnings,
        "stub_tdnet_event": stub_event,
        "stub_discord": stub_discord,
        "all_stub_calls_ok": all_ok,
        # 保証カウンタ
        "real_save_called": False,
        "real_supabase_called": False,
        "real_discord_called": False,
        "sqlite_write_count": 0,
        "supabase_write_count": 0,
        "discord_send_count": 0,
    }

