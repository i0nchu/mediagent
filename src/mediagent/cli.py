"""Command-line bridge for Mediagent tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

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


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "experimental":
        return handle_experimental(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_VALIDATION_ERROR
    return args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mediagent")
    subcommands = parser.add_subparsers(dest="command")

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
    link_sync.add_argument("--max-html-bytes", type=int, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--max-media-bytes", type=int, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--timeout-seconds", type=float, default=None, help=argparse.SUPPRESS)
    link_sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    link_sync.add_argument("--dry-run", action="store_true", help="Run without tool side effects.")
    link_sync.set_defaults(handler=handle_link_sync)

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
    tools_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tools_run.add_argument("--dry-run", action="store_true", help="Run without tool side effects.")
    tools_run.add_argument("--allow-experimental", action="store_true", help=argparse.SUPPRESS)
    tools_run.set_defaults(handler=handle_tools_run)

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
        dry_run=args.dry_run,
        allow_experimental=args.allow_experimental,
    )


def handle_link_sync(args: argparse.Namespace) -> int:
    input_data: dict[str, Any] = {
        "url": args.url,
        "write_sidecar_metadata": args.write_sidecar_metadata,
        "overwrite": args.overwrite,
        "retry_failed": args.retry_failed,
    }
    optional_fields = {
        "db_path": args.db_path,
        "library_root": args.library_root,
        "target_dir": args.target_dir,
        "max_html_bytes": args.max_html_bytes,
        "max_media_bytes": args.max_media_bytes,
        "timeout_seconds": args.timeout_seconds,
    }
    input_data.update({key: value for key, value in optional_fields.items() if value is not None})
    return run_tool_command(
        tool="link.media.sync",
        input_data=input_data,
        json_output=args.json,
        dry_run=args.dry_run,
    )


def run_tool_command(
    *,
    tool: str,
    input_data: dict[str, Any],
    json_output: bool,
    dry_run: bool,
    allow_experimental: bool = False,
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
    if json_output:
        print_json(payload)
    else:
        print_human_result(payload)
    if result.is_success:
        return EXIT_SUCCESS
    if result.error and result.error.category.value in VALIDATION_ERROR_CATEGORIES:
        return EXIT_VALIDATION_ERROR
    return EXIT_RUNTIME_FAILURE


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
