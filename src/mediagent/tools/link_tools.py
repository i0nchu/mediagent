"""Undocumented experimental link resolver tools."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mediagent.core import db
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
)
from mediagent.core.storage import default_library_root, plan_storage_path, platform_library_env_name
from mediagent.core.sync import TERMINAL_ITEM_STATUSES, item_status_from_file_counts
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec
from mediagent.tools.metadata_tools import metadata_write


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
    return ToolResult.success(
        {
            "resolution": resolution,
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
                "resolution": resolution,
                "link": link,
            }
        )
    return ToolResult.success(
        {
            "converted": True,
            "item": item,
            "resolution": resolution,
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
    policy = _link_safety_policy(input_data)
    request = ResolveRequest(http_client=context.http_client, policy=policy)
    resolutions: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    summary = {
        "links_considered": len(links),
        "resolved": 0,
        "skipped_links": 0,
        "queued": 0,
        "skipped_items": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
    }
    warnings: list[str] = []
    for link in links:
        resolution = default_link_resolver_registry().resolve(link["original_url"], request=request)
        resolutions.append({"link": _safe_link_record(link), "resolution": resolution})
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

    if context.dry_run:
        planned_downloads = []
        for item in resolved_items:
            planned_downloads.extend(_planned_downloads(context, input_data, item))
        summary["queued"] = len(resolved_items)
        return ToolResult.success(
            {
                "db_path": str(db_path),
                "summary": summary,
                "links": resolutions,
                "planned_downloads": planned_downloads,
            },
            warnings=warnings,
        )

    db.initialize_database(db_path)
    for item in resolved_items:
        db.upsert_media_item(db_path, item)
    statuses = db.get_media_statuses(db_path, resolved_items)
    items_to_sync, skipped_items = _sync_candidates(
        resolved_items,
        statuses,
        retry_failed=input_data.get("retry_failed", False),
    )
    summary["queued"] = len(items_to_sync)
    summary["skipped_items"] = skipped_items

    item_results: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    for item in items_to_sync:
        result = await _sync_one_link_item(context, db_path, item, input_data)
        item_results.append(result)
        summary[result["status"]] += 1
        summary["files_downloaded"] += result["files_downloaded"]
        summary["files_failed"] += result["files_failed"]
        summary["bytes_written"] += result["bytes_written"]
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])

    run_status = "success"
    if summary["failed"] or summary["partial"]:
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


def _resolve_url(context: ToolContext, raw_url: str, input_data: dict[str, Any]) -> dict[str, Any]:
    policy = LinkSafetyPolicy(
        max_redirects=max(0, int(input_data.get("max_redirects", 3))),
        timeout_seconds=float(input_data.get("timeout_seconds", 30.0)),
        max_html_bytes=max(1, int(input_data.get("max_html_bytes", 1024 * 1024))),
        max_media_bytes=max(1, int(input_data.get("max_media_bytes", 1024 * 1024 * 1024))),
    )
    request = ResolveRequest(http_client=context.http_client, policy=policy)
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
    if not links and db_path.exists():
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
    if not links:
        raise ValueError("Provide url, urls, link_id, link_ids, or queued links in db_path.")
    return links


def _limited_items(items: list[dict[str, Any]], limit: Any) -> list[dict[str, Any]]:
    if limit is None:
        return items
    return items[: max(0, int(limit))]


def _sync_candidates(
    items: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str],
    *,
    retry_failed: bool,
) -> tuple[list[dict[str, Any]], int]:
    candidates = []
    skipped = 0
    for item in items:
        status = statuses.get((item["platform"], item["remote_id"]))
        if status == "failed" and retry_failed:
            candidates.append(item)
            continue
        if status in TERMINAL_ITEM_STATUSES:
            skipped += 1
            continue
        candidates.append(item)
    return candidates, skipped


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
            result["files_downloaded"] += 1
            result["bytes_written"] += file_record.get("size_bytes") or 0
            result["artifacts"].append(str(target_path))
            continue
        download_result = _download_file_safely(
            context,
            input_data,
            url=_file_remote_url(file_info),
            target_path=target_path,
            overwrite=overwrite,
            expected_mime_prefix=_expected_mime_prefix(file_info),
        )
        if download_result.is_success:
            db.upsert_media_file(
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
            )
            result["files_downloaded"] += 1
            result["bytes_written"] += download_result.data.get("size_bytes") or 0
            result["artifacts"].append(download_result.data["target_path"])
            if write_sidecar_metadata:
                metadata_result = await _write_sidecar_metadata(
                    context,
                    item=item,
                    file_info=file_info,
                    target_path=target_path,
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
    )
    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status=result["status"])
    return result


def _download_file_safely(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    url: str,
    target_path: Path,
    overwrite: bool,
    expected_mime_prefix: str | None,
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
    request = ResolveRequest(http_client=context.http_client, policy=policy)
    try:
        response, final_url = fetch_limited_follow_redirects(
            url,
            request=request,
            max_bytes=policy.max_media_bytes + 1,
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
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if partial_path.exists():
            partial_path.unlink()
        partial_path.write_bytes(response.content)
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
    files = (item.get("metadata") or {}).get("files", [])
    return [
        file_info
        for file_info in files
        if isinstance(file_info, dict) and (file_info.get("url") or file_info.get("remote_url"))
    ]


def _file_remote_url(file_info: dict[str, Any]) -> str:
    return str(file_info.get("remote_url") or file_info.get("url") or "")


def _existing_file_record(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    target_path = plan.final_path
    content = target_path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    return db.upsert_media_file(
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
    )


def _path_known_to_item(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
) -> bool:
    if not db_path.exists():
        return False
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT mf.id
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            WHERE mi.platform = ?
              AND mi.remote_id = ?
              AND mf.local_path = ?
              AND mf.remote_url = ?
            """,
            (
                item["platform"],
                item["remote_id"],
                str(target_path),
                _file_remote_url(file_info),
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
                "item_metadata": item.get("metadata") or {},
                "file": file_info,
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
