"""Tool contracts used by CLI, workflows, and future agents."""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mediagent.core.filesystem import resolve_placeholders
from mediagent.core.redaction import redact_secrets, redact_text
from mediagent.core.schema import validate_input


class Permission(StrEnum):
    READ_ENV = "read_env"
    READ_CREDENTIALS = "read_credentials"
    WRITE_CREDENTIALS = "write_credentials"
    READ_DB = "read_db"
    WRITE_DB = "write_db"
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    NETWORK = "network"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    AUTH = "auth"
    PERMISSION = "permission"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    FILESYSTEM = "filesystem"
    DATABASE = "database"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    category: ErrorCategory = ErrorCategory.RUNTIME
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category.value,
            "details": self.details,
        }


@dataclass
class ToolResult:
    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rate_limit: dict[str, Any] | None = None
    error: ToolError | None = None

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        *,
        artifacts: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        rate_limit: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            status=ToolStatus.SUCCESS,
            data=data or {},
            artifacts=artifacts or [],
            warnings=warnings or [],
            rate_limit=rate_limit,
        )

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        category: ErrorCategory | str = ErrorCategory.RUNTIME,
        rate_limit: dict[str, Any] | None = None,
    ) -> "ToolResult":
        if isinstance(category, str):
            category = ErrorCategory(category)
        return cls(
            status=ToolStatus.FAILURE,
            data=redact_secrets(data or {}),
            warnings=warnings or [],
            rate_limit=rate_limit,
            error=ToolError(
                code=code,
                message=redact_text(message),
                category=category,
                details=redact_secrets(details or {}),
            ),
        )

    @property
    def is_success(self) -> bool:
        return self.status == ToolStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "data": self.data,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
            "rate_limit": self.rate_limit,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[Permission, ...]
    dry_run_supported: bool
    experimental: bool = False
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "permissions": [permission.value for permission in self.permissions],
            "dry_run_supported": self.dry_run_supported,
            "experimental": self.experimental,
            "hidden": self.hidden,
        }


@dataclass
class ToolContext:
    cwd: Path
    env: Mapping[str, str]
    dry_run: bool
    run_id: str
    data_dir: Path | None = None
    library_dir: Path | None = None
    db_path: Path | None = None
    log_path: Path | None = None
    http_client: Any | None = None

    @classmethod
    def from_env(
        cls,
        *,
        dry_run: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        http_client: Any | None = None,
    ) -> "ToolContext":
        source_env = os.environ if env is None else env
        current_dir = (cwd or Path.cwd()).resolve()
        return cls(
            cwd=current_dir,
            env=source_env,
            dry_run=dry_run,
            run_id=str(uuid.uuid4()),
            data_dir=_path_from_env(source_env, "MEDIAGENT_DATA_DIR"),
            library_dir=_path_from_env(source_env, "MEDIAGENT_LIBRARY_DIR"),
            db_path=_path_from_env(source_env, "MEDIAGENT_DB_PATH"),
            log_path=_path_from_env(source_env, "MEDIAGENT_LOG_PATH"),
            http_client=http_client,
        )

    def allowed_write_roots(self) -> list[Path]:
        roots: list[Path] = []
        if self.data_dir:
            roots.append(self.data_dir)
        if self.library_dir:
            roots.append(self.library_dir)
        for name, value in self.env.items():
            if name.startswith("MEDIAGENT_") and name.endswith("_LIBRARY_DIR") and name != "MEDIAGENT_LIBRARY_DIR":
                if value:
                    roots.append(Path(resolve_placeholders(value, self.env)))
        if self.db_path:
            roots.append(self.db_path.parent)
        if self.log_path:
            roots.append(self.log_path.parent)
        return [root.expanduser().resolve() for root in roots]


Handler = Callable[[ToolContext, dict[str, Any]], ToolResult | Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    spec: ToolSpec
    handler: Handler

    async def run(self, context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
        result = self.handler(context, input_data)
        if inspect.isawaitable(result):
            return await result
        return result


class ToolRegistryError(Exception):
    exit_code = 2

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = ToolError(
            code=code,
            message=redact_text(message),
            category=ErrorCategory.VALIDATION,
            details=redact_secrets(details or {}),
        )


class ToolNotFoundError(ToolRegistryError):
    pass


class ToolValidationError(ToolRegistryError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        name = definition.spec.name
        if name in self._tools:
            raise ToolValidationError(
                "duplicate_tool",
                f"Tool is already registered: {name}",
            )
        self._tools[name] = definition

    def list(self, *, include_experimental: bool = False, include_hidden: bool = False) -> list[ToolSpec]:
        definitions = sorted(self._tools.values(), key=lambda item: item.spec.name)
        if not include_experimental:
            definitions = [definition for definition in definitions if not definition.spec.experimental]
        if not include_hidden:
            definitions = [definition for definition in definitions if not definition.spec.hidden]
        return [definition.spec for definition in definitions]

    def get(self, name: str, *, allow_experimental: bool = False) -> ToolDefinition:
        try:
            definition = self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                "unknown_tool",
                f"Unknown tool: {name}",
                details={"tool": name},
            ) from exc
        if definition.spec.experimental and not allow_experimental:
            raise ToolNotFoundError(
                "experimental_tool_not_allowed",
                f"Experimental tool is not available through this path: {name}",
                details={"tool": name},
            )
        return definition

    def inspect(self, name: str, *, allow_experimental: bool = False) -> ToolSpec:
        return self.get(name, allow_experimental=allow_experimental).spec

    async def run(
        self,
        name: str,
        input_data: dict[str, Any],
        context: ToolContext,
        *,
        allow_experimental: bool = False,
    ) -> ToolResult:
        definition = self.get(name, allow_experimental=allow_experimental)
        if context.dry_run and not definition.spec.dry_run_supported:
            raise ToolValidationError(
                "dry_run_not_supported",
                f"Tool does not support dry-run: {name}",
                details={"tool": name},
            )
        errors = validate_input(definition.spec.input_schema, input_data)
        if errors:
            raise ToolValidationError(
                "invalid_input",
                "Tool input does not match the declared schema.",
                details={"tool": name, "errors": errors},
            )
        try:
            return await definition.run(context, input_data)
        except ToolRegistryError:
            raise
        except Exception as exc:
            return ToolResult.failure(
                "runtime_error",
                "Tool failed unexpectedly.",
                details={
                    "tool": name,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
                category=ErrorCategory.RUNTIME,
            )


def _path_from_env(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name)
    if not value:
        return None
    return Path(value).expanduser().resolve()
