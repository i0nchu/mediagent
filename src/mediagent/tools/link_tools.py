"""Undocumented experimental link resolver tools."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from mediagent.core import db, library_content
from mediagent.core.comics import IGNORED_COMIC_SPACER_HEALTH
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.links import (
    ALLOWED_MEDIA_MIME_TYPES,
    LinkSafetyPolicy,
    ResolveRequest,
    URLSafetyError,
    clean_mime,
    default_link_resolver_registry,
    fetch_limited_follow_redirects,
    header_value,
    int_header,
    normalize_url,
    resolution_to_media_item,
    sanitize_link_resolution_for_output,
)
from mediagent.core.storage import default_library_root, plan_storage_path, platform_library_env_name
from mediagent.core.sync import TERMINAL_ITEM_STATUSES, item_status_from_file_counts
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec
from mediagent.tools.metadata_tools import metadata_write


@dataclass(frozen=True)
class _ContentTransformOutcome:
    content: bytes | None
    skip_reason: str | None = None


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="link.queue.upsert",
                description="Queue explicit URLs with normalized URL dedupe.",
                input_schema={
                    "type": "object",
                    "required_any": [["url", "urls"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "url": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "ingest_platform": {"type": "string"},
                        "source": {"type": "object"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=queue_upsert,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="link.resolve.preview",
                description="Safely resolve one queued or direct URL without downloading.",
                input_schema={
                    "type": "object",
                    "required_any": [["url", "link_id"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "url": {"type": "string"},
                        "link_id": {"type": "integer"},
                        "record": {"type": "boolean"},
                        "timeout_seconds": {"type": "number"},
                        "max_redirects": {"type": "integer"},
                        "max_html_bytes": {"type": "integer"},
                        "max_media_bytes": {"type": "integer"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
                experimental=True,
            ),
            handler=resolve_preview,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="link.resolve.to_media_item",
                description="Convert one safe link resolution into a normalized media item.",
                input_schema={
                    "type": "object",
                    "required_any": [["resolution", "url", "link_id"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "resolution": {"type": "object"},
                        "url": {"type": "string"},
                        "link_id": {"type": "integer"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
                experimental=True,
            ),
            handler=resolve_to_media_item,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="link.media.sync",
                description="Resolve explicit links, dedupe media items, download files, and record media state.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "url": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "link_id": {"type": "integer"},
                        "link_ids": {"type": "array", "items": {"type": "integer"}},
                        "status": {"type": "string"},
                        "library_root": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "overwrite": {"type": "boolean"},
                        "retry_failed": {"type": "boolean"},
                        "retry_auth_skipped": {"type": "boolean"},
                        "repair_missing_files": {"type": "boolean"},
                        "lease_seconds": {"type": "integer"},
                        "timeout_seconds": {"type": "number"},
                        "max_redirects": {"type": "integer"},
                        "max_html_bytes": {"type": "integer"},
                        "max_media_bytes": {"type": "integer"},
                        "write_sidecar_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.NETWORK,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=media_sync,
        ),
    ]


async def queue_upsert(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        return db_path
    urls = _input_urls(input_data)
    if not urls:
        return ToolResult.failure(
            "missing_url",
            "Provide url or urls.",
            category=ErrorCategory.VALIDATION,
        )
    links = []
    for url in urls:
        record = _link_record_from_url(url, input_data=input_data, context=context)
        if context.dry_run:
            links.append({**record, "id": None, "is_new": None})
            continue
        db.initialize_database(db_path)
        links.append(db.upsert_link(db_path, record))
    return ToolResult.success(
        {
            "db_path": str(db_path),
            "links": links,
            "summary": {
                "received": len(urls),
                "queued": len(links),
                "dry_run": context.dry_run,
            },
        }
    )


async def resolve_preview(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    link = None
    db_path = _db_path(context, input_data)
    if input_data.get("link_id") is not None:
        if not db_path:
            return ToolResult.failure(
                "missing_db_path",
                "Provide db_path or set MEDIAGENT_DB_PATH when using link_id.",
                category=ErrorCategory.VALIDATION,
            )
        link = db.get_link(db_path, link_id=int(input_data["link_id"]))
        if link is None:
            return ToolResult.failure(
                "unknown_link",
                "Link queue record was not found.",
                details={"link_id": input_data["link_id"]},
                category=ErrorCategory.DATABASE,
            )
        raw_url = link["original_url"]
    else:
        raw_url = input_data["url"]

    resolution = _resolve_url(context, raw_url, input_data)
    should_record = bool(input_data.get("record", input_data.get("link_id") is not None))
    if link and db_path and should_record and not context.dry_run:
        status = "resolved" if resolution.get("status") == "resolved" else "skipped"
        link = db.update_link_resolution(
            db_path,
            link_id=int(link["id"]),
            status=status,
            resolution=resolution,
            skip_reason=resolution.get("skip_reason"),
        )
    public_resolution = sanitize_link_resolution_for_output(resolution)
    return ToolResult.success(
        {
            "resolution": public_resolution,
            "link": link,
            "recorded": bool(link and should_record and not context.dry_run),
        },
        warnings=resolution.get("warnings") or [],
    )


async def resolve_to_media_item(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolution = input_data.get("resolution")
    link = None
    db_path = _db_path(context, input_data)
    if not isinstance(resolution, dict):
        if input_data.get("link_id") is not None:
            if not db_path:
                return ToolResult.failure(
                    "missing_db_path",
                    "Provide db_path or set MEDIAGENT_DB_PATH when using link_id.",
                    category=ErrorCategory.VALIDATION,
                )
            link = db.get_link(db_path, link_id=int(input_data["link_id"]))
            if link is None:
                return ToolResult.failure(
                    "unknown_link",
                    "Link queue record was not found.",
                    details={"link_id": input_data["link_id"]},
                    category=ErrorCategory.DATABASE,
                )
            resolution = link.get("resolution") or {}
            if resolution.get("status") not in {"resolved", "skipped"}:
                resolution = _resolve_url(context, link["original_url"], input_data)
        else:
            resolution = _resolve_url(context, input_data["url"], input_data)

    provenance = _ingest_provenance(link)
    item = resolution_to_media_item(resolution, ingest_provenance=provenance)
    if item is None:
        return ToolResult.success(
            {
                "converted": False,
                "skip_reason": resolution.get("skip_reason"),
                "resolution": sanitize_link_resolution_for_output(resolution),
                "link": link,
            }
        )
    return ToolResult.success(
        {
            "converted": True,
            "item": _public_media_item(item),
            "resolution": sanitize_link_resolution_for_output(resolution),
            "link": link,
        }
    )


async def media_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        return db_path
    try:
        links = _sync_input_links(db_path, input_data, context=context)
    except ValueError as exc:
        return ToolResult.failure("missing_link_input", str(exc), category=ErrorCategory.VALIDATION)
    links = _limited_items(links, input_data.get("limit"))
    resolutions: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    summary = {
        "links_considered": len(links),
        "auth_links_retried": sum(1 for link in links if link.get("_auth_retry")),
        "resolved": 0,
        "skipped_links": 0,
        "queued": 0,
        "skipped_items": 0,
        "skipped_healthy": 0,
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
        "repaired": 0,
        "still_missing_files": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_deduplicated": 0,
        "dedup_bytes_reclaimed": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "comic_links_considered": 0,
        "comic_links_failed": 0,
        "cbz_packaged": 0,
        "cbz_existing": 0,
        "cbz_failed_or_incomplete": 0,
    }
    warnings: list[str] = []
    comic_route = await sync_dedicated_comic_links(
        context,
        input_data,
        links,
        db_path=db_path,
    )
    links = comic_route["remaining_links"]
    resolutions.extend(comic_route["links"])
    warnings.extend(comic_route["warnings"])

    policy = _link_safety_policy(input_data)
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
    )
    for link in links:
        resolution = default_link_resolver_registry().resolve(link["original_url"], request=request)
        resolutions.append({"link": _safe_link_record(link), "resolution": sanitize_link_resolution_for_output(resolution)})
        if not context.dry_run and link.get("id") is not None:
            db.update_link_resolution(
                db_path,
                link_id=int(link["id"]),
                status="resolved" if resolution.get("status") == "resolved" else "skipped",
                resolution=resolution,
                skip_reason=resolution.get("skip_reason"),
            )
        if resolution.get("status") != "resolved":
            summary["skipped_links"] += 1
            continue
        summary["resolved"] += 1
        item = resolution_to_media_item(resolution, ingest_provenance=_link_ingest_provenance(link))
        if item is None:
            summary["skipped_links"] += 1
            continue
        resolved_items.append(item)
    resolved_items = _dedupe_media_items(resolved_items)
    statuses = db.get_media_statuses(db_path, resolved_items)
    items_to_sync, candidate_summary = _sync_candidates(
        resolved_items,
        statuses,
        db_path=db_path,
        retry_failed=input_data.get("retry_failed", False),
        repair_missing_files=input_data.get("repair_missing_files", False),
    )
    summary.update(candidate_summary)
    summary["queued"] = len(items_to_sync)
    merge_comic_route_summary(summary, comic_route["summary"])

    if context.dry_run:
        planned_downloads = list(comic_route["planned_downloads"])
        for item in items_to_sync:
            planned_downloads.extend(_planned_downloads(context, input_data, item))
        data = {
            "db_path": str(db_path),
            "summary": summary,
            "links": resolutions,
            "planned_downloads": planned_downloads,
        }
        if comic_route["failed"]:
            return ToolResult.failure(
                "link_media_sync_partial" if summary["resolved"] else "link_media_sync_failed",
                "Link media sync preview could not resolve every dedicated comic source.",
                data=data,
                warnings=warnings,
                category=ErrorCategory.NETWORK,
            )
        return ToolResult.success(data, warnings=warnings)

    db.initialize_database(db_path)
    for item in resolved_items:
        db.upsert_media_item(db_path, item)

    item_results: list[dict[str, Any]] = list(comic_route["items"])
    artifacts: list[dict[str, str]] = list(comic_route["artifacts"])
    for item in items_to_sync:
        result = await _sync_one_link_item(context, db_path, item, input_data)
        item_results.append(result)
        summary[result["status"]] += 1
        summary["files_downloaded"] += result["files_downloaded"]
        summary["files_deduplicated"] += result.get("files_deduplicated", 0)
        summary["dedup_bytes_reclaimed"] += result.get("dedup_bytes_reclaimed", 0)
        summary["files_failed"] += result["files_failed"]
        summary["bytes_written"] += result["bytes_written"]
        if item.get("_repair"):
            if result["status"] == "downloaded":
                summary["repaired"] += 1
            summary["still_missing_files"] += result["files_failed"]
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])

    run_status = "success"
    if summary["failed"] or summary["partial"] or comic_route["failed"]:
        run_status = "partial" if summary["downloaded"] or summary["partial"] else "failed"
    db.insert_run(
        db_path,
        run_type="tool",
        name="link.media.sync",
        status=run_status,
        summary=summary,
        error=None,
        dry_run=False,
    )
    data = {
        "db_path": str(db_path),
        "summary": summary,
        "links": resolutions,
        "items": item_results,
        "packages": comic_route["packages"],
    }
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings)
    return ToolResult.failure(
        "link_media_sync_partial" if run_status == "partial" else "link_media_sync_failed",
        "Link media sync finished with failed or partially downloaded items.",
        data=data,
        warnings=warnings,
        category=ErrorCategory.NETWORK,
    )


async def sync_dedicated_comic_links(
    context: ToolContext,
    input_data: dict[str, Any],
    links: list[dict[str, Any]],
    *,
    db_path: Path,
) -> dict[str, Any]:
    """Route recognized comic links through their multi-page exact adapters."""

    from mediagent.tools import comic_tools

    comic_links: list[tuple[dict[str, Any], str]] = []
    remaining_links: list[dict[str, Any]] = []
    for link in links:
        provider = comic_tools.comic_link_provider(str(link.get("original_url") or ""))
        if provider is None:
            remaining_links.append(link)
        else:
            comic_links.append((link, provider))

    summary = {
        "comic_links_considered": len(comic_links),
        "comic_links_failed": 0,
        "resolved": 0,
        "skipped_links": 0,
        "queued": 0,
        "skipped_items": 0,
        "skipped_healthy": 0,
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
        "repaired": 0,
        "still_missing_files": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "cbz_packaged": 0,
        "cbz_existing": 0,
        "cbz_failed_or_incomplete": 0,
    }
    output_links: list[dict[str, Any]] = []
    item_results: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    planned_downloads: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    warnings: list[str] = []
    route_failed = False

    for link, provider in comic_links:
        comic_input = dict(input_data)
        for key in ("urls", "link_id", "link_ids", "status", "limit", "lease_seconds", "target_dir"):
            comic_input.pop(key, None)
        comic_input["url"] = link["original_url"]
        comic_input["_ingest_provenance"] = _link_ingest_provenance(link)
        result = await comic_tools.comic_link_sync(context, comic_input)
        targets = result.data.get("targets") if isinstance(result.data, dict) else None
        resolved = bool(targets)
        error_code = result.error.code if result.error else None
        resolution = {
            "status": "resolved" if resolved else "skipped",
            "resolver": f"{provider}_comic_exact",
            "source_url": link["original_url"],
            "normalized_url": link.get("normalized_url") or normalize_url(link["original_url"]),
            "canonical_url": link.get("normalized_url") or normalize_url(link["original_url"]),
            "origin_source": provider,
            "media_type": "photo",
            "remote_id": (targets or [{}])[0].get("target"),
            "skip_reason": None if resolved else ("requires_auth" if result.error and result.error.category == ErrorCategory.AUTH else error_code),
            "details": {
                "work_type": "comic",
                "policy": "exact",
                "target_count": len(targets or []),
                "error_code": error_code,
            },
        }
        output_links.append(
            {
                "link": _safe_link_record(link),
                "resolution": sanitize_link_resolution_for_output(resolution),
            }
        )
        if not context.dry_run and link.get("id") is not None:
            db.update_link_resolution(
                db_path,
                link_id=int(link["id"]),
                status="resolved" if resolved else "skipped",
                resolution=resolution,
                skip_reason=resolution["skip_reason"],
            )
        if resolved:
            summary["resolved"] += 1
        else:
            summary["skipped_links"] += 1
        if not result.is_success:
            route_failed = True
            summary["comic_links_failed"] += 1

        comic_summary = result.data.get("summary", {}) if isinstance(result.data, dict) else {}
        for key in (
            "queued",
            "skipped_items",
            "skipped_healthy",
            "repair_items",
            "repair_files_missing",
            "repair_files_corrupt",
            "repair_files_unhealthy",
            "repaired",
            "still_missing_files",
            "downloaded",
            "partial",
            "failed",
            "cbz_packaged",
            "cbz_existing",
            "cbz_failed_or_incomplete",
        ):
            summary[key] += int(comic_summary.get(key, 0) or 0)
        current_items = result.data.get("items", []) if isinstance(result.data, dict) else []
        item_results.extend(current_items)
        for item_result in current_items:
            summary["files_downloaded"] += int(item_result.get("files_downloaded", 0) or 0)
            summary["files_failed"] += int(item_result.get("files_failed", 0) or 0)
            summary["bytes_written"] += int(item_result.get("bytes_written", 0) or 0)
        packages.extend(result.data.get("packages", []) if isinstance(result.data, dict) else [])
        planned_downloads.extend(result.data.get("planned_downloads", []) if isinstance(result.data, dict) else [])
        artifacts.extend(result.artifacts)
        warnings.extend(result.warnings)

    return {
        "remaining_links": remaining_links,
        "summary": summary,
        "links": output_links,
        "items": item_results,
        "packages": packages,
        "planned_downloads": planned_downloads,
        "artifacts": artifacts,
        "warnings": warnings,
        "failed": route_failed,
    }


def merge_comic_route_summary(summary: dict[str, Any], comic_summary: dict[str, Any]) -> None:
    for key, value in comic_summary.items():
        if key in summary:
            summary[key] += int(value or 0)


def _resolve_url(context: ToolContext, raw_url: str, input_data: dict[str, Any]) -> dict[str, Any]:
    policy = LinkSafetyPolicy(
        max_redirects=max(0, int(input_data.get("max_redirects", 3))),
        timeout_seconds=float(input_data.get("timeout_seconds", 30.0)),
        max_html_bytes=max(1, int(input_data.get("max_html_bytes", 1024 * 1024))),
        max_media_bytes=max(1, int(input_data.get("max_media_bytes", 1024 * 1024 * 1024))),
    )
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
    )
    return default_link_resolver_registry().resolve(raw_url, request=request)


def _link_safety_policy(input_data: dict[str, Any]) -> LinkSafetyPolicy:
    return LinkSafetyPolicy(
        max_redirects=max(0, int(input_data.get("max_redirects", 3))),
        timeout_seconds=float(input_data.get("timeout_seconds", 30.0)),
        max_html_bytes=max(1, int(input_data.get("max_html_bytes", 1024 * 1024))),
        max_media_bytes=max(1, int(input_data.get("max_media_bytes", 1024 * 1024 * 1024))),
    )


def _ingest_provenance(link: dict[str, Any] | None) -> dict[str, Any]:
    if not link:
        return {}
    return {
        "platform": link.get("ingest_platform"),
        "chat_id": link.get("source_chat_id"),
        "message_id": link.get("source_message_id"),
        "message_date": link.get("source_message_date"),
        "collector_run_id": link.get("collector_run_id"),
        "link_id": link.get("id"),
    }


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(raw_path, context.env)).expanduser().resolve()
    return context.db_path


def _required_db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | ToolResult:
    path = _db_path(context, input_data)
    if path:
        return path
    return ToolResult.failure(
        "missing_db_path",
        "Provide db_path or set MEDIAGENT_DB_PATH.",
        category=ErrorCategory.VALIDATION,
    )


def _input_urls(input_data: dict[str, Any]) -> list[str]:
    urls = []
    if input_data.get("url"):
        urls.append(str(input_data["url"]))
    urls.extend(str(url) for url in input_data.get("urls") or [] if url)
    return urls


def _link_record_from_url(url: str, *, input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = input_data.get("source") if isinstance(input_data.get("source"), dict) else {}
    return {
        "ingest_platform": input_data.get("ingest_platform") or source.get("platform") or "cli",
        "original_url": url,
        "normalized_url": normalize_url(url),
        "source_chat_id": source.get("chat_id"),
        "source_message_id": source.get("message_id"),
        "source_message_date": source.get("message_date"),
        "collector_run_id": source.get("collector_run_id") or context.run_id,
    }


def _sync_input_links(db_path: Path, input_data: dict[str, Any], *, context: ToolContext) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for url in _input_urls(input_data):
        record = _link_record_from_url(url, input_data=input_data, context=context)
        if context.dry_run:
            links.append({**record, "id": None, "is_new": None})
        else:
            db.initialize_database(db_path)
            links.append(db.upsert_link(db_path, record))
    link_ids = []
    if input_data.get("link_id") is not None:
        link_ids.append(int(input_data["link_id"]))
    link_ids.extend(int(value) for value in input_data.get("link_ids") or [])
    for link_id in link_ids:
        link = db.get_link(db_path, link_id=link_id)
        if link is not None:
            links.append(link)
    has_explicit_links = bool(links)
    if not has_explicit_links and db_path.exists():
        status = input_data.get("status") or "queued"
        if context.dry_run:
            links = db.list_ready_links(
                db_path,
                status=status,
                limit=input_data.get("limit"),
            )
        else:
            links = db.claim_links(
                db_path,
                status=status,
                limit=input_data.get("limit"),
                lease_owner=context.run_id,
                lease_seconds=int(input_data.get("lease_seconds", 900)),
            )
        if input_data.get("retry_auth_skipped"):
            remaining = _remaining_limit(input_data.get("limit"), len(links))
            if remaining != 0:
                if context.dry_run:
                    auth_links = db.list_auth_skipped_links(db_path, limit=remaining)
                else:
                    auth_links = db.claim_auth_skipped_links(
                        db_path,
                        limit=remaining,
                        lease_owner=context.run_id,
                        lease_seconds=int(input_data.get("lease_seconds", 900)),
                    )
                existing_ids = {link.get("id") for link in links}
                links.extend(
                    {**link, "_auth_retry": True}
                    for link in auth_links
                    if link.get("id") not in existing_ids
                )
    if not links:
        raise ValueError("Provide url, urls, link_id, link_ids, or queued links in db_path.")
    return links


def _limited_items(items: list[dict[str, Any]], limit: Any) -> list[dict[str, Any]]:
    if limit is None:
        return items
    return items[: max(0, int(limit))]


def _remaining_limit(limit: Any, used: int) -> int | None:
    if limit is None:
        return None
    return max(0, int(limit) - used)


def _sync_candidates(
    items: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str],
    *,
    db_path: Path,
    retry_failed: bool,
    repair_missing_files: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates = []
    summary = {
        "skipped_items": 0,
        "skipped_healthy": 0,
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
    }
    for item in items:
        status = statuses.get((item["platform"], item["remote_id"]))
        if status == "failed" and retry_failed:
            candidates.append(item)
            continue
        if status in TERMINAL_ITEM_STATUSES:
            if status == "downloaded" and repair_missing_files:
                repair = _repair_assessment(db_path, item)
                if repair["repairable"]:
                    repair_item = dict(item)
                    repair_item["_repair"] = repair
                    candidates.append(repair_item)
                    summary["repair_items"] += 1
                    summary["repair_files_missing"] += int(repair.get("missing", 0))
                    summary["repair_files_corrupt"] += int(repair.get("corrupt", 0))
                    summary["repair_files_unhealthy"] += int(repair.get("unhealthy", 0))
                    continue
                summary["skipped_healthy"] += 1
            summary["skipped_items"] += 1
            continue
        candidates.append(item)
    return candidates, summary


def _repair_assessment(db_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    records = db.list_media_files(db_path, platform=item["platform"], remote_id=item["remote_id"])
    records_by_remote = {str(record.get("remote_url") or ""): record for record in records if record.get("remote_url")}
    records_by_key = {str(record.get("file_key") or ""): record for record in records if record.get("file_key")}
    missing = 0
    corrupt = 0
    unhealthy = 0
    files: list[dict[str, Any]] = []
    for file_info in _link_item_files(item):
        remote_url = _file_remote_url(file_info)
        stable_key = _stable_file_key(file_info)
        record = (records_by_key.get(stable_key) if stable_key else None) or records_by_remote.get(remote_url)
        reason = None
        health = None
        status = None
        local_path = None
        if record is None:
            reason = "missing_record"
            missing += 1
        else:
            health = str(record.get("file_health") or "unknown")
            status = str(record.get("status") or "")
            local_path = record.get("local_path")
            if record.get("library_state") == "removed":
                pass
            elif status == "skipped" and health == IGNORED_COMIC_SPACER_HEALTH:
                pass
            elif health == "corrupt":
                reason = "corrupt_file"
                corrupt += 1
            elif health == "missing":
                reason = "missing_file"
                missing += 1
            elif status and status != "downloaded":
                reason = "unhealthy_status"
                unhealthy += 1
            elif status == "downloaded" and local_path and not Path(str(local_path)).exists():
                reason = "downloaded_file_missing_on_disk"
                missing += 1
            elif health not in {"valid", "unknown"}:
                reason = "unhealthy_file"
                unhealthy += 1
        if reason:
            files.append(
                {
                    "remote_url": remote_url,
                    "part": file_info.get("part"),
                    "reason": reason,
                    "status": status,
                    "file_health": health,
                    "local_path": local_path,
                }
            )
    return {
        "repairable": bool(files),
        "missing": missing,
        "corrupt": corrupt,
        "unhealthy": unhealthy,
        "files": files,
    }


def _dedupe_media_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("platform")), str(item.get("remote_id")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _public_media_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: sanitize_link_resolution_for_output(value)
        for key, value in item.items()
        if not str(key).startswith("_")
    }


async def _sync_one_link_item(
    context: ToolContext,
    db_path: Path,
    item: dict[str, Any],
    input_data: dict[str, Any],
) -> dict[str, Any]:
    platform = item["platform"]
    remote_id = item["remote_id"]
    files = _link_item_files(item)
    result = {
        "platform": platform,
        "remote_id": remote_id,
        "status": "queued",
        "files_total": len(files),
        "files_downloaded": 0,
        "files_skipped": 0,
        "files_deduplicated": 0,
        "dedup_bytes_reclaimed": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "artifacts": [],
        "warnings": [],
        "errors": [],
    }
    if not files:
        db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status="failed")
        result["status"] = "failed"
        result["errors"].append({"code": "missing_files", "message": "Resolved link item has no downloadable files."})
        return result
    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status="downloading")
    overwrite = input_data.get("overwrite", False)
    write_sidecar_metadata = input_data.get("write_sidecar_metadata", False)
    for file_info in files:
        try:
            target_dir, include_platform_layer = _target_dir_for_item(context, input_data, item)
            ensure_inside(target_dir, context.allowed_write_roots())
            plan = plan_storage_path(
                library_root=target_dir,
                item=item,
                file_info=file_info,
                include_platform_layer=include_platform_layer,
            )
            target_path = plan.final_path
            ensure_inside(target_path, [target_dir])
        except PathSafetyError as exc:
            target_path = context.data_dir / "unsafe" if context.data_dir else Path("unsafe")
            _record_failed_link_file(db_path, item, file_info, target_path, "unsafe_path", str(exc))
            result["files_failed"] += 1
            result["errors"].append(_file_error(file_info, "unsafe_path", str(exc)))
            continue
        if overwrite:
            try:
                library_content.ensure_target_write_safe(db_path, target_path)
            except ValueError as exc:
                result["files_failed"] += 1
                result["errors"].append(
                    _file_error(file_info, "shared_content_write_conflict", str(exc))
                )
                continue
        if target_path.exists() and not overwrite:
            if not _path_known_to_item(db_path, item, file_info, target_path):
                _record_failed_link_file(
                    db_path,
                    item,
                    file_info,
                    target_path,
                    "target_conflict",
                    "Target file already exists but is not known to Mediagent.",
                )
                result["files_failed"] += 1
                result["errors"].append(
                    _file_error(file_info, "target_conflict", "Target file already exists but is not known to Mediagent.")
                )
                continue
            file_record = _existing_file_record(db_path, item, file_info, plan)
            final_target = Path(str(file_record.get("local_path") or target_path))
            result["files_downloaded"] += 1
            result["bytes_written"] += file_record.get("size_bytes") or 0
            result["artifacts"].append(str(final_target))
            continue
        download_result = _download_file_safely(
            context,
            input_data,
            url=_file_download_url(file_info),
            headers=_file_download_headers(file_info),
            target_path=target_path,
            overwrite=overwrite,
            expected_mime_prefix=_expected_mime_prefix(file_info),
            content_transform=_file_content_transform(file_info),
        )
        if download_result.is_success:
            if download_result.data.get("skipped"):
                db.upsert_media_file(
                    db_path,
                    platform=platform,
                    remote_id=remote_id,
                    remote_url=_file_remote_url(file_info),
                    local_path=None,
                    mime_type=download_result.data.get("mime_type") or file_info.get("mime_type"),
                    size_bytes=download_result.data.get("size_bytes"),
                    checksum=download_result.data.get("checksum"),
                    status="skipped",
                    library_relative_path=None,
                    storage_layout=plan.layout,
                    file_health=IGNORED_COMIC_SPACER_HEALTH,
                    source_timestamp=plan.source_timestamp,
                    verified_at=datetime.now(UTC).isoformat(),
                    file_key=_stable_file_key(file_info),
                )
                result["files_skipped"] += 1
                result["warnings"].append(
                    f"Ignored non-content JMComic spacer at page {int(file_info.get('page', 0)) + 1}."
                )
                continue
            file_record = db.upsert_media_file(
                db_path,
                platform=platform,
                remote_id=remote_id,
                remote_url=_file_remote_url(file_info),
                local_path=download_result.data["target_path"],
                mime_type=download_result.data.get("mime_type") or file_info.get("mime_type"),
                size_bytes=download_result.data.get("size_bytes"),
                checksum=download_result.data.get("checksum"),
                status="downloaded",
                library_relative_path=plan.relative_path.as_posix(),
                storage_layout=plan.layout,
                file_health="valid",
                source_timestamp=plan.source_timestamp,
                verified_at=datetime.now(UTC).isoformat(),
                file_key=_stable_file_key(file_info),
            )
            adoption = library_content.adopt_media_file(db_path, file_id=int(file_record["id"]))
            final_target = Path(str(adoption.get("target_path") or download_result.data["target_path"]))
            if adoption.get("deduplicated"):
                result["files_deduplicated"] += 1
                result["dedup_bytes_reclaimed"] += int(adoption.get("bytes_reclaimed") or 0)
            result["files_downloaded"] += 1
            result["bytes_written"] += download_result.data.get("size_bytes") or 0
            result["artifacts"].append(str(final_target))
            if write_sidecar_metadata and adoption.get("deduplicated"):
                result["warnings"].append("Skipped source-specific sidecar for globally deduplicated content.")
            elif write_sidecar_metadata:
                metadata_result = await _write_sidecar_metadata(
                    context,
                    item=item,
                    file_info=file_info,
                    target_path=final_target,
                    overwrite=overwrite,
                )
                result["warnings"].extend(metadata_result["warnings"])
                result["errors"].extend(metadata_result["errors"])
                result["artifacts"].extend(metadata_result["artifacts"])
            continue
        _record_failed_link_file(
            db_path,
            item,
            file_info,
            target_path,
            download_result.error.code if download_result.error else "download_failed",
            download_result.error.message if download_result.error else "Download failed.",
        )
        result["files_failed"] += 1
        result["errors"].append(
            _file_error(
                file_info,
                download_result.error.code if download_result.error else "download_failed",
                download_result.error.message if download_result.error else "Download failed.",
            )
        )
    result["status"] = item_status_from_file_counts(
        total=result["files_total"],
        downloaded=result["files_downloaded"],
        failed=result["files_failed"],
        skipped=result["files_skipped"],
    )
    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status=result["status"])
    return result


def _download_file_safely(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    url: str,
    headers: dict[str, str] | None = None,
    target_path: Path,
    overwrite: bool,
    expected_mime_prefix: str | None,
    content_transform: Callable[[bytes], bytes | _ContentTransformOutcome] | None = None,
) -> ToolResult:
    partial_path = target_path.with_name(target_path.name + ".partial")
    try:
        ensure_inside(target_path, context.allowed_write_roots())
        ensure_inside(partial_path, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if target_path.exists() and not overwrite:
        return ToolResult.failure(
            "target_exists",
            "Target file already exists and overwrite is false.",
            details={"target_path": str(target_path)},
            category=ErrorCategory.VALIDATION,
        )
    policy = _link_safety_policy(input_data)
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
    )
    try:
        response, final_url = fetch_limited_follow_redirects(
            url,
            request=request,
            max_bytes=policy.max_media_bytes + 1,
            headers=headers,
        )
    except URLSafetyError as exc:
        return ToolResult.failure(
            "unsafe_url",
            str(exc),
            details={"reason": exc.reason, **exc.details},
            category=ErrorCategory.NETWORK,
        )
    except Exception as exc:
        return ToolResult.failure(
            "download_failed",
            "Download failed during safe link GET.",
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.NETWORK,
        )
    if response.status_code in (401, 403):
        return ToolResult.failure(
            "requires_auth",
            "Download URL requires authentication.",
            details={"status_code": response.status_code},
            category=ErrorCategory.AUTH,
        )
    if not 200 <= response.status_code < 300:
        return ToolResult.failure(
            "download_failed",
            "Download failed during safe link GET.",
            details={"status_code": response.status_code},
            category=ErrorCategory.NETWORK,
        )
    content_length = int_header(response.headers, "content-length")
    if content_length is not None and content_length > policy.max_media_bytes:
        return ToolResult.failure(
            "too_large",
            "Download Content-Length exceeds the configured link media limit.",
            details={"size_bytes": content_length, "max_media_bytes": policy.max_media_bytes},
            category=ErrorCategory.NETWORK,
        )
    if len(response.content) > policy.max_media_bytes:
        return ToolResult.failure(
            "too_large",
            "Download body exceeds the configured link media limit.",
            details={"bytes_read": len(response.content), "max_media_bytes": policy.max_media_bytes},
            category=ErrorCategory.NETWORK,
        )
    mime_type = clean_mime(header_value(response.headers, "content-type"))
    final_url_suffix = Path(urlparse(final_url).path).suffix.lower()
    if mime_type not in ALLOWED_MEDIA_MIME_TYPES and final_url_suffix == ".mov":
        mime_type = "video/quicktime"
    if mime_type not in ALLOWED_MEDIA_MIME_TYPES:
        return ToolResult.failure(
            "unsupported_media_type",
            "Download Content-Type is not an allowed media MIME type.",
            details={"mime_type": mime_type},
            category=ErrorCategory.NETWORK,
        )
    if expected_mime_prefix and not mime_type.startswith(expected_mime_prefix):
        return ToolResult.failure(
            "download_validation_failed",
            f"Content type does not start with {expected_mime_prefix!r}.",
            details={"mime_type": mime_type},
            category=ErrorCategory.NETWORK,
        )
    content = response.content
    if content_transform is not None:
        try:
            transformed = content_transform(content)
        except Exception as exc:
            return ToolResult.failure(
                "download_transform_failed",
                "Downloaded media could not be decoded.",
                details={"exception_type": type(exc).__name__},
                category=ErrorCategory.NETWORK,
            )
        if isinstance(transformed, _ContentTransformOutcome):
            if transformed.skip_reason:
                checksum = hashlib.sha256(content).hexdigest()
                return ToolResult.success(
                    {
                        "url": url,
                        "final_url": final_url,
                        "skipped": True,
                        "skip_reason": transformed.skip_reason,
                        "size_bytes": len(content),
                        "checksum": f"sha256:{checksum}",
                        "mime_type": mime_type,
                    }
                )
            if transformed.content is None:
                return ToolResult.failure(
                    "download_transform_failed",
                    "Downloaded media transform returned no content.",
                    category=ErrorCategory.NETWORK,
                )
            content = transformed.content
        else:
            content = transformed
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if partial_path.exists():
            partial_path.unlink()
        partial_path.write_bytes(content)
        checksum, size_bytes = _hash_file(partial_path)
        partial_path.replace(target_path)
    except Exception:
        _remove_partial(partial_path)
        raise
    return ToolResult.success(
        {
            "url": url,
            "final_url": final_url,
            "target_path": str(target_path),
            "partial_path": str(partial_path),
            "finalized": True,
            "size_bytes": size_bytes,
            "checksum": f"sha256:{checksum}",
            "mime_type": mime_type,
        },
        artifacts=[{"type": "file", "path": str(target_path)}],
    )


def _target_dir_for_item(
    context: ToolContext,
    input_data: dict[str, Any],
    item: dict[str, Any],
) -> tuple[Path, bool]:
    raw_path = input_data.get("library_root") or input_data.get("target_dir")
    if raw_path:
        return (
            normalize_path(str(raw_path), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", True),
        )
    platform_root = context.env.get(platform_library_env_name(item.get("platform")))
    if platform_root:
        return (
            normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", False),
        )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        input_data.get("include_platform_layer", True),
    )


def _planned_downloads(
    context: ToolContext,
    input_data: dict[str, Any],
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    planned = []
    for file_info in _link_item_files(item):
        target_dir, include_platform_layer = _target_dir_for_item(context, input_data, item)
        plan = plan_storage_path(
            library_root=target_dir,
            item=item,
            file_info=file_info,
            include_platform_layer=include_platform_layer,
        )
        planned.append(
            {
                "platform": item["platform"],
                "remote_id": item["remote_id"],
                "kind": file_info.get("kind"),
                "page": file_info.get("page", 0),
                "relative_path": plan.relative_path.as_posix(),
                "target_path": str(plan.final_path),
                "partial_path": str(plan.partial_path),
                "layout": plan.layout,
                "date_source": plan.date_source,
            }
        )
    return planned


def _link_item_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_files = (item.get("_runtime") or {}).get("files")
    if isinstance(runtime_files, list) and runtime_files:
        return [
            file_info
            for file_info in runtime_files
            if isinstance(file_info, dict)
            and (file_info.get("url") or file_info.get("remote_url"))
            and (file_info.get("download_context") or file_info.get("runtime_headers") or file_info.get("url"))
        ]
    files = (item.get("metadata") or {}).get("files", [])
    return [
        file_info
        for file_info in files
        if isinstance(file_info, dict) and (file_info.get("url") or file_info.get("remote_url"))
    ]


def _file_download_url(file_info: dict[str, Any]) -> str:
    download_context = file_info.get("download_context")
    if isinstance(download_context, dict) and download_context.get("url"):
        return str(download_context["url"])
    return _file_remote_url(file_info)


def _file_download_headers(file_info: dict[str, Any]) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    download_context = file_info.get("download_context")
    if isinstance(download_context, dict) and isinstance(download_context.get("headers"), dict):
        headers.update({str(key): str(value) for key, value in download_context["headers"].items()})
    if isinstance(file_info.get("runtime_headers"), dict):
        headers.update({str(key): str(value) for key, value in file_info["runtime_headers"].items()})
    return headers or None


def _file_remote_url(file_info: dict[str, Any]) -> str:
    return str(file_info.get("remote_url") or file_info.get("url") or "")


def _stable_file_key(file_info: dict[str, Any]) -> str | None:
    value = (
        file_info.get("file_key")
        or file_info.get("content_identity")
        or file_info.get("stable_url")
    )
    text = str(value or "").strip()
    if text:
        return text
    if file_info.get("storage_category") == "comic-pages":
        try:
            page_number = int(file_info.get("page_number") or int(file_info.get("page", 0)) + 1)
        except (TypeError, ValueError):
            return None
        return f"page:{page_number:06d}"
    return None


def _file_content_transform(
    file_info: dict[str, Any],
) -> Callable[[bytes], bytes | _ContentTransformOutcome] | None:
    runtime = file_info.get("runtime_decode")
    if not isinstance(runtime, dict) or runtime.get("provider") != "jmcomic":
        return None
    try:
        segments = int(runtime.get("vertical_segments") or 0)
    except (TypeError, ValueError):
        segments = 0
    if segments <= 0:
        return None
    from mediagent.platforms.jmcomic.images import is_non_content_spacer, restore_vertical_slices

    def transform(content: bytes) -> bytes | _ContentTransformOutcome:
        if is_non_content_spacer(content, segment_count=segments):
            return _ContentTransformOutcome(content=None, skip_reason="jmcomic_non_content_spacer")
        return restore_vertical_slices(content, segment_count=segments)

    return transform


def _existing_file_record(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    target_path = plan.final_path
    content = target_path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    record = db.upsert_media_file(
        db_path,
        platform=item["platform"],
        remote_id=item["remote_id"],
        remote_url=_file_remote_url(file_info),
        local_path=str(target_path),
        mime_type=file_info.get("mime_type"),
        size_bytes=len(content),
        checksum=f"sha256:{checksum}",
        status="downloaded",
        library_relative_path=plan.relative_path.as_posix(),
        storage_layout=plan.layout,
        file_health="valid",
        source_timestamp=plan.source_timestamp,
        verified_at=datetime.now(UTC).isoformat(),
        file_key=_stable_file_key(file_info),
    )
    adoption = library_content.adopt_media_file(db_path, file_id=int(record["id"]))
    if adoption.get("adopted"):
        record["local_path"] = adoption.get("target_path")
        record["library_relative_path"] = adoption.get("library_relative_path")
        record["library_entry_id"] = adoption.get("entry_id")
        record["deduplicated"] = adoption.get("deduplicated", False)
    return record


def _path_known_to_item(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
) -> bool:
    if not db_path.exists():
        return False
    stable_key = _stable_file_key(file_info)
    with db.connect(db_path) as connection:
        identity_sql = "mf.file_key = ?" if stable_key else "mf.remote_url = ?"
        identity = stable_key or _file_remote_url(file_info)
        row = connection.execute(
            f"""
            SELECT mf.id
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            WHERE mi.platform = ?
              AND mi.remote_id = ?
              AND mf.local_path = ?
              AND {identity_sql}
            """,
            (
                item["platform"],
                item["remote_id"],
                str(target_path),
                identity,
            ),
        ).fetchone()
    return row is not None


def _record_failed_link_file(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
    code: str,
    message: str,
) -> None:
    db.upsert_media_file(
        db_path,
        platform=item["platform"],
        remote_id=item["remote_id"],
        remote_url=_file_remote_url(file_info),
        local_path=str(target_path),
        mime_type=file_info.get("mime_type"),
        size_bytes=None,
        checksum=None,
        status="failed",
        file_health="unknown",
        file_key=_stable_file_key(file_info),
    )


async def _write_sidecar_metadata(
    context: ToolContext,
    *,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
    overwrite: bool,
) -> dict[str, list[Any]]:
    metadata_path = target_path.with_suffix(".json")
    result = await metadata_write(
        context,
        {
            "target_path": str(metadata_path),
            "overwrite": overwrite,
            "metadata": {
                "platform": item["platform"],
                "remote_id": item.get("remote_id"),
                "source_url": item.get("source_url"),
                "author_id": item.get("author_id"),
                "author_name": item.get("author_name"),
                "media_type": item.get("media_type"),
                "item_metadata": sanitize_link_resolution_for_output(item.get("metadata") or {}),
                "file": sanitize_link_resolution_for_output(file_info),
            },
        },
    )
    if result.is_success:
        return {"artifacts": [artifact["path"] for artifact in result.artifacts], "warnings": [], "errors": []}
    if result.error and result.error.code == "target_exists":
        return {"artifacts": [], "warnings": [f"Metadata already exists: {metadata_path}"], "errors": []}
    return {
        "artifacts": [],
        "warnings": [],
        "errors": [
            {
                "code": result.error.code if result.error else "metadata_write_failed",
                "message": result.error.message if result.error else "Metadata write failed.",
                "target_path": str(metadata_path),
            }
        ],
    }


def _expected_mime_prefix(file_info: dict[str, Any]) -> str | None:
    if file_info.get("kind") == "ugoira_zip":
        return None
    if file_info.get("kind") == "image":
        return "image/"
    media_type = file_info.get("media_type")
    if media_type == "photo":
        return "image/"
    if media_type == "video":
        return "video/"
    if media_type == "audio":
        return "audio/"
    return None


def _file_error(file_info: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "url": _file_remote_url(file_info),
        "kind": file_info.get("kind"),
        "page": file_info.get("page", 0),
    }


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size_bytes += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def _remove_partial(partial_path: Path) -> None:
    try:
        if partial_path.exists():
            partial_path.unlink()
    except FileNotFoundError:
        pass


def _link_ingest_provenance(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": link.get("ingest_platform"),
        "chat_id": link.get("source_chat_id"),
        "message_id": link.get("source_message_id"),
        "message_date": link.get("source_message_date"),
        "collector_run_id": link.get("collector_run_id"),
        "link_id": link.get("id"),
    }


def _safe_link_record(link: dict[str, Any]) -> dict[str, Any]:
    return {
        key: link.get(key)
        for key in (
            "id",
            "ingest_platform",
            "original_url",
            "normalized_url",
            "canonical_url",
            "source_chat_id",
            "source_message_id",
            "source_message_date",
            "collector_run_id",
            "status",
            "skip_reason",
            "attempt_count",
            "max_attempts",
            "last_error_code",
            "next_attempt_at",
            "retryable",
            "lease_owner",
            "lease_expires_at",
            "is_new",
            "previous_status",
        )
    }
