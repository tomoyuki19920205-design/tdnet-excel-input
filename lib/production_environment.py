"""Explicit, cwd-independent bootstrap for Production write credentials."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SUPABASE_WRITE_ENV_NAMES = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProductionEnvironmentError(RuntimeError):
    """The explicitly selected Production root cannot provide safe write config."""


@dataclass(frozen=True)
class ProductionEnvironment:
    production_root: Path
    env_files: tuple[Path, ...]
    required_names: tuple[str, ...]

    def safe_metadata(self) -> dict[str, object]:
        """Return reportable metadata without any credential values."""
        return {
            "production_root": str(self.production_root),
            "env_files": [str(path) for path in self.env_files],
            "required_env_names": list(self.required_names),
        }


def _parse_env_file(path: Path) -> Iterable[tuple[str, str]]:
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ProductionEnvironmentError(
                f"invalid environment assignment in {path} at line {line_number}"
            )
        name, value = (part.strip() for part in line.split("=", 1))
        if not _ENV_NAME_RE.fullmatch(name):
            raise ProductionEnvironmentError(
                f"invalid environment name in {path} at line {line_number}"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        yield name, value


def bootstrap_production_write_environment(
    production_root: Path | str,
    *,
    required_names: tuple[str, ...] = SUPABASE_WRITE_ENV_NAMES,
) -> ProductionEnvironment:
    """Load only explicit Production-root env files and fail closed for writes.

    Existing process values always win, including explicitly empty values. Files
    are restricted to ``<production_root>/.env.local`` then ``.env`` and may not
    be symlinks to another directory.
    """
    requested_root = Path(production_root).expanduser()
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise ProductionEnvironmentError(
            f"Production root does not exist: {requested_root}"
        ) from exc
    if not root.is_dir():
        raise ProductionEnvironmentError(f"Production root is not a directory: {root}")

    env_files: list[Path] = []
    for filename in (".env.local", ".env"):
        candidate = root / filename
        if not candidate.exists():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ProductionEnvironmentError(
                f"Production environment file cannot be resolved: {candidate}"
            ) from exc
        if resolved.parent != root or not resolved.is_file():
            raise ProductionEnvironmentError(
                f"Production environment file is outside the selected root: {candidate}"
            )
        env_files.append(resolved)
        for name, value in _parse_env_file(resolved):
            if name not in os.environ:
                os.environ[name] = value

    missing = [name for name in required_names if not os.environ.get(name)]
    if missing:
        joined = ", ".join(missing)
        raise ProductionEnvironmentError(
            f"Production write environment is incomplete; missing: {joined}"
        )
    return ProductionEnvironment(root, tuple(env_files), tuple(required_names))
