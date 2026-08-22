"""Strict parsers for scalar values returned by J-Quants APIs."""
from __future__ import annotations

from typing import Any


_TRUE_STRINGS = frozenset({"true", "1"})
_FALSE_STRINGS = frozenset({"false", "0"})


def parse_optional_boolean(value: Any) -> bool | None:
    """Parse a J-Quants boolean without Python's truthy-string semantics.

    Blank and NULL mean that the flag is not supplied/applicable. Unexpected
    nonblank values are rejected so a provider schema change cannot silently
    alter production calculations.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        raise ValueError(f"unsupported numeric boolean: {value!r}")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
    raise ValueError(f"unsupported boolean value: {value!r}")
