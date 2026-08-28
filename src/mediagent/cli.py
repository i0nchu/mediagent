"""Command-line bridge for Mediagent tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mediagent.agent import AgentRunner
from mediagent.agent.llm import OllamaClient
from mediagent.agent.skills import default_skill_registry
from mediagent.core.config import EnvFileError, load_env_file
from mediagent.core.tooling import ErrorCategory, ToolContext, ToolRegistryError
from mediagent.tools.defaults import create_default_registry


EXIT_SUCCESS = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_VALIDATION_ERROR = 2

VALIDATION_ERROR_CATEGORIES = {
    ErrorCategory.VALIDATION.value,
    ErrorCategory.AUTH.value,
    ErrorCategory.PERMISSION.value,
    ErrorCategory.FILESYSTEM.value,
    ErrorCategory.DATABASE.value,
}

SIMPLE_COMMANDS = {"init", "add", "sync", "status"}
SOURCE_SYNC_TOOLS = {
    "pixiv": "pixiv.bookmarks.sync",
    "telegram": "telegram.inbox.sync_links",
    "jmcomic": "jmcomic.favorites.sync",
    "nhentai": "nhentai.favorites.sync",
    "instagram": "instagram.saved.sync",
}
SOURCE_STATUS_TOOLS = {
    "pixiv": "pixiv.auth.status",
    "telegram": "telegram.auth.status",
    "jmcomic": "jmcomic.auth.status",
    "nhentai": "nhentai.auth.status",
    "instagram": "instagram.auth.status",
    "reddit": "reddit.auth.status",
    "x": "x.auth.status",
}


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "experimental":
        return handle_experimental(argv[1:])
    if argv and argv[0] in SIMPLE_COMMANDS:
        try:
            _load_simple_command_env()
        except EnvFileError as exc:
            return print_error(
                {"code": "invalid_env_file", "message": str(exc), "details": {}},
                json_output="--json" in argv or "--summary-json" in argv,
                exit_code=EXIT_VALIDATION_ERROR,
            )
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_VALIDATION_ERROR
    return args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediagent",
        description="Collect links and configured media sources into one managed library.",
    )
    subcommands = parser.add_subparsers(dest="command")

    initialize = subcommands.add_parser(
        "init",
        help="Initialize or upgrade the configured SQLite database.",
    )
    initialize.add_argument("--dry-run", action="store_true", help="Preview without changing SQLite.")
    initialize.add_argument("--json", action="store_true", help="Emit complete machine-readable JSON.")
    initialize.set_defaults(handler=handle_init)

    add = subcommands.add_parser("add", help="Download one explicit media or post URL.")
    add.add_argument("url", help="Explicit URL to resolve and download.")
    add.add_argument("--overwrite", action="store_true", help="Replace an existing target file.")
    add.add_argument(
        "--repair",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Repair missing/failed tracked content (default: enabled).",
    )
    add.add_argument("--dry-run", action="store_true", help="Preview without writing files or SQLite.")
    add.add_argument("--json", action="store_true", help="Emit complete machine-readable JSON.")
    add.set_defaults(handler=handle_add)

    sync = subcommands.add_parser("sync", help="Synchronize one configured source.")
    sync.add_argument(
        "source",
        choices=tuple(SOURCE_SYNC_TOOLS),
        help="Configured source to synchronize.",
    )
    sync.add_argument(
        "--folder",
        action="append",
        default=[],
        help="JMComic favorite folder name or ID; repeatable.",
    )
    sync.add_argument("--full", action="store_true", help="Request a complete source scan when supported.")
    sync.add_argument("--overwrite", action="store_true", help="Replace existing target files.")
    sync.add_argument(
        "--repair",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retry failures and repair missing tracked files (default: enabled).",
    )
    sync_output = sync.add_mutually_exclusive_group()
    sync_output.add_argument("--json", action="store_true", help="Emit complete machine-readable JSON.")
    sync_output.add_argument("--summary-json", action="store_true", help="Emit compact machine-readable JSON.")
    sync.add_argument("--dry-run", action="store_true", help="Preview without writing files or SQLite.")
    sync.set_defaults(handler=handle_source_sync)

    status = subcommands.add_parser(
        "status",
        help="Check core configuration or one source session.",
    )
    status.add_argument(
        "source",
        nargs="?",
        choices=tuple(SOURCE_STATUS_TOOLS),
        help="Optional source name.",
    )
    status.add_argument("--json", action="store_true", help="Emit complete machine-readable JSON.")
    status.set_defaults(handler=handle_status)

    link = subcommands.add_parser("link", help="Resolve and download explicit media links.")
    link_commands = link.add_subparsers(dest="link_command")

    link_sync = link_commands.add_parser("sync", help="Resolve and download one explicit URL.")
    link_sync.add_argument("url", help="Explicit URL to resolve and download.")
    link_sync.add_argument("--db-path", default=None, help="SQLite database path. Defaults to MEDIAGENT_DB_PATH.")
    link_sync.add_argument("--library-root", default=None, help="Output library root. Defaults to configured library root.")
    link_sync.add_argument("--target-dir", default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--write-sidecar-metadata", action="store_true", help="Write JSON sidecar metadata files.")
    link_sync.add_argument("--overwrite", action="store_true", help="Overwrite existing known target files.")
    link_sync.add_argument("--retry-failed", action="store_true", help="Retry media items currently marked failed.")
    link_sync.add_argument("--repair-missing-files", action="store_true", help="Redownload tracked files that are missing or unhealthy.")
    link_sync.add_argument("--max-html-bytes", type=int, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--max-media-bytes", type=int, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--timeout-seconds", type=float, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    link_sync.add_argument("--dry-run", action="store_true", help="Run without tool side effects.")
    link_sync.set_defaults(handler=handle_link_sync)

    library = subcommands.add_parser("library", help="Manage Mediagent-tracked library content.")
    library_commands = library.add_subparsers(dest="library_command")

    library_dedupe = library_commands.add_parser("deduplicate", help="Globally deduplicate tracked file content.")
    library_dedupe.add_argument("--db-path", default=None, help="SQLite database path. Defaults to MEDIAGENT_DB_PATH.")
    library_dedupe.add_argument("--library-root", default=None, help="Managed library root.")
    library_dedupe.add_argument("--dry-run", action="store_true", help="Hash and report without changing files or SQLite.")
    library_dedupe.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    library_dedupe.set_defaults(handler=handle_library_deduplicate)

    library_reconcile_trash = library_commands.add_parser(
        "reconcile-trash",
        help="Import verified pre-v10 trash as removed library state.",
    )
    library_reconcile_trash.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path. Defaults to MEDIAGENT_DB_PATH.",
    )
    library_reconcile_trash.add_argument("--library-root", default=None, help="Managed library root.")
    library_reconcile_trash.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify and report legacy trash without changing SQLite.",
    )
    library_reconcile_trash.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    library_reconcile_trash.set_defaults(handler=handle_library_reconcile_trash)

    library_trash = library_commands.add_parser("trash", help="Inspect or prepare Mediagent managed trash.")
    library_trash_commands = library_trash.add_subparsers(dest="library_trash_command")
    for command, help_text, handler in (
        ("status", "Inspect managed-trash ownership and permissions.", handle_library_trash_status),
        ("prepare", "Create and validate the .trash/mediagent namespace.", handle_library_trash_prepare),
    ):
        trash_command = library_trash_commands.add_parser(command, help=help_text)
        trash_command.add_argument("--library-root", default=None, help="Managed library root.")
        trash_command.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        if command == "prepare":
            trash_command.add_argument("--dry-run", action="store_true", help="Inspect without creating directories.")
        trash_command.set_defaults(handler=handler)

    library_remove = library_commands.add_parser("remove", help="Move one managed library entry to Mediagent trash.")
    _add_library_selector_arguments(library_remove)
    library_remove.add_argument("--reason", default=None, help="Optional audit reason.")
    library_remove.add_argument("--external-ref", default=None, help="Optional external-system identifier.")
    library_remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    library_remove.set_defaults(handler=handle_library_remove)

    library_restore = library_commands.add_parser("restore", help="Restore one removed managed library entry.")
    _add_library_selector_arguments(library_restore, include_path=True)
    library_restore.add_argument("--removal-id", default=None, help="Removal operation identifier.")
    library_restore.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    library_restore.set_defaults(handler=handle_library_restore)

    library_rename = library_commands.add_parser("rename", help="Rename one active managed library entry.")
    _add_library_selector_arguments(library_rename)
    library_rename.add_argument("--name", required=True, help="New display/file name; the existing extension is retained.")
    library_rename.add_argument("--external-ref", default=None, help="Optional external-system identifier.")
    library_rename.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    library_rename.set_defaults(handler=handle_library_rename)

    tools = subcommands.add_parser("tools", help="Inspect and run agent-callable tools.")
    tool_commands = tools.add_subparsers(dest="tools_command")

    tools_list = tool_commands.add_parser("list", help="List registered tools.")
    tools_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tools_list.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include undocumented experimental tools.",
    )
    tools_list.set_defaults(handler=handle_tools_list)

    tools_inspect = tool_commands.add_parser("inspect", help="Inspect a registered tool.")
    tools_inspect.add_argument("tool", help="Tool name.")
    tools_inspect.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tools_inspect.add_argument("--allow-experimental", action="store_true", help=argparse.SUPPRESS)
    tools_inspect.set_defaults(handler=handle_tools_inspect)

    tools_run = tool_commands.add_parser("run", help="Run a registered tool.")
    tools_run.add_argument("tool", help="Tool name.")
    tools_run.add_argument(
        "--input",
        default=None,
        help="Path to JSON input. Use '-' to read stdin. Defaults to an empty object.",
    )
    tool_output = tools_run.add_mutually_exclusive_group()
    tool_output.add_argument("--json", action="store_true", help="Emit complete machine-readable JSON.")
    tool_output.add_argument(
        "--summary-json",
        action="store_true",
        help="Emit a compact machine-readable summary suitable for recurring service logs.",
    )
    tools_run.add_argument("--dry-run", action="store_true", help="Run without tool side effects.")
    tools_run.add_argument("--allow-experimental", action="store_true", help=argparse.SUPPRESS)
    tools_run.set_defaults(handler=handle_tools_run)

    agent = subcommands.add_parser("agent", help="Run LLM-guided Mediagent skills.")
    agent_commands = agent.add_subparsers(dest="agent_command")

    agent_run = agent_commands.add_parser("run", help="Run a natural-language task through Agent Core.")
    agent_run.add_argument("task", help="Natural-language task.")
    agent_run.add_argument("--skill", default=None, help="Force a specific SKILL.")
    agent_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    agent_run.add_argument("--dry-run", action="store_true", help="Preview tool calls without side effects.")
    agent_run.add_argument("--execute", action="store_true", help="Execute mode is the default; kept for compatibility.")
    agent_run.add_argument("--max-steps", type=int, default=4, help="Maximum LLM/tool steps.")
    agent_run.add_argument("--allow-experimental", action="store_true", help="Allow experimental tools in SKILL allowlists.")
    agent_run.set_defaults(handler=handle_agent_run)

    agent_skills = agent_commands.add_parser("skills", help="Inspect local Agent Core SKILL files.")
    agent_skill_commands = agent_skills.add_subparsers(dest="agent_skills_command")

    agent_skills_list = agent_skill_commands.add_parser("list", help="List built-in SKILL files.")
    agent_skills_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    agent_skills_list.set_defaults(handler=handle_agent_skills_list)

    agent_skills_inspect = agent_skill_commands.add_parser("inspect", help="Inspect a built-in SKILL.")
    agent_skills_inspect.add_argument("skill", help="SKILL name.")
    agent_skills_inspect.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    agent_skills_inspect.set_defaults(handler=handle_agent_skills_inspect)

    return parser


def handle_tools_list(args: argparse.Namespace) -> int:
    registry = create_default_registry()
    specs = [spec.to_dict() for spec in registry.list(include_experimental=args.include_experimental)]
    if args.json:
        print_json({"tools": specs})
    else:
        for spec in specs:
            print(f"{spec['name']}\t{spec['description']}")
    return EXIT_SUCCESS


def handle_init(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="core.db.init",
        input_data={},
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
        compact_human=True,
    )


def handle_add(args: argparse.Namespace) -> int:
    comic_link = _is_comic_link(args.url)
    input_data: dict[str, Any] = {
        "url": args.url,
        "overwrite": args.overwrite,
        "retry_failed": args.repair,
        "repair_missing_files": args.repair,
    }
    if not comic_link:
        input_data["write_sidecar_metadata"] = False
    return run_tool_command(
        tool="comic.link.sync" if comic_link else "link.media.sync",
        input_data=input_data,
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
        compact_human=True,
    )


def handle_source_sync(args: argparse.Namespace) -> int:
    if args.folder and args.source != "jmcomic":
        return print_error(
            {
                "code": "unsupported_source_option",
                "message": "--folder is only supported for the jmcomic source.",
                "details": {"source": args.source},
            },
            json_output=args.json or args.summary_json,
            exit_code=EXIT_VALIDATION_ERROR,
        )
    input_data: dict[str, Any] = {
        "overwrite": args.overwrite,
        "retry_failed": args.repair,
        "repair_missing_files": args.repair,
    }
    if args.full and args.source in {"pixiv", "telegram", "instagram"}:
        input_data["full_sync"] = True
    if args.source == "pixiv":
        input_data.update({"package_comics": True, "include_ugoira_metadata": True})
    if args.folder:
        input_data["folders"] = args.folder
    return run_tool_command(
        tool=SOURCE_SYNC_TOOLS[args.source],
        input_data=input_data,
        json_output=args.json,
        summary_json=args.summary_json,
        dry_run=args.dry_run,
        compact_human=True,
    )


def handle_status(args: argparse.Namespace) -> int:
    if args.source:
        tool = SOURCE_STATUS_TOOLS[args.source]
        input_data: dict[str, Any] = {}
    else:
        tool = "core.env.check"
        required = ["MEDIAGENT_DATA_DIR", "MEDIAGENT_DB_PATH"]
        library_paths = sorted(
            name
            for name in os.environ
            if name.startswith("MEDIAGENT_") and name.endswith("_LIBRARY_DIR")
        )
        inspected_paths = list(dict.fromkeys([*required, "MEDIAGENT_LIBRARY_DIR", *library_paths]))
        input_data = {"required": required, "path_vars": inspected_paths}
    return run_tool_command(
        tool=tool,
        input_data=input_data,
        json_output=args.json,
        summary_json=False,
        dry_run=False,
        compact_human=True,
    )


def handle_tools_inspect(args: argparse.Namespace) -> int:
    registry = create_default_registry()
    try:
        spec = registry.inspect(args.tool, allow_experimental=args.allow_experimental).to_dict()
    except ToolRegistryError as exc:
        return print_error(exc.error.to_dict(), json_output=args.json, exit_code=exc.exit_code)
    if args.json:
        print_json({"tool": spec})
    else:
        print(f"{spec['name']}")
        print(f"  description: {spec['description']}")
        print(f"  permissions: {', '.join(spec['permissions'])}")
        print(f"  dry_run_supported: {spec['dry_run_supported']}")
    return EXIT_SUCCESS


def handle_tools_run(args: argparse.Namespace) -> int:
    try:
        input_data = read_input(args.input)
    except ValueError as exc:
        return print_error(
            {"code": "invalid_input_file", "message": str(exc), "details": {}},
            json_output=args.json,
            exit_code=EXIT_VALIDATION_ERROR,
        )

    return run_tool_command(
        tool=args.tool,
        input_data=input_data,
        json_output=args.json,
        summary_json=args.summary_json,
        dry_run=args.dry_run,
        allow_experimental=args.allow_experimental,
    )


def handle_link_sync(args: argparse.Namespace) -> int:
    comic_link = _is_comic_link(args.url)
    input_data: dict[str, Any] = {
        "url": args.url,
        "overwrite": args.overwrite,
    }
    if not comic_link or args.retry_failed:
        input_data["retry_failed"] = args.retry_failed
    if not comic_link or args.repair_missing_files:
        input_data["repair_missing_files"] = args.repair_missing_files
    if not comic_link:
        input_data["write_sidecar_metadata"] = args.write_sidecar_metadata
    optional_fields = {
        "db_path": args.db_path,
        "library_root": args.library_root,
        "target_dir": None if comic_link else args.target_dir,
        "max_html_bytes": None if comic_link else args.max_html_bytes,
        "max_media_bytes": args.max_media_bytes,
        "timeout_seconds": args.timeout_seconds,
    }
    input_data.update({key: value for key, value in optional_fields.items() if value is not None})
    return run_tool_command(
        tool="comic.link.sync" if comic_link else "link.media.sync",
        input_data=input_data,
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
    )


def _add_library_selector_arguments(parser: argparse.ArgumentParser, *, include_path: bool = True) -> None:
    selector = parser.add_mutually_exclusive_group()
    if include_path:
        selector.add_argument("--path", default=None, help="Managed library file path.")
    selector.add_argument("--entry-id", default=None, help="Stable Mediagent library entry identifier.")
    parser.add_argument("--db-path", default=None, help="SQLite database path. Defaults to MEDIAGENT_DB_PATH.")
    parser.add_argument("--library-root", default=None, help="Managed library root.")


def _library_input(args: argparse.Namespace, *names: str) -> dict[str, Any]:
    return {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}


def handle_library_deduplicate(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.content.deduplicate",
        input_data=_library_input(args, "db_path", "library_root"),
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
    )


def handle_library_reconcile_trash(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.trash.reconcile",
        input_data=_library_input(args, "db_path", "library_root"),
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
    )


def handle_library_trash_status(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.trash.status",
        input_data=_library_input(args, "library_root"),
        json_output=args.json,
        summary_json=False,
        dry_run=False,
    )


def handle_library_trash_prepare(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.trash.prepare",
        input_data=_library_input(args, "library_root"),
        json_output=args.json,
        summary_json=False,
        dry_run=args.dry_run,
    )


def handle_library_remove(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.entry.remove",
        input_data=_library_input(args, "db_path", "library_root", "path", "entry_id", "reason", "external_ref"),
        json_output=args.json,
        summary_json=False,
        dry_run=False,
    )


def handle_library_restore(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.entry.restore",
        input_data=_library_input(args, "db_path", "library_root", "path", "entry_id", "removal_id"),
        json_output=args.json,
        summary_json=False,
        dry_run=False,
    )


def handle_library_rename(args: argparse.Namespace) -> int:
    return run_tool_command(
        tool="library.entry.rename",
        input_data=_library_input(args, "db_path", "library_root", "path", "entry_id", "name", "external_ref"),
        json_output=args.json,
        summary_json=False,
        dry_run=False,
    )


def _is_comic_link(url: str) -> bool:
    from mediagent.tools.comic_tools import comic_link_provider

    return comic_link_provider(url) is not None


def handle_agent_skills_list(args: argparse.Namespace) -> int:
    registry = default_skill_registry()
    skills = [skill.summary() for skill in registry.list()]
    if args.json:
        print_json({"skills": skills})
    else:
        for skill in skills:
            print(f"{skill['name']}\t{skill['description']}")
    return EXIT_SUCCESS


def handle_agent_skills_inspect(args: argparse.Namespace) -> int:
    registry = default_skill_registry()
    try:
        skill = registry.get(args.skill)
    except KeyError as exc:
        return print_error(
            {"code": "unknown_skill", "message": str(exc), "details": {"skill": args.skill}},
            json_output=args.json,
            exit_code=EXIT_VALIDATION_ERROR,
        )
    if args.json:
        print_json({"skill": skill.to_dict()})
    else:
        print(f"{skill.name}")
        print(f"  description: {skill.description}")
        print(f"  allowed_tools: {', '.join(skill.allowed_tools)}")
        print(f"  default_dry_run: {skill.default_dry_run}")
        print(f"  risk_level: {skill.risk_level}")
        print(f"  requires_initial_tool_call: {skill.requires_initial_tool_call}")
        print(f"  supports_unbounded: {skill.supports_unbounded}")
        if skill.supported_intents:
            print(f"  supported_intents: {', '.join(skill.supported_intents)}")
        if skill.unsupported_intents:
            print(f"  unsupported_intents: {', '.join(skill.unsupported_intents)}")
        print()
        print(skill.body)
    return EXIT_SUCCESS


def handle_agent_run(args: argparse.Namespace) -> int:
    if args.dry_run and args.execute:
        return print_error(
            {
                "code": "invalid_agent_mode",
                "message": "Use either --dry-run or --execute, not both.",
                "details": {},
            },
            json_output=args.json,
            exit_code=EXIT_VALIDATION_ERROR,
        )
    try:
        llm_client = build_llm_client()
    except ValueError as exc:
        return print_error(
            {"code": "invalid_llm_config", "message": str(exc), "details": {}},
            json_output=args.json,
            exit_code=EXIT_VALIDATION_ERROR,
        )
    execute = not args.dry_run
    context = ToolContext.from_env(dry_run=args.dry_run)
    runner = AgentRunner.default(
        llm_client,
        max_steps=args.max_steps,
        allow_experimental=args.allow_experimental,
    )
    result = asyncio.run(
        runner.run(
            task=args.task,
            context=context,
            skill_name=args.skill,
            execute=execute,
        )
    )
    payload = result.to_dict()
    if args.json:
        print_json(payload)
    else:
        print_agent_human_result(payload)
    if result.is_success:
        return EXIT_SUCCESS
    if result.status.value == "needs_user":
        return EXIT_VALIDATION_ERROR
    return EXIT_RUNTIME_FAILURE


def run_tool_command(
    *,
    tool: str,
    input_data: dict[str, Any],
    json_output: bool,
    summary_json: bool,
    dry_run: bool,
    allow_experimental: bool = False,
    compact_human: bool = False,
) -> int:
    registry = create_default_registry()
    context = ToolContext.from_env(dry_run=dry_run)
    try:
        result = asyncio.run(
            registry.run(
                tool,
                input_data,
                context,
                allow_experimental=allow_experimental,
            )
        )
    except ToolRegistryError as exc:
        return print_error(exc.error.to_dict(), json_output=json_output, exit_code=exc.exit_code)
    payload = {
        "tool": tool,
        "run_id": context.run_id,
        **result.to_dict(),
    }
    if summary_json:
        print_json(_summary_tool_payload(payload))
    elif json_output:
        print_json(payload)
    elif compact_human:
        print_compact_human_result(payload)
    else:
        print_human_result(payload)
    if result.is_success:
        return EXIT_SUCCESS
    if result.error and result.error.category.value in VALIDATION_ERROR_CATEGORIES:
        return EXIT_VALIDATION_ERROR
    return EXIT_RUNTIME_FAILURE


def _summary_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    compact_data = {
        key: data[key]
        for key in (
            "provider",
            "collection",
            "policy",
            "target_policy",
            "complete",
            "pages_fetched",
            "expected_total",
            "favorites_seen",
            "following",
            "auth_recovery_attempted",
            "auth_recovered",
            "session_checkpointed",
            "session_checkpoints",
            "dry_run",
            "snapshot",
            "summary",
        )
        if key in data
    }
    return {
        "tool": payload.get("tool"),
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "data": compact_data,
        "artifact_count": len(payload.get("artifacts") or []),
        "warnings": payload.get("warnings") or [],
        "rate_limit": payload.get("rate_limit"),
        "error": payload.get("error"),
    }


def _load_simple_command_env() -> None:
    configured = os.environ.get("MEDIAGENT_ENV_FILE")
    if configured == "":
        return
    env_path = Path(configured).expanduser() if configured else Path.cwd() / ".env"
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    load_env_file(env_path.resolve())


def build_llm_client() -> OllamaClient:
    import os

    provider = os.environ.get("MEDIAGENT_LLM_PROVIDER", "ollama").strip().lower()
    if provider != "ollama":
        raise ValueError(f"Unsupported LLM provider: {provider}")
    return OllamaClient(
        base_url=os.environ.get("MEDIAGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        model=os.environ.get("MEDIAGENT_OLLAMA_MODEL", "qwen3:8b"),
        timeout=float(os.environ.get("MEDIAGENT_OLLAMA_TIMEOUT_SECONDS", "60")),
        num_predict=int(os.environ.get("MEDIAGENT_OLLAMA_NUM_PREDICT", "512")),
    )


def handle_experimental_telegram_sync_links(args: argparse.Namespace) -> int:
    args.tool = "telegram.inbox.sync_links"
    args.allow_experimental = True
    return handle_tools_run(args)


def handle_experimental(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "telegram" and argv[1] == "sync-links":
        parser = argparse.ArgumentParser(prog="mediagent experimental telegram sync-links")
        parser.add_argument(
            "--input",
            default=None,
            help="Path to JSON input. Use '-' to read stdin. Defaults to an empty object.",
        )
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        parser.add_argument("--dry-run", action="store_true", help="Run without tool side effects.")
        args = parser.parse_args(argv[2:])
        return handle_experimental_telegram_sync_links(args)
    return print_error(
        {
            "code": "unknown_experimental_command",
            "message": "Unknown experimental command.",
            "details": {},
        },
        json_output="--json" in argv,
        exit_code=EXIT_VALIDATION_ERROR,
    )


def read_input(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON input: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Tool input must be a JSON object.")
    return data


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def print_error(error: dict[str, Any], *, json_output: bool, exit_code: int) -> int:
    if json_output:
        print_json({"status": "failure", "error": error})
    else:
        print(f"error: {error['message']}", file=sys.stderr)
    return exit_code


def print_human_result(payload: dict[str, Any]) -> None:
    print(f"status: {payload['status']}")
    if payload.get("data"):
        print(json.dumps(payload["data"], ensure_ascii=False, indent=2, sort_keys=True))
    if payload.get("warnings"):
        for warning in payload["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    if payload.get("error"):
        print(f"error: {payload['error']['message']}", file=sys.stderr)


def print_compact_human_result(payload: dict[str, Any]) -> None:
    compact = _summary_tool_payload(payload)
    source_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for key in (
        "auth_status",
        "authenticated",
        "remote_verified",
        "reusable",
        "credentials_configured",
        "session_configured",
        "session_present",
        "missing",
        "paths",
    ):
        if key in source_data:
            compact["data"][key] = source_data[key]
    print(f"status: {compact['status']}")
    print(f"operation: {compact['tool']}")
    if compact["data"]:
        print(json.dumps(compact["data"], ensure_ascii=False, indent=2, sort_keys=True))
    for warning in compact["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if compact["error"]:
        print(f"error: {compact['error']['message']}", file=sys.stderr)


def print_agent_human_result(payload: dict[str, Any]) -> None:
    print(f"status: {payload['status']}")
    print(f"skill: {payload.get('skill')}")
    print(f"dry_run: {payload.get('dry_run')}")
    if payload.get("message"):
        print(payload["message"])
    for step in payload.get("steps") or []:
        action = step.get("action") or {}
        print(f"step {step.get('index')}: {action.get('action')}")
        if action.get("tool"):
            print(f"  tool: {action['tool']}")
        if step.get("tool_result"):
            print(json.dumps(step["tool_result"], ensure_ascii=False, indent=2, sort_keys=True))
        if step.get("error"):
            print(f"  error: {step['error']['message']}", file=sys.stderr)
    if payload.get("error"):
        print(f"error: {payload['error']['message']}", file=sys.stderr)
