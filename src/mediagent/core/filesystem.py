"""Filesystem helpers with explicit write-boundary checks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PathSafetyError(ValueError):
    pass


def resolve_placeholders(value: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise PathSafetyError(f"Missing environment variable: {name}")
        return env[name]

    return PLACEHOLDER_PATTERN.sub(replace, value)


def normalize_path(
    value: str,
    *,
    env: Mapping[str, str],
    base_dir: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    expanded = resolve_placeholders(value, env)
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        root = base_dir or cwd or Path.cwd()
        path = root / path
    return path.resolve()


def ensure_inside(path: Path, roots: list[Path]) -> None:
    if not roots:
        raise PathSafetyError("No allowed write roots are configured.")
    resolved_path = path.resolve()
    resolved_roots = [root.resolve() for root in roots]
    if not any(_is_relative_to(resolved_path, root) for root in resolved_roots):
        roots_text = ", ".join(str(root) for root in resolved_roots)
        raise PathSafetyError(f"Path is outside allowed roots: {resolved_path} not in {roots_text}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath([str(path), str(root)])
    except ValueError:
        return False
    return common == str(root)
