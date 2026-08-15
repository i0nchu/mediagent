"""Comic-only link and favorite synchronization tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.storage import default_library_root
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.platforms.jmcomic import auth as jm_auth
from mediagent.platforms.jmcomic.client import JMComicApiTransport, JMComicClient, JMComicClientError
from mediagent.platforms.jmcomic.links import JMComicLinkError, parse_jmcomic_link
from mediagent.platforms.nhentai import auth as nh_auth
from mediagent.platforms.nhentai import client as nh_client
from mediagent.platforms.nhentai.links import parse_gallery_link
from mediagent.tools import link_tools
from mediagent.tools.pixiv_library_tools import (
    _apply_comic_package,
    _comic_package_plan,
    _public_package_plan,
)


def definitions() -> list[ToolDefinition]:
    common_sync_properties = {
        "db_path": {"type": "string"},
        "library_root": {"type": "string"},
        "include_platform_layer": {"type": "boolean"},
        "download_limit": {"type": "integer"},
        "overwrite": {"type": "boolean"},
        "retry_failed": {"type": "boolean"},
        "repair_missing_files": {"type": "boolean"},
        "timeout_seconds": {"type": "number"},
        "max_media_bytes": {"type": "integer"},
    }
    sync_permissions = (
        Permission.READ_ENV,
        Permission.READ_CREDENTIALS,
        Permission.WRITE_CREDENTIALS,
        Permission.NETWORK,
        Permission.READ_DB,
        Permission.WRITE_DB,
        Permission.READ_FILES,
        Permission.WRITE_FILES,
    )
    collect_permissions = (
        Permission.READ_ENV,
        Permission.READ_CREDENTIALS,
        Permission.WRITE_CREDENTIALS,
        Permission.NETWORK,
    )
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="comic.link.sync",
                description="Download explicit nhentai or JMComic links exactly as linked and package complete chapters as CBZ.",
                input_schema={
                    "type": "object",
                    "required_any": [["url", "urls"]],
                    "properties": {
                        **common_sync_properties,
                        "url": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                    },
                },
                output_schema={"type": "object"},
                permissions=sync_permissions,
                dry_run_supported=True,
            ),
            handler=comic_link_sync,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="nhentai.auth.status",
                description="Inspect local nhentai session configuration without claiming remote authentication.",
                input_schema={"type": "object", "properties": {"session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=nhentai_auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="nhentai.auth.refresh",
                description="Refresh and atomically persist a reusable nhentai browser session.",
                input_schema={"type": "object", "properties": {"session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=False,
            ),
            handler=nhentai_auth_refresh,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="nhentai.favorites.collect",
                description="Validate nhentai authentication and collect one complete favorites snapshot without downloading.",
                input_schema={
                    "type": "object",
                    "properties": {"session_file": {"type": "string"}, "timeout_seconds": {"type": "number"}},
                },
                output_schema={"type": "object"},
                permissions=collect_permissions,
                dry_run_supported=True,
            ),
            handler=nhentai_favorites_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="nhentai.favorites.sync",
                description="Treat the complete nhentai favorites list as an inbox and sync each gallery exactly once.",
                input_schema={"type": "object", "properties": {**common_sync_properties, "session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=sync_permissions,
                dry_run_supported=True,
            ),
            handler=nhentai_favorites_sync,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.auth.status",
                description="Inspect local JMComic credential/session configuration without claiming remote authentication.",
                input_schema={"type": "object", "properties": {"session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=jmcomic_auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.auth.login",
                description="Log in with configured JMComic credentials and atomically persist the reusable session.",
                input_schema={"type": "object", "properties": {"session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=False,
            ),
            handler=jmcomic_auth_login,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.collect",
                description="Validate JMComic authentication and collect one complete favorites snapshot without downloading.",
                input_schema={
                    "type": "object",
                    "properties": {"session_file": {"type": "string"}, "timeout_seconds": {"type": "number"}},
                },
                output_schema={"type": "object"},
                permissions=collect_permissions,
                dry_run_supported=True,
            ),
            handler=jmcomic_favorites_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.sync",
                description="Treat the complete JMComic album favorites list as an inbox and follow active albums for new chapters.",
                input_schema={"type": "object", "properties": {**common_sync_properties, "session_file": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=sync_permissions,
                dry_run_supported=True,
            ),
            handler=jmcomic_favorites_sync,
        ),
    ]


async def comic_link_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    urls = _input_urls(input_data)
    if not urls:
        return ToolResult.failure("missing_url", "Provide url or urls.", category=ErrorCategory.VALIDATION)
    path_error = _validate_sync_paths(context, input_data)
    if path_error is not None:
        return path_error
    try:
        items: list[dict[str, Any]] = []
        targets = []
        jm_client: JMComicClient | None = None
        nh_session = _optional_nh_session(context, input_data)
        ingest_provenance = input_data.get("_ingest_provenance")
        for url in urls:
            nh_link = parse_gallery_link(url)
            if nh_link is not None:
                resolved = nh_client.resolve_exact(
                    url,
                    http_client=context.http_client,
                    session=nh_session,
                    timeout=float(input_data.get("timeout_seconds", 30.0)),
                )
                _apply_ingest_provenance(resolved, ingest_provenance)
                items.extend(resolved)
                targets.append({"provider": "nhentai", "target": f"gallery:{nh_link.gallery_id}", "policy": "exact"})
                continue
            try:
                jm_link = parse_jmcomic_link(url)
            except JMComicLinkError as exc:
                raise ValueError("Only nhentai gallery and JMComic album/photo/cover links are supported.") from exc
            if jm_client is None:
                jm_client = _jm_client(context, input_data, login_if_needed=False)
            resolution = jm_client.resolve_exact(url)
            resolved = resolution.normalized_items()
            _apply_ingest_provenance(resolved, ingest_provenance)
            items.extend(resolved)
            targets.append({"provider": "jmcomic", "target": jm_link.provider_work_id, "policy": "exact"})
        result = await _sync_items(context, input_data, items)
        result.data["targets"] = targets
        result.data["policy"] = "exact"
        if jm_client is not None and not context.dry_run:
            _persist_jm_session_if_configured(context, input_data, jm_client)
        return result
    except Exception as exc:
        return _provider_failure(exc)


def comic_link_provider(url: str) -> str | None:
    """Return the dedicated comic provider for a supported exact link."""

    if parse_gallery_link(url) is not None:
        return "nhentai"
    try:
        parse_jmcomic_link(url)
    except JMComicLinkError:
        return None
    return "jmcomic"


def _apply_ingest_provenance(
    items: list[dict[str, Any]],
    provenance: Any,
) -> None:
    if not isinstance(provenance, dict) or not provenance:
        return
    public_provenance = {
        key: provenance.get(key)
        for key in ("platform", "chat_id", "message_id", "message_date", "collector_run_id", "link_id")
        if provenance.get(key) is not None
    }
    if not public_provenance:
        return
    for item in items:
        metadata = dict(item.get("metadata") or {})
        metadata["ingested_from"] = public_provenance
        metadata["source_provenance"] = [public_provenance]
        item["metadata"] = metadata


async def nhentai_auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path = nh_auth.session_file_path(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    if path is None:
        return ToolResult.success(
            {
                "provider": "nhentai",
                "auth_status": "unconfigured",
                "configured": False,
                "session_present": False,
                "reusable": False,
                "authenticated": False,
                "remote_verified": False,
            }
        )
    if not path.exists():
        return ToolResult.success(
            {
                "provider": "nhentai",
                "auth_status": "session_missing",
                "configured": True,
                "session_present": False,
                "reusable": False,
                "authenticated": False,
                "remote_verified": False,
                "session_file": str(path),
            }
        )
    try:
        nh_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    except nh_auth.NhentaiAuthError as exc:
        return ToolResult.failure(exc.code, str(exc), category=ErrorCategory.AUTH)
    return ToolResult.success(
        {
            "provider": "nhentai",
            "auth_status": "session_available_unverified",
            "configured": True,
            "session_present": True,
            "reusable": True,
            "authenticated": None,
            "remote_verified": False,
            "session_file": str(path),
        }
    )


async def nhentai_auth_refresh(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        data = nh_auth.refresh_saved_session(
            http_client=context.http_client,
            env=context.env,
            cwd=context.cwd,
            session_file=input_data.get("session_file"),
        )
    except nh_auth.NhentaiAuthError as exc:
        if exc.code != "nhentai_auth_required":
            return _provider_failure(exc)
        try:
            session = nh_auth.load_session(
                env=context.env,
                cwd=context.cwd,
                session_file=input_data.get("session_file"),
            )
            nh_client.get_favorites_page(
                page=1,
                http_client=context.http_client,
                session=session,
            )
        except Exception as validation_exc:
            return _provider_failure(validation_exc)
        return ToolResult.failure(
            "nhentai_refresh_rejected",
            "nhentai rejected session refresh; the current session remains usable but was not extended.",
            data={
                "provider": "nhentai",
                "operation": "refresh",
                "operation_status": "failed",
                "rotated": False,
                "current_auth_usable": True,
                "verification": "favorites_access_succeeded",
                "write_performed": False,
            },
            warnings=[
                "nhentai did not rotate the imported browser session, but authenticated favorites access remains usable."
            ],
            category=ErrorCategory.AUTH,
        )
    except Exception as exc:
        return _provider_failure(exc)
    return ToolResult.success({"provider": "nhentai", **data})


async def nhentai_favorites_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path_error = _validate_sync_paths(context, input_data)
    if path_error is not None:
        return path_error
    try:
        session = nh_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
        try:
            collection = nh_client.collect_favorites(
                http_client=context.http_client,
                session=session,
                timeout=float(input_data.get("timeout_seconds", 30.0)),
            )
        except nh_client.NhentaiApiError as exc:
            if exc.code != "nhentai_auth_required" or context.dry_run:
                raise
            session = nh_auth.refresh_session(
                http_client=context.http_client,
                session=session,
                timeout=float(input_data.get("timeout_seconds", 30.0)),
            )
            if not context.dry_run:
                nh_auth.save_session(
                    session,
                    env=context.env,
                    cwd=context.cwd,
                    session_file=input_data.get("session_file"),
                )
            collection = nh_client.collect_favorites(
                http_client=context.http_client,
                session=session,
                timeout=float(input_data.get("timeout_seconds", 30.0)),
            )
        if not collection.get("complete"):
            raise ValueError("nhentai favorites snapshot is incomplete; previous membership was preserved.")
        targets = [
            {"target_type": "gallery", "target_id": str(target["provider_work_id"]).split(":", 1)[1], "metadata": target}
            for target in collection["targets"]
        ]
        snapshot = _commit_or_preview_snapshot(
            context,
            input_data,
            "nhentai",
            _favorites_collection_key(session.get("account_id")),
            targets,
            "exact",
        )
        selected = _limit(targets, input_data.get("download_limit"))
        result = await _sync_favorite_targets(
            context,
            input_data,
            selected,
            lambda target: nh_client.resolve_exact(
                    f"https://nhentai.net/g/{target['target_id']}/",
                    http_client=context.http_client,
                    session=session,
                    timeout=float(input_data.get("timeout_seconds", 30.0)),
                ),
        )
        result.data.update({"collection": "favorites", "target_policy": "exact", "snapshot": snapshot, "favorites_seen": len(targets)})
        return result
    except Exception as exc:
        return _provider_failure(exc)


async def nhentai_favorites_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        session = nh_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
        collection = nh_client.collect_favorites(
            http_client=context.http_client,
            session=session,
            timeout=float(input_data.get("timeout_seconds", 30.0)),
        )
        if not collection.get("complete"):
            raise ValueError("nhentai favorites snapshot is incomplete.")
        return ToolResult.success(
            {
                "provider": "nhentai",
                "collection": "favorites",
                "complete": True,
                "pages_fetched": int(collection.get("pages_fetched") or 0),
                "expected_total": collection.get("expected_total"),
                "favorites_seen": len(collection.get("targets") or []),
                "target_policy": "exact",
            }
        )
    except Exception as exc:
        return _provider_failure(exc)


async def jmcomic_auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    config = jm_auth.load_config(env=context.env, cwd=context.cwd)
    session_path = jm_auth.session_file_path(
        env=context.env,
        cwd=context.cwd,
        session_file=input_data.get("session_file"),
    )
    try:
        session = jm_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    except jm_auth.JMComicAuthError as exc:
        return ToolResult.failure(exc.code, str(exc), category=ErrorCategory.AUTH)
    credentials_configured = bool(config.username and config.password)
    session_present = bool(session_path and session_path.exists())
    if session.usable:
        auth_status = "session_available_unverified"
        authenticated: bool | None = None
    elif credentials_configured:
        auth_status = "credentials_available_login_required"
        authenticated = False
    elif session_path is not None:
        auth_status = "session_missing"
        authenticated = False
    else:
        auth_status = "unconfigured"
        authenticated = False
    return ToolResult.success(
        {
            "provider": "jmcomic",
            "auth_status": auth_status,
            "credentials_configured": credentials_configured,
            "session_configured": session_path is not None,
            "session_present": session_present,
            "reusable": session.usable,
            "authenticated": authenticated,
            "remote_verified": False,
        }
    )


async def jmcomic_auth_login(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        client = _jm_client(context, input_data, login_if_needed=True, force_login=True)
        path = jm_auth.save_session(
            client.session,
            env=context.env,
            cwd=context.cwd,
            session_file=input_data.get("session_file"),
        )
    except Exception as exc:
        return _provider_failure(exc)
    return ToolResult.success({"provider": "jmcomic", "status": "authenticated", "session_file": str(path), "credentials_written": True})


async def jmcomic_favorites_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path_error = _validate_sync_paths(context, input_data)
    if path_error is not None:
        return path_error
    recovery: _JMAuthRecovery | None = None
    try:
        client = _jm_client(context, input_data, login_if_needed=not context.dry_run)
        recovery = _JMAuthRecovery(context, input_data, client, allow_reauth=not context.dry_run)
        recovery.checkpoint()
        collection = recovery.run(client.collect_favorites)
        targets = [
            {
                "target_type": "album",
                "target_id": favorite.album_id,
                "metadata": {
                    "provider_work_id": favorite.provider_work_id,
                    "title": favorite.title,
                    "latest_photo_id": favorite.latest_photo_id,
                },
            }
            for favorite in collection.items
        ]
        snapshot = _commit_or_preview_snapshot(
            context,
            input_data,
            "jmcomic",
            _favorites_collection_key(client.session.username),
            targets,
            "series_and_follow",
        )
        selected = _limit(targets, input_data.get("download_limit"))
        result = await _sync_favorite_targets(
            context,
            input_data,
            selected,
            lambda target: recovery.run(
                lambda: client.resolve_exact(
                    f"https://18comic.vip/album/{target['target_id']}/"
                )
            ).normalized_items(),
        )
        recovery.checkpoint()
        result.data.update(
            {
                "collection": "favorites",
                "target_policy": "series_and_follow",
                "snapshot": snapshot,
                "favorites_seen": len(targets),
                "following": len(targets),
                **recovery.safe_metadata(),
            }
        )
        return result
    except Exception as exc:
        result = _provider_failure(exc)
        if recovery is not None:
            result.data.update(recovery.safe_metadata())
        return result


async def _sync_favorite_targets(
    context: ToolContext,
    input_data: dict[str, Any],
    targets: list[dict[str, Any]],
    resolve_target: Any,
) -> ToolResult:
    summary: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    warnings: list[str] = []
    failures: list[dict[str, Any]] = []
    attempted = 0
    processed = 0
    for target in targets:
        attempted += 1
        try:
            resolved_items = resolve_target(target)
        except Exception as exc:
            failed = _provider_failure(exc)
            failures.append(_target_failure(target, failed))
            if failed.error and failed.error.category in {ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT}:
                break
            continue
        target_result = await _sync_items(context, input_data, resolved_items)
        processed += 1
        target_summary = target_result.data.get("summary") or {}
        for key, value in target_summary.items():
            if isinstance(value, int) and not isinstance(value, bool):
                summary[key] = summary.get(key, 0) + value
        items.extend(target_result.data.get("items") or [])
        packages.extend(target_result.data.get("packages") or [])
        artifacts.extend(target_result.artifacts)
        warnings.extend(target_result.warnings)
        if not target_result.is_success:
            failures.append(_target_failure(target, target_result))
            if target_result.error and target_result.error.category in {ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT}:
                break
    data = {
        "dry_run": context.dry_run,
        "db_path": str(_required_db_path(context, input_data)),
        "library_root": str(_library_root(context, input_data)),
        "summary": {
            **summary,
            "targets_selected": len(targets),
            "targets_attempted": attempted,
            "targets_processed": processed,
            "targets_failed": len(failures),
            "targets_unprocessed": len(targets) - attempted,
        },
        "items": items,
        "packages": packages,
        "target_failures": failures,
    }
    if not failures:
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings)
    category = next(
        (
            ErrorCategory(failure["category"])
            for failure in failures
            if failure.get("category") in {ErrorCategory.AUTH.value, ErrorCategory.RATE_LIMIT.value}
        ),
        ErrorCategory.NETWORK,
    )
    return ToolResult.failure(
        "comic_favorites_sync_partial",
        "Comic favorites sync completed with one or more failed targets.",
        data=data,
        warnings=warnings,
        category=category,
    )


def _target_failure(target: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    error = result.error
    return {
        "target_type": str(target.get("target_type") or "unknown"),
        "target_id": str(target.get("target_id") or "unknown"),
        "code": error.code if error else "comic_target_failed",
        "category": error.category.value if error else ErrorCategory.RUNTIME.value,
    }


async def jmcomic_favorites_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    recovery: _JMAuthRecovery | None = None
    try:
        client = _jm_client(context, input_data, login_if_needed=not context.dry_run)
        recovery = _JMAuthRecovery(context, input_data, client, allow_reauth=not context.dry_run)
        recovery.checkpoint()
        collection = recovery.run(client.collect_favorites)
        return ToolResult.success(
            {
                "provider": "jmcomic",
                "collection": "favorites",
                "complete": True,
                "pages_fetched": collection.pages_fetched,
                "expected_total": collection.total,
                "favorites_seen": len(collection.items),
                "target_policy": "series_and_follow",
                "following": len(collection.items),
                **recovery.safe_metadata(),
            }
        )
    except Exception as exc:
        result = _provider_failure(exc)
        if recovery is not None:
            result.data.update(recovery.safe_metadata())
        return result


async def _sync_items(context: ToolContext, input_data: dict[str, Any], items: list[dict[str, Any]]) -> ToolResult:
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        return db_path
    try:
        library_root = _library_root(context, input_data)
        ensure_inside(db_path, context.allowed_write_roots())
        ensure_inside(library_root, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    items = _dedupe_items(items)
    statuses = db.get_media_statuses(db_path, items)
    if input_data.get("overwrite"):
        for item in items:
            statuses.pop((item["platform"], item["remote_id"]), None)
    for identity in _changed_manifest_identities(db_path, items):
        statuses.pop(identity, None)
    candidates, skipped = link_tools._sync_candidates(
        items,
        statuses,
        db_path=db_path,
        retry_failed=bool(input_data.get("retry_failed", True)),
        repair_missing_files=bool(input_data.get("repair_missing_files", True)),
    )
    if context.dry_run:
        planned_downloads = [
            plan
            for item in candidates
            for plan in link_tools._planned_downloads(context, input_data, item)
        ]
        return ToolResult.success(
            {
                "dry_run": True,
                "db_path": str(db_path),
                "library_root": str(library_root),
                "summary": {
                    "resolved_items": len(items),
                    "queued": len(candidates),
                    "planned_files": len(planned_downloads),
                    **skipped,
                },
                "planned_downloads": planned_downloads,
            }
        )
    db.initialize_database(db_path)
    for item in items:
        db.upsert_media_item(db_path, item)
    item_results = []
    artifacts: list[dict[str, str]] = []
    for item in candidates:
        item_result = await link_tools._sync_one_link_item(context, db_path, item, input_data)
        item_results.append(item_result)
        artifacts.extend({"type": "file", "path": path} for path in item_result["artifacts"])
    packages = []
    refreshed_identities = {(item["platform"], item["remote_id"]) for item in candidates}
    for item in _load_comic_items(db_path, {(item["platform"], item["remote_id"]) for item in items}):
        plan = _comic_package_plan(
            item=item,
            library_root=library_root,
            include_platform_layer=bool(input_data.get("include_platform_layer", True)),
            overwrite=bool(input_data.get("overwrite")),
            migrate_legacy=False,
            refresh_tracked=(item["platform"], item["remote_id"]) in refreshed_identities,
        )
        if plan["status"] == "ready":
            try:
                package = _apply_comic_package(db_path=db_path, item=item, plan=plan, library_root=library_root)
                packages.append({**_public_package_plan(plan), "status": "packaged", **package})
                artifacts.append({"type": "file", "path": package["target_path"]})
            except Exception as exc:
                packages.append({**_public_package_plan(plan), "status": "failed", "error": {"code": "comic_package_failed", "exception_type": type(exc).__name__}})
        else:
            packages.append(_public_package_plan(plan))
    downloaded = sum(result["status"] == "downloaded" for result in item_results)
    failed = sum(result["status"] == "failed" for result in item_results)
    partial = sum(result["status"] == "partial" for result in item_results)
    files_skipped = sum(int(result.get("files_skipped", 0)) for result in item_results)
    package_failed = sum(package["status"] in {"failed", "blocked", "incomplete"} for package in packages)
    summary = {
        "resolved_items": len(items),
        "queued": len(candidates),
        "downloaded": downloaded,
        "partial": partial,
        "failed": failed,
        "files_skipped": files_skipped,
        "cbz_packaged": sum(package["status"] == "packaged" for package in packages),
        "cbz_existing": sum(package["status"] == "existing" for package in packages),
        "cbz_failed_or_incomplete": package_failed,
        **skipped,
    }
    run_status = "success" if not (failed or partial or package_failed) else "partial"
    db.insert_run(db_path, run_type="tool", name="comic.sync", status=run_status, summary=summary, error=None, dry_run=False)
    data = {"dry_run": False, "db_path": str(db_path), "library_root": str(library_root), "summary": summary, "items": item_results, "packages": packages}
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts)
    return ToolResult.failure("comic_sync_partial", "Comic sync completed with incomplete downloads or packages.", data=data, category=ErrorCategory.NETWORK)


def _load_comic_items(db_path: Path, identities: set[tuple[str, str]]) -> list[dict[str, Any]]:
    if not identities:
        return []
    item_rows = []
    file_rows = []
    with db.connect(db_path) as connection:
        for identity_batch in _chunked(sorted(identities), 400):
            placeholders = ",".join("(?, ?)" for _ in identity_batch)
            params = [value for identity in identity_batch for value in identity]
            item_rows.extend(
                connection.execute(
                    f"SELECT id, platform, remote_id, source_url, author_id, author_name, media_type, status, metadata_json, source_availability FROM media_items WHERE (platform, remote_id) IN ({placeholders}) ORDER BY id",
                    params,
                ).fetchall()
            )
        item_rows.sort(key=lambda row: int(row["id"]))
        item_ids = [int(row["id"]) for row in item_rows]
        for item_id_batch in _chunked(item_ids, 900):
            placeholders = ",".join("?" for _ in item_id_batch)
            file_rows.extend(
                connection.execute(
                    f"SELECT id, media_item_id, file_key, remote_url, local_path, mime_type, size_bytes, checksum, status, library_relative_path, storage_layout, file_health, source_timestamp, verified_at FROM media_files WHERE media_item_id IN ({placeholders}) ORDER BY id",
                    item_id_batch,
                ).fetchall()
            )
        file_rows.sort(key=lambda row: int(row["id"]))
    files: dict[int, list[dict[str, Any]]] = {}
    for row in file_rows:
        files.setdefault(int(row["media_item_id"]), []).append(dict(row))
    return [
        {**{key: row[key] for key in row.keys() if key != "metadata_json"}, "metadata": json.loads(row["metadata_json"]), "files": files.get(int(row["id"]), [])}
        for row in item_rows
        if (str(row["platform"]), str(row["remote_id"])) in identities
    ]


def _changed_manifest_identities(
    db_path: Path,
    items: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    if not db_path.exists() or not items:
        return set()
    identities = {(str(item["platform"]), str(item["remote_id"])) for item in items}
    rows = []
    with db.connect(db_path) as connection:
        for identity_batch in _chunked(sorted(identities), 400):
            placeholders = ",".join("(?, ?)" for _ in identity_batch)
            params = [value for identity in identity_batch for value in identity]
            rows.extend(
                connection.execute(
                    f"SELECT platform, remote_id, metadata_json FROM media_items WHERE (platform, remote_id) IN ({placeholders})",
                    params,
                ).fetchall()
            )
    previous = {
        (str(row["platform"]), str(row["remote_id"])): json.loads(row["metadata_json"])
        for row in rows
    }
    changed = set()
    for item in items:
        identity = (str(item["platform"]), str(item["remote_id"]))
        old_metadata = previous.get(identity)
        if old_metadata is None:
            continue
        if _manifest_keys(old_metadata) != _manifest_keys(item.get("metadata") or {}):
            changed.add(identity)
    return changed


def _manifest_keys(metadata: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        key
        for file_info in metadata.get("files") or []
        if isinstance(file_info, dict)
        for key in [link_tools._stable_file_key(file_info) or str(file_info.get("url") or "")]
        if key
    )


def _chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _favorites_collection_key(account_id: Any) -> str:
    text = str(account_id or "").strip()
    if not text:
        return "favorites:default"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"favorites:{digest}"


def _commit_or_preview_snapshot(
    context: ToolContext,
    input_data: dict[str, Any],
    provider: str,
    collection_key: str,
    targets: list[dict[str, Any]],
    policy: str,
) -> dict[str, Any]:
    if context.dry_run:
        return {"committed": False, "dry_run": True, "total": len(targets)}
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        raise ValueError("Provide db_path or set MEDIAGENT_DB_PATH.")
    ensure_inside(db_path, context.allowed_write_roots())
    db.initialize_database(db_path)
    result = db.commit_collection_snapshot(
        db_path,
        provider=provider,
        collection_key=collection_key,
        targets=targets,
        metadata={"policy": policy},
    )
    return {"committed": True, **result}


def _jm_client(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    login_if_needed: bool,
    force_login: bool = False,
) -> JMComicClient:
    try:
        session = jm_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    except jm_auth.JMComicAuthError:
        if not force_login:
            raise
        session = jm_auth.JMComicSession({})
    client = JMComicClient(
        JMComicApiTransport(http_client=context.http_client, timeout=float(input_data.get("timeout_seconds", 30.0))),
        session=session,
    )
    if force_login or (login_if_needed and not session.usable):
        config = jm_auth.load_config(env=context.env, cwd=context.cwd)
        if not config.username or not config.password:
            raise jm_auth.JMComicAuthError("jmcomic_login_required", "JMComic credentials or a reusable session are required.")
        client.login(username=config.username, password=config.password)
    return client


class _JMAuthRecovery:
    """Bound one JMComic run to one credential recovery and durable cookie checkpoints."""

    def __init__(
        self,
        context: ToolContext,
        input_data: dict[str, Any],
        client: JMComicClient,
        *,
        allow_reauth: bool,
    ) -> None:
        self.context = context
        self.input_data = input_data
        self.client = client
        self.allow_reauth = allow_reauth
        self.reauth_attempted = False
        self.auth_recovered = False
        self.session_checkpoints = 0
        self._last_checkpointed_session: jm_auth.JMComicSession | None = None

    def run(self, operation: Any) -> Any:
        try:
            result = operation()
        except JMComicClientError as exc:
            if exc.code != "jmcomic_auth_required" or not self.allow_reauth or self.reauth_attempted:
                raise
            self.reauth_attempted = True
            config = jm_auth.load_config(env=self.context.env, cwd=self.context.cwd)
            if not config.username or not config.password:
                raise jm_auth.JMComicAuthError(
                    "jmcomic_login_required",
                    "JMComic credentials are required to recover an expired session.",
                ) from exc
            self.client.login(username=config.username, password=config.password)
            self.auth_recovered = True
            self.checkpoint()
            result = operation()
        self.checkpoint()
        return result

    def checkpoint(self) -> bool:
        if self.context.dry_run or self.client.session == self._last_checkpointed_session:
            return False
        if not _persist_jm_session_if_configured(self.context, self.input_data, self.client):
            return False
        self._last_checkpointed_session = self.client.session
        self.session_checkpoints += 1
        return True

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "auth_recovery_attempted": self.reauth_attempted,
            "auth_recovered": self.auth_recovered,
            "session_checkpointed": self.session_checkpoints > 0,
            "session_checkpoints": self.session_checkpoints,
        }


def _persist_jm_session_if_configured(
    context: ToolContext,
    input_data: dict[str, Any],
    client: JMComicClient,
) -> bool:
    if not client.session.usable:
        return False
    if jm_auth.session_file_path(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file")) is None:
        return False
    jm_auth.save_session(client.session, env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    return True


def _optional_nh_session(context: ToolContext, input_data: dict[str, Any]) -> dict[str, Any] | None:
    path = nh_auth.session_file_path(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))
    if path is None or not path.exists():
        return None
    return nh_auth.load_session(env=context.env, cwd=context.cwd, session_file=input_data.get("session_file"))


def _required_db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | ToolResult:
    raw = input_data.get("db_path")
    path = Path(resolve_placeholders(raw, context.env)).expanduser().resolve() if raw else context.db_path
    if path is None:
        return ToolResult.failure("missing_db_path", "Provide db_path or set MEDIAGENT_DB_PATH.", category=ErrorCategory.VALIDATION)
    return path


def _library_root(context: ToolContext, input_data: dict[str, Any]) -> Path:
    if input_data.get("library_root"):
        return normalize_path(str(input_data["library_root"]), env=context.env, cwd=context.cwd)
    return default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)


def _validate_sync_paths(context: ToolContext, input_data: dict[str, Any]) -> ToolResult | None:
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        return db_path
    try:
        ensure_inside(db_path, context.allowed_write_roots())
        ensure_inside(_library_root(context, input_data), context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    return None


def _input_urls(input_data: dict[str, Any]) -> list[str]:
    values = [input_data.get("url"), *(input_data.get("urls") or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _limit(values: list[dict[str, Any]], value: Any) -> list[dict[str, Any]]:
    return values if value is None else values[: max(0, int(value))]


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({(item["platform"], item["remote_id"]): item for item in items}.values())


def _provider_failure(exc: Exception) -> ToolResult:
    code = str(getattr(exc, "code", "comic_source_failed"))
    if "auth" in code or "login" in code or "session" in code:
        category = ErrorCategory.AUTH
    elif "rate" in code:
        category = ErrorCategory.RATE_LIMIT
    elif isinstance(exc, (ValueError, JMComicLinkError)):
        category = ErrorCategory.VALIDATION
    else:
        category = ErrorCategory.NETWORK
    return ToolResult.failure(code, str(exc), details={"exception_type": type(exc).__name__}, category=category)
