"""Credential and auth-session primitives."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mediagent.core.filesystem import PathSafetyError, resolve_placeholders
from mediagent.core.redaction import redact_secrets


class CredentialSource(StrEnum):
    ENV = "env"
    FILE = "file"


@dataclass(frozen=True)
class CredentialRef:
    source: CredentialSource
    name: str
    key: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CredentialRef":
        return cls(
            source=CredentialSource(value["source"]),
            name=value["name"],
            key=value.get("key"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "name": self.name,
            "key": self.key,
        }


@dataclass(frozen=True)
class AuthSession:
    provider: str
    account_id: str | None
    scopes: list[str]
    expires_at: str | None
    refresh_available: bool
    status: str
    credential_refs: list[CredentialRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "scopes": self.scopes,
            "expires_at": self.expires_at,
            "refresh_available": self.refresh_available,
            "status": self.status,
            "credential_refs": [ref.to_dict() for ref in self.credential_refs],
            "metadata": redact_secrets(self.metadata),
        }


def resolve_credential(
    ref: CredentialRef,
    *,
    env: dict[str, str] | Any,
    cwd: Path,
) -> str | None:
    if ref.source == CredentialSource.ENV:
        return env.get(ref.name)
    if ref.source == CredentialSource.FILE:
        raw_path = resolve_placeholders(ref.name, env)
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if not path.exists():
            return None
        if ref.key:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get(ref.key)
            return str(value) if value is not None else None
        return path.read_text(encoding="utf-8").strip()
    raise PathSafetyError(f"Unsupported credential source: {ref.source}")


def resolve_credential_path(
    value: str,
    *,
    env: dict[str, str] | Any,
    cwd: Path,
) -> Path:
    raw_path = resolve_placeholders(value, env)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def read_credential_json(
    value: str,
    *,
    env: dict[str, str] | Any,
    cwd: Path,
) -> dict[str, Any]:
    path = resolve_credential_path(value, env=env, cwd=cwd)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PathSafetyError("Credential file must contain a JSON object.")
    return data


def write_credential_json(
    value: str,
    data: dict[str, Any],
    *,
    env: dict[str, str] | Any,
    cwd: Path,
) -> Path:
    path = resolve_credential_path(value, env=env, cwd=cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    os.chmod(path, 0o600)
    return path


def resolve_credentials(
    refs: list[CredentialRef],
    *,
    env: dict[str, str] | Any,
    cwd: Path,
) -> dict[str, str | None]:
    return {ref.key or ref.name: resolve_credential(ref, env=env, cwd=cwd) for ref in refs}
