"""Download tools."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.http import UrllibHttpClient
from mediagent.core.rate_limit import extract_rate_limit
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="download.http",
                description="Download one remote file to a safe local path.",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "required_any": [["target_path", "target_dir"]],
                    "required_with": [{"field": "target_dir", "required": ["filename"]}],
                    "properties": {
                        "url": {"type": "string"},
                        "target_path": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "filename": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                        "attempts": {"type": "integer"},
                        "timeout_seconds": {"type": "number"},
                        "expected_mime_prefix": {"type": "string"},
                        "headers": {"type": "object"},
                        "use_partial": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=download_http,
        )
    ]


async def download_http(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        target_path = _target_path(context, input_data)
        ensure_inside(target_path, context.allowed_write_roots())
        partial_path = target_path.with_name(target_path.name + ".partial")
        ensure_inside(partial_path, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    overwrite = input_data.get("overwrite", False)
    if target_path.exists() and not overwrite:
        return ToolResult.failure(
            "target_exists",
            "Target file already exists and overwrite is false.",
            details={"target_path": str(target_path)},
            category=ErrorCategory.VALIDATION,
        )

    if context.dry_run:
        return ToolResult.success(
            {
                "url": input_data["url"],
                "target_path": str(target_path),
                "partial_path": str(partial_path),
                "would_download": True,
            }
        )

    attempts = max(1, input_data.get("attempts", 3))
    timeout = float(input_data.get("timeout_seconds", 30.0))
    client = context.http_client or UrllibHttpClient()
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            response = client.get(
                input_data["url"],
                headers=input_data.get("headers"),
                timeout=timeout,
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        if 200 <= response.status_code < 300:
            validation_error = _validate_response(response.headers, response.content, input_data)
            if validation_error:
                last_error = validation_error
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            use_partial = input_data.get("use_partial", True)
            write_path = partial_path if use_partial else target_path
            try:
                write_path.write_bytes(response.content)
                if use_partial:
                    partial_path.replace(target_path)
            except Exception:
                if use_partial and partial_path.exists():
                    partial_path.unlink()
                raise
            checksum = hashlib.sha256(response.content).hexdigest()
            return ToolResult.success(
                {
                    "url": input_data["url"],
                    "target_path": str(target_path),
                    "partial_path": str(partial_path) if use_partial else None,
                    "finalized": True,
                    "size_bytes": len(response.content),
                    "checksum": f"sha256:{checksum}",
                    "attempts": attempt,
                    "mime_type": _header(response.headers, "content-type"),
                },
                artifacts=[{"type": "file", "path": str(target_path)}],
                rate_limit=extract_rate_limit(response.headers),
            )
        last_error = f"HTTP {response.status_code}"
        if response.status_code == 429:
            return ToolResult.failure(
                "rate_limited",
                "Download was rate limited.",
                details={"attempt": attempt},
                category=ErrorCategory.RATE_LIMIT,
                rate_limit=extract_rate_limit(response.headers),
            )

    return ToolResult.failure(
        "download_failed",
        "Download failed after bounded attempts.",
        details={"attempts": attempts, "last_error": last_error},
        category=ErrorCategory.NETWORK,
    )


def _target_path(context: ToolContext, input_data: dict[str, Any]) -> Path:
    if input_data.get("target_path"):
        return normalize_path(input_data["target_path"], env=context.env, cwd=context.cwd)
    if not input_data.get("target_dir") or not input_data.get("filename"):
        raise ValueError("Provide target_path or both target_dir and filename.")
    target_dir = normalize_path(input_data["target_dir"], env=context.env, cwd=context.cwd)
    return (target_dir / input_data["filename"]).resolve()


def _validate_response(
    headers: dict[str, str],
    content: bytes,
    input_data: dict[str, Any],
) -> str | None:
    content_length = _header(headers, "content-length")
    if content_length is not None and int(content_length) != len(content):
        return "Content length does not match response body size."
    expected_mime_prefix = input_data.get("expected_mime_prefix")
    content_type = _header(headers, "content-type")
    if expected_mime_prefix and (not content_type or not content_type.startswith(expected_mime_prefix)):
        return f"Content type does not start with {expected_mime_prefix!r}."
    return None


def _header(headers: dict[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return None
