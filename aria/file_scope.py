from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from aria.errors import ConfigurationError


_GLOB_MARKERS = frozenset("*?[")


def normalize_scope_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError("file scope path is invalid")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ConfigurationError("file scope path must be repository-relative POSIX syntax")
    normalized = value.rstrip("/")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConfigurationError("file scope path escapes or ambiguously names the repository")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ConfigurationError("file scope path contains control characters")
    return normalized


def normalize_scope(paths: object) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise ConfigurationError("task file scope must be a non-empty list")
    normalized = sorted({normalize_scope_path(path) for path in paths})
    if len(normalized) != len(paths):
        raise ConfigurationError("task file scope must be unique and sorted")
    return normalized


def _static_prefix(pattern: str) -> tuple[str, ...]:
    prefix: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if any(marker in part for marker in _GLOB_MARKERS):
            break
        prefix.append(part)
    return tuple(prefix)


def _glob_match(path: str, pattern: str) -> bool:
    """Match repository paths without allowing ``*`` to cross ``/``.

    ``fnmatch`` treats a slash like any other character.  That is unsafe for a
    repository lease because ``src/*`` would otherwise also authorize
    ``src/private/secret.py``.  ARIA uses the conventional path semantics:
    ``*`` and ``?`` stay inside one segment while a complete ``**`` segment is
    recursive.
    """
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")

    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def scopes_overlap(left: str, right: str) -> bool:
    left = normalize_scope_path(left)
    right = normalize_scope_path(right)
    left_glob = any(marker in left for marker in _GLOB_MARKERS)
    right_glob = any(marker in right for marker in _GLOB_MARKERS)
    if not left_glob and not right_glob:
        return (
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
        )
    if _glob_match(left, right) or _glob_match(right, left):
        return True
    left_prefix = _static_prefix(left)
    right_prefix = _static_prefix(right)
    shared = min(len(left_prefix), len(right_prefix))
    return left_prefix[:shared] == right_prefix[:shared]


def scope_sets_overlap(left: list[str], right: list[str]) -> bool:
    return any(scopes_overlap(a, b) for a in left for b in right)


def path_allowed(path: str, scope_paths: list[str]) -> bool:
    candidate = normalize_scope_path(path)
    for pattern in scope_paths:
        pattern = normalize_scope_path(pattern)
        if any(marker in pattern for marker in _GLOB_MARKERS):
            if _glob_match(candidate, pattern):
                return True
        elif candidate == pattern or candidate.startswith(pattern + "/"):
            return True
    return False
