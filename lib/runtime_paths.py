"""Resolve mutable paths without opening files or creating directories.

Code, configuration, templates and migration scripts must not use this resolver.
Explicit absolute paths outside the supplied code root remain caller overrides.
The environment is read on each call, never cached at import time.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

STATE_ROOT_ENV = "TDNET_RUNTIME_STATE_ROOT"
CODE_ROOT = Path(__file__).absolute().parents[1]


def _validate(path: str) -> None:
    if not path or not path.strip() or "\x00" in path:
        raise ValueError("Runtime state path must not be empty or contain NUL")
    windows = PureWindowsPath(path)
    if ".." in windows.parts or ".." in Path(path).parts:
        raise ValueError("Runtime state path must not contain parent traversal")
    if os.name == "nt":
        if path.startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("Windows device paths are not supported")
        parts = list(windows.parts[1:] if windows.anchor else windows.parts)
        if windows.drive.startswith("\\\\"):
            parts = windows.drive.strip("\\").split("\\") + parts
        for part in parts:
            if any(ord(c) < 32 or c in '<>:"|?*' for c in part):
                raise ValueError("Invalid Windows runtime state path")
            if part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved():
                raise ValueError("Invalid Windows runtime state path component")


def runtime_state_root(code_root: str | os.PathLike = CODE_ROOT) -> Path:
    """Return the configured absolute root, or the unchanged legacy root.

    Validation is lexical: even an absent destination is accepted without I/O.
    Deployment must use a trusted directory without escaping junctions/symlinks.
    """
    if STATE_ROOT_ENV not in os.environ:
        return Path(code_root)
    value = os.environ[STATE_ROOT_ENV]
    _validate(value)
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("TDNET_RUNTIME_STATE_ROOT must be an absolute path")
    return root


def runtime_path(legacy_path: str | os.PathLike, *,
                 code_root: str | os.PathLike = CODE_ROOT) -> Path:
    """Rebase a mutable default onto state root; preserve explicit external paths.

    With no environment override, return exactly the legacy path (including
    relative paths). No mkdir, stat, resolve, database open or lock acquisition.
    """
    destination = runtime_state_root(code_root)
    path = Path(legacy_path)
    if STATE_ROOT_ENV not in os.environ:
        return path
    _validate(os.fspath(legacy_path))
    if path.is_absolute():
        if path.is_relative_to(destination):
            return path
        try:
            relative = path.relative_to(Path(code_root).absolute())
        except ValueError:
            return path
    else:
        if path.anchor or path.drive:
            raise ValueError("Drive-relative runtime paths are not supported")
        relative = path
    return destination / relative


def runtime_default(relative_path: str, legacy_path: str | os.PathLike) -> Path:
    """Rebase a historical default whose old location was outside code root.

    Only use for a default, never an explicitly supplied caller override.
    """
    root = runtime_state_root()
    if STATE_ROOT_ENV not in os.environ:
        return Path(legacy_path)
    _validate(relative_path)
    relative = Path(relative_path)
    if relative.anchor or relative.drive:
        raise ValueError("Runtime default must be relative to state root")
    return root / relative
