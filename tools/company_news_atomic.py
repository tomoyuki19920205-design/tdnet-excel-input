"""Durable same-directory atomic writes for Company News runtime files."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32})
DEFAULT_REPLACE_BACKOFF_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8)


def _is_transient_replace_error(error: OSError) -> bool:
    return isinstance(error, PermissionError) and getattr(error, "winerror", None) in WINDOWS_TRANSIENT_REPLACE_ERRORS


def replace_with_retry(
    source: Path,
    target: Path,
    *,
    backoff_seconds: Iterable[float] = DEFAULT_REPLACE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Replace *target*, retrying only transient Windows sharing/access errors.

    Returns the number of retries used. All other failures remain fail-closed.
    """
    retries = 0
    delays = tuple(backoff_seconds)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return retries
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt >= len(delays):
                raise
            sleep(delays[attempt])
            retries += 1
    raise AssertionError("unreachable")


def atomic_write_text(
    path: Path,
    text: str,
    *,
    backoff_seconds: Iterable[float] = DEFAULT_REPLACE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Flush text to a unique sibling temp file and atomically replace *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return replace_with_retry(temporary, path, backoff_seconds=backoff_seconds, sleep=sleep)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any]) -> int:
    return atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_write_jsonl(path: Path, values: list[dict[str, Any]]) -> int:
    body = "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values)
    return atomic_write_text(path, body)
