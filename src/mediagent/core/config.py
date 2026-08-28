"""Small, non-executing configuration loader for human-facing commands."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import MutableMapping
from pathlib import Path

from mediagent.core.filesystem import PathSafetyError, resolve_placeholders


ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EnvFileError(ValueError):
    pass


def load_env_file(
    path: Path,
    *,
    env: MutableMapping[str, str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    """Load simple KEY=VALUE records without executing shell code."""

    target = os.environ if env is None else env
    if not path.is_file():
        return {}

    loaded: dict[str, str] = {}
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not ENV_KEY.fullmatch(key):
            raise EnvFileError(f"Invalid .env assignment on line {line_number}.")
        try:
            value = _parse_value(raw_value)
            value = resolve_placeholders(value, {**target, **loaded})
        except (ValueError, PathSafetyError) as exc:
            raise EnvFileError(f"Invalid .env value on line {line_number}: {exc}") from exc
        if override or key not in target:
            target[key] = value
        loaded[key] = target[key]
    return loaded


def _parse_value(raw_value: str) -> str:
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    values = list(lexer)
    if not values:
        return ""
    return " ".join(values)
