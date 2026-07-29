"""Storage planning tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.storage import default_library_root, plan_storage_path, platform_library_env_name
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="storage.path.plan",
                description="Plan a deterministic scanner-friendly library path for one media file.",
                input_schema={
                    "type": "object",
                    "required": ["item", "file"],
                    "properties": {
                        "library_root": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "item": {"type": "object"},
                        "file": {"type": "object"},
                        "create_dirs": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=storage_path_plan,
        )
    ]


async def storage_path_plan(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        library_root, include_platform_layer = _library_root(context, input_data)
        ensure_inside(library_root, context.allowed_write_roots())
        plan = plan_storage_path(
            library_root=library_root,
            item=input_data["item"],
            file_info=input_data["file"],
            include_platform_layer=include_platform_layer,
        )
        ensure_inside(plan.final_path, [library_root])
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    create_dirs = input_data.get("create_dirs", False)
    if create_dirs and not context.dry_run:
        plan.final_path.parent.mkdir(parents=True, exist_ok=True)

    return ToolResult.success(
        {
            **plan.to_dict(),
            "would_create_dirs": bool(create_dirs and context.dry_run),
            "directories_created": bool(create_dirs and not context.dry_run),
        }
    )


def _library_root(context: ToolContext, input_data: dict[str, Any]) -> tuple[Path, bool]:
    raw_path = input_data.get("library_root")
    if raw_path:
        return (
            normalize_path(str(raw_path), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", True),
        )
    item = input_data.get("item") if isinstance(input_data.get("item"), dict) else {}
    platform = item.get("platform") or input_data.get("platform")
    if platform:
        platform_root = context.env.get(platform_library_env_name(platform))
        if platform_root:
            return (
                normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
                input_data.get("include_platform_layer", False),
            )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        input_data.get("include_platform_layer", True),
    )
