"""Comic-only link and favorite synchronization tools."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mediagent.core import assets, comic_sync_state, db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.operational_logging import ProgressLogger
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
from mediagent.platforms.jmcomic.client import JMComicApiTransport, JMComicClient, JMComicClientError, JMComicResolution
from mediagent.platforms.jmcomic import incremental as jm_incremental
from mediagent.platforms.jmcomic.favorite_folders import (
    ALL_FOLDER_ALIASES,
    ALL_FOLDER_NAME,
    FOLDERS_ENV,
    JMComicFavoriteFolder,
    JMComicFavoriteFolderError,
    all_folder,
    normalize_folder_name,
    parse_folder_locator,
    parse_remote_folder_list,
)
from mediagent.platforms.jmcomic.links import JMComicLinkError, parse_jmcomic_link
from mediagent.platforms.nhentai import auth as nh_auth
from mediagent.platforms.nhentai import client as nh_client
from mediagent.platforms.nhentai.links import parse_gallery_link
from mediagent.tools import jmcomic_reconcile, link_tools
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
    folder_selector_schema = {
        "type": "array",
        "items": {"type": "string"},
    }
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
                name="jmcomic.favorites.folders.register",
                description="Register or rename one local JMComic favorite-folder alias from a trusted folder URL or numeric ID.",
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "required_any": [["folder", "folder_id", "url"]],
                    "properties": {
                        "name": {"type": "string"},
                        "folder": {"type": "string"},
                        "folder_id": {"type": "string"},
                        "url": {"type": "string"},
                        "replace": {"type": "boolean"},
                        "db_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
            ),
            handler=jmcomic_favorite_folder_register,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.folders.list",
                description="List locally registered JMComic favorite-folder aliases without contacting JMComic.",
                input_schema={"type": "object", "properties": {"db_path": {"type": "string"}}},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.READ_DB),
                dry_run_supported=True,
            ),
            handler=jmcomic_favorite_folders_list,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.folders.collect",
                description="Fetch the current JMComic favorite-folder name and ID index without downloading comics.",
                input_schema={
                    "type": "object",
                    "properties": {"session_file": {"type": "string"}, "timeout_seconds": {"type": "number"}},
                },
                output_schema={"type": "object"},
                permissions=collect_permissions,
                dry_run_supported=True,
            ),
            handler=jmcomic_favorite_folders_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.collect",
                description="Validate JMComic authentication and collect one complete favorites snapshot without downloading.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                        "db_path": {"type": "string"},
                        "folders": folder_selector_schema,
                    },
                },
                output_schema={"type": "object"},
                permissions=(*collect_permissions, Permission.READ_DB),
                dry_run_supported=True,
            ),
            handler=jmcomic_favorites_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.favorites.sync",
                description="Treat the complete JMComic album favorites list as an inbox and follow active albums for new chapters.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **common_sync_properties,
                        "session_file": {"type": "string"},
                        "folders": folder_selector_schema,
                        "max_chapters": {"type": "integer", "minimum": 1, "default": 20},
                        "time_budget_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 300},
                        "audit_chapters": {"type": "integer", "minimum": 0, "default": 2},
                        "revalidate_all": {"type": "boolean", "default": False},
                    },
                },
                output_schema={"type": "object"},
                permissions=sync_permissions,
                dry_run_supported=True,
            ),
            handler=jmcomic_favorites_sync,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="jmcomic.library.reconcile",
                description=(
                    "Audit existing JMComic chapters against current album manifests and, "
                    "with explicit confirmation, rebuild only affected CBZ archives from healthy local pages."
                ),
                input_schema={
                    "type": "object",
                    "required": ["mode"],
                    "properties": {
                        "mode": {"type": "string", "enum": ["plan", "apply"]},
                        "confirm": {"type": "boolean"},
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "quarantine_dir": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "album_id": {"type": "string"},
                        "album_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1},
                        "include_details": {"type": "boolean"},
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_CREDENTIALS,
                    Permission.NETWORK,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=jmcomic_library_reconcile,
        ),
    ]


async def jmcomic_library_reconcile(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    mode = str(input_data.get("mode") or "").strip().lower()
    if mode not in {"plan", "apply"}:
        return ToolResult.failure(
            "jmcomic_reconcile_mode_invalid",
            "mode must be plan or apply.",
            category=ErrorCategory.VALIDATION,
        )
    path_error = _validate_sync_paths(context, input_data)
    if path_error is not None:
        return path_error
    db_path = _required_db_path(context, input_data)
    assert isinstance(db_path, Path)
    library_root = _library_root(context, input_data).resolve()
    quarantine_dir = (
        normalize_path(str(input_data["quarantine_dir"]), env=context.env, cwd=context.cwd)
        if input_data.get("quarantine_dir")
        else library_root / ".trash" / "mediagent-jmcomic-reconcile" / context.run_id
    ).resolve()
    try:
        ensure_inside(quarantine_dir, [library_root])
        if not db_path.is_file():
            raise ValueError(f"Database does not exist: {db_path}")
        existing_items = jmcomic_reconcile.select_jmcomic_items(db_path)
        available_album_ids = {
            album_id
            for item in existing_items
            if (album_id := jmcomic_reconcile.album_id_for_item(item)) is not None
        }
        requested = _reconcile_album_ids(input_data)
        if requested is not None:
            unknown = sorted(requested - available_album_ids, key=int)
            if unknown:
                raise ValueError(f"JMComic albums are not present in the database: {', '.join(unknown)}")
            selected_album_ids = requested
        else:
            selected_album_ids = available_album_ids
        ordered_album_ids = sorted(selected_album_ids, key=int)
        if input_data.get("limit") is not None:
            ordered_album_ids = ordered_album_ids[: int(input_data["limit"])]
        selected_album_ids = set(ordered_album_ids)

        resolved_by_album: dict[str, list[dict[str, Any]]] = {}
        failed_albums: dict[str, dict[str, str]] = {}
        if ordered_album_ids:
            client = _jm_client(context, input_data, login_if_needed=False)
            for album_id in ordered_album_ids:
                try:
                    album = client.get_album(album_id)
                    resolved_by_album[album_id] = jmcomic_reconcile.identities_from_album(album)
                except Exception as exc:
                    failed_albums[album_id] = {
                        "code": str(getattr(exc, "code", "jmcomic_manifest_failed")),
                        "exception_type": type(exc).__name__,
                    }
        manifest = jmcomic_reconcile.build_manifest(
            db_path=db_path,
            library_root=library_root,
            quarantine_dir=quarantine_dir,
            resolved_by_album=resolved_by_album,
            failed_albums=failed_albums,
            include_platform_layer=bool(input_data.get("include_platform_layer", True)),
            selected_album_ids=selected_album_ids,
        )
        is_plan = mode == "plan" or context.dry_run
        public = jmcomic_reconcile.public_manifest(
            manifest,
            include_details=bool(input_data.get("include_details", True)),
        )
        public.update(
            {
                "provider": "jmcomic",
                "mode": "plan" if is_plan else "apply",
                "dry_run": context.dry_run,
                "quarantine_dir": str(quarantine_dir),
            }
        )
        if is_plan:
            return ToolResult.success(public)
        if not bool(input_data.get("confirm")):
            return ToolResult.failure(
                "jmcomic_reconcile_confirmation_required",
                "Apply mode requires confirm=true after reviewing a plan.",
                data=public,
                category=ErrorCategory.VALIDATION,
            )
        if manifest["summary"]["blocked"]:
            return ToolResult.failure(
                "jmcomic_reconcile_blocked",
                "No changes were applied because one or more albums or chapters could not be reconciled safely.",
                data=public,
                category=ErrorCategory.VALIDATION,
            )
        manifest = jmcomic_reconcile.apply_manifest(
            db_path=db_path,
            library_root=library_root,
            manifest=manifest,
        )
        public = jmcomic_reconcile.public_manifest(
            manifest,
            include_details=bool(input_data.get("include_details", True)),
        )
        public.update(
            {
                "provider": "jmcomic",
                "mode": "apply",
                "dry_run": False,
                "quarantine_dir": str(quarantine_dir),
            }
        )
        if manifest["summary"].get("apply_failed"):
            return ToolResult.failure(
                "jmcomic_reconcile_partial",
                "Reconciliation completed with one or more rebuild failures.",
                data=public,
                category=ErrorCategory.FILESYSTEM,
            )
        db.insert_run(
            db_path,
            run_type="maintenance",
            name="jmcomic.library.reconcile",
            status="success",
            summary=public["summary"],
            error=None,
            dry_run=False,
        )
        return ToolResult.success(public)
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure(
            "jmcomic_reconcile_invalid",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.VALIDATION,
        )
    except Exception as exc:
        return _provider_failure(exc)


def _reconcile_album_ids(input_data: dict[str, Any]) -> set[str] | None:
    raw_values = [input_data.get("album_id"), *(input_data.get("album_ids") or [])]
    values = [str(value).strip() for value in raw_values if str(value or "").strip()]
    if not values:
        return None
    invalid = [value for value in values if not value.isdigit() or int(value) <= 0]
    if invalid:
        raise ValueError("JMComic album IDs must be positive integers.")
    return set(values)


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
        if input_data.get("_delivery_link_id") is not None and not context.dry_run:
            from mediagent.core import link_delivery_jobs

            link_delivery_jobs.bind_media(
                _required_db_path(context, input_data), link_id=int(input_data["_delivery_link_id"]),
                identities=[(item["platform"], item["remote_id"]) for item in items],
            )
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


async def jmcomic_favorite_folder_register(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        name, name_key = normalize_folder_name(input_data.get("name"))
        locator_values = [
            str(input_data[key]).strip()
            for key in ("folder", "folder_id", "url")
            if input_data.get(key) not in (None, "")
        ]
        if len(locator_values) != 1:
            raise JMComicFavoriteFolderError(
                "jmcomic_folder_locator_invalid",
                "Provide exactly one of folder, folder_id, or url.",
            )
        folder_id, canonical_url = parse_folder_locator(locator_values[0])
        if name_key in ALL_FOLDER_ALIASES and folder_id != "0":
            raise JMComicFavoriteFolderError(
                "jmcomic_folder_name_reserved",
                "The All folder name is reserved for folder ID 0.",
            )
        if folder_id == "0" and name_key not in ALL_FOLDER_ALIASES:
            raise JMComicFavoriteFolderError(
                "jmcomic_folder_id_reserved",
                "Folder ID 0 must use a reserved All folder name.",
            )
        db_path = _required_db_path(context, input_data)
        if isinstance(db_path, ToolResult):
            return db_path
        ensure_inside(db_path, context.allowed_write_roots())
        folder = JMComicFavoriteFolder(name, folder_id, canonical_url)
        if context.dry_run:
            return ToolResult.success({"provider": "jmcomic", "registered": False, "dry_run": True, "folder": folder.public_dict()})
        db.initialize_database(db_path)
        registered = db.register_collection_scope_alias(
            db_path,
            provider="jmcomic",
            account_key=_jm_account_collection_key(context),
            scope_kind="favorite_folder",
            scope_name=name,
            scope_name_key=name_key,
            remote_id=folder_id,
            canonical_url=canonical_url,
            replace=bool(input_data.get("replace")),
        )
        return ToolResult.success(
            {
                "provider": "jmcomic",
                "registered": True,
                "folder": {
                    "name": registered["name"],
                    "folder_id": registered["remote_id"],
                    "url": registered["url"],
                },
            }
        )
    except Exception as exc:
        return _provider_failure(exc)


async def jmcomic_favorite_folders_list(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _required_db_path(context, input_data)
        if isinstance(db_path, ToolResult):
            return db_path
        ensure_inside(db_path, context.allowed_write_roots())
        aliases = db.list_collection_scope_aliases(
            db_path,
            provider="jmcomic",
            account_key=_jm_account_collection_key(context),
            scope_kind="favorite_folder",
        )
        folders = [
            all_folder().public_dict(),
            *(
                {
                    "name": alias["name"],
                    "folder_id": alias["remote_id"],
                    "url": alias["url"],
                    "updated_at": alias["updated_at"],
                }
                for alias in aliases
            ),
        ]
        return ToolResult.success({"provider": "jmcomic", "folders": folders, "folder_count": len(folders)})
    except Exception as exc:
        return _provider_failure(exc)


async def jmcomic_favorite_folders_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    recovery: _JMAuthRecovery | None = None
    try:
        client = _jm_client(context, input_data, login_if_needed=not context.dry_run)
        recovery = _JMAuthRecovery(context, input_data, client, allow_reauth=not context.dry_run)
        recovery.checkpoint()
        folders = recovery.run(lambda: _remote_jm_favorite_folders(client))
        return ToolResult.success(
            {
                "provider": "jmcomic",
                "complete": True,
                "folders": [folder.public_dict() for folder in folders],
                "folder_count": len(folders),
                **recovery.safe_metadata(),
            }
        )
    except Exception as exc:
        result = _provider_failure(exc)
        if recovery is not None:
            result.data.update(recovery.safe_metadata())
        return result


async def jmcomic_favorites_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path_error = _validate_sync_paths(context, input_data)
    if path_error is not None:
        return path_error
    recovery: _JMAuthRecovery | None = None
    try:
        client = _jm_client(context, input_data, login_if_needed=not context.dry_run)
        recovery = _JMAuthRecovery(context, input_data, client, allow_reauth=not context.dry_run)
        recovery.checkpoint()
        folders = _resolve_jm_favorite_folders_with_discovery(context, input_data, client, recovery)
        targets, folder_snapshots = _collect_jm_favorite_folder_targets(client, recovery, folders)
        snapshot = _commit_or_preview_snapshot(
            context,
            input_data,
            "jmcomic",
            _jm_account_collection_key(context, client),
            targets,
            "series_and_follow",
            metadata={
                "selected_folders": [folder.public_dict() for folder in folders],
                "folder_snapshots": folder_snapshots,
            },
        )
        selected = _limit(targets, input_data.get("download_limit"))
        result = await _sync_jm_incremental_targets(context, input_data, selected, client, recovery)
        recovery.checkpoint()
        result.data.update(
            {
                "collection": "favorites",
                "target_policy": "series_and_follow",
                "snapshot": snapshot,
                "favorites_seen": len(targets),
                "following": len(targets),
                "folder_memberships_seen": sum(folder["favorites_seen"] for folder in folder_snapshots),
                "folders": folder_snapshots,
                **recovery.safe_metadata(),
            }
        )
        return result
    except Exception as exc:
        result = _provider_failure(exc)
        if recovery is not None:
            result.data.update(recovery.safe_metadata())
        return result


async def _sync_jm_incremental_targets(
    context: ToolContext,
    input_data: dict[str, Any],
    targets: list[dict[str, Any]],
    client: JMComicClient,
    recovery: "_JMAuthRecovery",
) -> ToolResult:
    """Observe complete albums, then resolve and commit bounded chapter work."""
    database = _required_db_path(context, input_data)
    library = _library_root(context, input_data)
    max_chapters = int(input_data.get("max_chapters", 20))
    budget = float(input_data.get("time_budget_seconds", 300))
    audit_limit = int(input_data.get("audit_chapters", 2))
    revalidate = bool(input_data.get("revalidate_all", False))
    repair = bool(input_data.get("repair_missing_files", True))
    retry = bool(input_data.get("retry_failed", True))
    if max_chapters < 1 or not math.isfinite(budget) or budget <= 0 or audit_limit < 0:
        return ToolResult.failure("invalid_sync_budget", "Chapter/time limits must be positive and audit_chapters nonnegative.", category=ErrorCategory.VALIDATION)
    if revalidate and (not repair or not retry):
        return ToolResult.failure("invalid_revalidation_policy", "Full revalidation requires repair and failed-chapter retries.", category=ErrorCategory.VALIDATION)
    deadline = time.monotonic() + budget
    options = {**input_data, "_deadline_monotonic": deadline, "_incremental_sync": True}
    now = datetime.now(UTC)
    account_key = _jm_account_collection_key(context, client)
    # One batched media/file load for the collection. No per-album table scans.
    if database.exists():
        with db.connect(database) as connection:
            identities = {("jmcomic", str(row["remote_id"])) for row in connection.execute("SELECT remote_id FROM media_items WHERE platform = 'jmcomic'")}
        cached = {item["remote_id"]: item for item in _load_comic_items(database, identities)}
    else:
        cached = {}
    summary: dict[str, int] = {
        "targets_selected": len(targets), "targets_attempted": 0, "targets_processed": 0,
        "targets_failed": 0, "albums_requested": 0, "chapters_requested": 0,
        "scramble_requested": 0, "chapters_processed": 0, "chapters_healthy": 0,
        "chapters_suppressed": 0, "chapters_pending": 0, "chapters_failed_deferred": 0,
        "chapters_skipped_no_repair": 0, "chapters_audited": 0, "chapters_local_repair": 0,
        "chapters_metadata_refresh": 0, "latest_hint_unchanged": 0, "filesystem_checks": 0,
    }
    results: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    exhausted = False
    owner = f"sync:{uuid.uuid4().hex}"
    if revalidate and not context.dry_run:
        comic_sync_state.request_revalidation(database, provider="jmcomic", account_key=account_key, album_ids=[str(target["target_id"]) for target in targets])
    previous_states = {
        str(target["target_id"]): comic_sync_state.read_album(database, provider="jmcomic", account_key=account_key, album_id=str(target["target_id"]))
        for target in targets
    }
    resuming_full_pass = any(row and row.get("revalidation_active") for row in previous_states.values())
    targets = sorted(targets, key=lambda target: (previous_states[str(target["target_id"])] or {}).get("last_work_at") or "")
    for target in targets:
        if time.monotonic() >= deadline:
            exhausted = True
            break
        scope = {"provider": "jmcomic", "account_key": account_key, "album_id": str(target["target_id"])}
        summary["targets_attempted"] += 1
        try:
            summary["albums_requested"] += 1
            album = recovery.run(lambda: client.get_album(target["target_id"]))
            manifest = jm_incremental.chapter_manifest(album)
            fingerprints = {row["id"]: row["fingerprint"] for row in manifest}
            previous = previous_states[str(target["target_id"])]
            previous_chapters = {row["chapter_id"]: row for row in (previous or {}).get("chapters", [])}
            prior_ids = set(previous_chapters)
            latest_hint = (target.get("metadata") or {}).get("latest_photo_id")
            summary["latest_hint_unchanged"] += int(previous is not None and previous.get("latest_photo_id") == latest_hint)
            start_album_pass = revalidate and (not resuming_full_pass or previous is None or bool(previous.get("revalidation_active")))
            if context.dry_run:
                state = previous or {"generation": 0, "chapters": [], "last_audit_at": now.isoformat(), "audit_cursor": 0}
                chapters = {row["chapter_id"]: dict(row) for row in state["chapters"]}
                if start_album_pass and not state.get("revalidation_active"):
                    for row in chapters.values():
                        if row["status"] != "suppressed":
                            row["status"] = "pending"
                    state = {**state, "revalidation_active": True}
            else:
                state = comic_sync_state.save_manifest(database, **scope, latest_photo_id=latest_hint, episodes=manifest, revalidate_all=False)
                chapters = {row["chapter_id"]: row for row in state["chapters"]}
            identities = {item["remote_id"]: item for item in jmcomic_reconcile.identities_from_album(album)}
            healths: dict[str, dict[str, Any]] = {}
            adoption: dict[str, str] = {}
            for episode in album.episodes:
                chapter_id = episode.photo_id
                old = cached.get(f"photo:{chapter_id}")
                health = jm_incremental.local_health(old, library)
                summary["filesystem_checks"] += health.get("filesystem_checks", 0)
                healths[chapter_id] = health
                chapter = chapters.get(chapter_id)
                if chapter is None or not int(chapter.get("attempt_count", 0)) and chapter.get("status") == "pending":
                    if health["suppressed"]:
                        adoption[chapter_id] = "suppressed"
                    elif health["complete"] and not state.get("revalidation_active"):
                        # Only legacy/unattempted state is adopted. An established
                        # changed-manifest pending chapter still needs resolution.
                        if chapter_id not in prior_ids:
                            adoption[chapter_id] = "succeeded"
            if adoption and not context.dry_run:
                comic_sync_state.adopt_chapters(database, **scope, states=adoption, generation=state["generation"])
                state = comic_sync_state.read_album(database, **scope)
                chapters = {row["chapter_id"]: row for row in state["chapters"]}
            if context.dry_run:
                for chapter_id, status in adoption.items():
                    chapters[chapter_id] = {**chapters.get(chapter_id, {}), "status": status, "attempt_count": 0}
            audit_at = datetime.fromisoformat(state["last_audit_at"]) if state.get("last_audit_at") else now
            audit_due = now - audit_at >= timedelta(hours=24)
            audit_ids: set[str] = set()
            audit_cursor = int(state.get("audit_cursor") or 0)
            if audit_due and audit_limit and not revalidate and album.episodes:
                audit_ids = {album.episodes[(audit_cursor + offset) % len(album.episodes)].photo_id for offset in range(min(audit_limit, len(album.episodes)))}
            album_failed = False
            scramble_id: str | None = None
            audited_positions: list[int] = []
            ordered_episodes = sorted(enumerate(album.episodes), key=lambda pair: (
                0 if f"photo:{pair[1].photo_id}" not in cached else
                1 if (chapters.get(pair[1].photo_id) or {}).get("status") in {"pending", "paused", "failed", "running"} else
                2 if not healths[pair[1].photo_id]["complete"] else
                3 if pair[1].photo_id in audit_ids else 4,
                pair[0],
            ))
            for ordinal, episode in ordered_episodes:
                chapter_id = episode.photo_id
                remote_id = f"photo:{chapter_id}"
                old = cached.get(remote_id)
                health = healths[chapter_id]
                chapter = chapters.get(chapter_id) or {"status": "pending", "attempt_count": 0}
                desired = jm_incremental.merge_album_metadata(old, identities[remote_id]) if old else None
                metadata_changed = old is not None and desired["metadata"] != old["metadata"]
                status = chapter.get("status")
                if health["suppressed"] or status == "suppressed":
                    if not context.dry_run and status != "suppressed":
                        if status == "failed":
                            comic_sync_state.mark_chapter_pending(database, **scope, chapter_id=chapter_id, reason="lifecycle_suppressed")
                        suppression = comic_sync_state.claim_chapter(database, **scope, chapter_id=chapter_id, owner=owner, generation=state["generation"])
                        if suppression is not None:
                            comic_sync_state.finish_chapter(database, **scope, chapter_id=chapter_id, token=suppression["token"], generation=suppression["generation"], status="suppressed", verified_remote=False)
                    summary["chapters_suppressed"] += 1
                    continue
                resumable = status in {"paused", "running"} and int(chapter.get("attempt_count", 0)) > 0 and chapter.get("last_error_code") in {None, "sync_budget_exhausted"}
                known_failed = status == "failed" or (not resumable and old is not None and old.get("status") in {"failed", "partial", "downloading"})
                needs_repair = old is not None and not health["complete"]
                if (known_failed and not retry) or (needs_repair and not repair and not resumable) or (old is not None and state.get("revalidation_active") and status not in {"succeeded", "suppressed"} and (not repair or not retry) and not resumable):
                    summary["chapters_skipped_no_repair"] += 1
                    summary["chapters_pending"] += 1
                    continue
                next_attempt = chapter.get("next_attempt_at")
                if status == "failed" and next_attempt and datetime.fromisoformat(next_attempt) > now and not revalidate:
                    summary["chapters_failed_deferred"] += 1
                    summary["chapters_pending"] += 1
                    continue
                audit = chapter_id in audit_ids
                full_check = bool(state.get("revalidation_active")) and status not in {"succeeded", "suppressed"}
                needs_remote = old is None or status not in {"succeeded", "suppressed"} or audit or full_check or bool(input_data.get("overwrite"))
                previous_chapter = previous_chapters.get(chapter_id)
                current_fingerprint = fingerprints[chapter_id]
                remote_manifest_changed = previous_chapter is not None and previous_chapter["fingerprint"] != current_fingerprint
                local_pending = status == "pending" and bool(chapter.get("adoption_pending"))
                local_only = old is not None and health["pages_usable"] and not audit and not full_check and not remote_manifest_changed and not input_data.get("overwrite") and (needs_repair or metadata_changed) and (status == "succeeded" or local_pending)
                if remote_manifest_changed:
                    needs_remote = True
                elif local_only:
                    needs_remote = False
                if not needs_remote and not needs_repair and not metadata_changed:
                    summary["chapters_healthy"] += 1
                    continue
                if summary["chapters_processed"] >= max_chapters or time.monotonic() >= deadline:
                    exhausted = True
                    summary["chapters_pending"] += 1
                    continue
                lease = None
                if not context.dry_run:
                    if status in {"succeeded", "failed"}:
                        comic_sync_state.mark_chapter_pending(database, **scope, chapter_id=chapter_id, reason="revalidation" if audit or full_check else "local_repair")
                    lease = comic_sync_state.claim_chapter(database, **scope, chapter_id=chapter_id, owner=owner, generation=state["generation"], lease_seconds=max(900, int(budget) + 60))
                    if lease is None:
                        summary["chapters_pending"] += 1
                        continue
                summary["chapters_processed"] += 1
                try:
                    if audit or full_check:
                        health = jm_incremental.local_health(old, library, deep=True)
                        summary["filesystem_checks"] += health.get("filesystem_checks", 0)
                        needs_repair = old is not None and not health["complete"]
                        if not context.dry_run:
                            for bad in health["bad_files"]:
                                db.update_media_file_health(database, file_id=int(bad["id"]), file_health=str(bad["file_health"]))
                    if needs_remote:
                        if scramble_id is None:
                            summary["scramble_requested"] += 1
                            scramble_id = recovery.run(lambda: client.get_scramble_id(chapter_id))
                        if time.monotonic() >= deadline:
                            exhausted = True
                            summary["chapters_pending"] += 1
                            if lease is not None:
                                comic_sync_state.finish_chapter(database, **scope, chapter_id=chapter_id, token=lease["token"], generation=lease["generation"], status="paused", error_code="sync_budget_exhausted", verified_remote=False)
                            continue
                        summary["chapters_requested"] += 1
                        photo = recovery.run(lambda: client.get_photo(chapter_id, scramble_id=scramble_id))
                        item = JMComicResolution(link=parse_jmcomic_link(f"https://18comic.vip/album/{album.album_id}/"), album=album, photos=(photo,)).normalized_items()[0]
                    else:
                        item = desired
                        summary["chapters_local_repair"] += int(needs_repair)
                        summary["chapters_metadata_refresh"] += int(metadata_changed)
                    chapter_options = {**options, "_refresh_comic_metadata": metadata_changed or (old is not None and status != "succeeded"), "_rebuild_comic_package": needs_repair and health.get("pages_usable", False)}
                    result = await _sync_items(context, chapter_options, [item])
                    results.extend(result.data.get("items") or [])
                    packages.extend(result.data.get("packages") or [])
                    artifacts.extend(result.artifacts)
                    warnings.extend(result.warnings)
                    for key, value in (result.data.get("summary") or {}).items():
                        if isinstance(value, int) and not isinstance(value, bool):
                            summary[key] = summary.get(key, 0) + value
                    paused = bool(result.data.get("budget_exhausted"))
                    if lease is not None:
                        committed = comic_sync_state.finish_chapter(database, **scope, chapter_id=chapter_id, token=lease["token"], generation=lease["generation"], status="paused" if paused else "succeeded" if result.is_success else "failed", error_code=None if result.is_success else result.error.code, verified_remote=needs_remote)
                        if not committed:
                            raise ValueError("JMComic chapter lease changed before completion.")
                    if paused:
                        exhausted = True
                        summary["chapters_pending"] += 1
                    elif not result.is_success:
                        failures.append({"target_type": "photo", "target_id": chapter_id, "code": result.error.code, "category": result.error.category.value})
                        album_failed = True
                    elif audit:
                        summary["chapters_audited"] += 1
                        audited_positions.append(ordinal)
                except Exception as exc:
                    failed = _provider_failure(exc)
                    if lease is not None:
                        comic_sync_state.finish_chapter(database, **scope, chapter_id=chapter_id, token=lease["token"], generation=lease["generation"], status="failed", error_code=failed.error.code)
                    failures.append(_target_failure({"target_type": "photo", "target_id": chapter_id}, failed))
                    album_failed = True
                    if failed.error.category in {ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT}:
                        raise
            if audited_positions and not context.dry_run:
                comic_sync_state.set_audit_cursor(database, **scope, cursor=(max(audited_positions) + 1) % len(album.episodes))
            summary["targets_processed"] += 1
            summary["targets_failed"] += int(album_failed)
        except Exception as exc:
            failed = _provider_failure(exc)
            failures.append(_target_failure(target, failed))
            summary["targets_failed"] += 1
            if failed.error.category in {ErrorCategory.AUTH, ErrorCategory.RATE_LIMIT}:
                break
    summary["targets_unprocessed"] = len(targets) - summary["targets_attempted"]
    final_states = [comic_sync_state.read_album(database, provider="jmcomic", account_key=account_key, album_id=str(target["target_id"])) for target in targets] if not context.dry_run else []
    summary["revalidation_albums_active"] = sum(bool(row and row["revalidation_active"]) for row in final_states)
    summary["albums_pending"] = summary["targets_unprocessed"]
    if not context.dry_run:
        summary["chapters_pending"] = sum(chapter["status"] not in {"succeeded", "suppressed"} for row in final_states if row for chapter in row["chapters"] if chapter["active"])
        summary["albums_pending"] = sum(bool(row and (row["revalidation_active"] or any(chapter["active"] and chapter["status"] not in {"succeeded", "suppressed"} for chapter in row["chapters"]))) for row in final_states) + sum(row is None for row in final_states)
    summary["provider_api_requests"] = summary["albums_requested"] + summary["chapters_requested"] + summary["scramble_requested"]
    data = {"dry_run": context.dry_run, "db_path": str(database), "library_root": str(library), "summary": summary, "items": results, "packages": packages, "target_failures": failures, "budget_exhausted": exhausted, "revalidation_active": bool(summary["revalidation_albums_active"])}
    if failures:
        return ToolResult.failure("comic_favorites_sync_partial", "Comic favorites sync retained chapter progress with failed work remaining.", data=data, category=ErrorCategory.NETWORK, warnings=warnings)
    return ToolResult.success(data, artifacts=artifacts, warnings=warnings)


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
        folders = _resolve_jm_favorite_folders_with_discovery(context, input_data, client, recovery)
        targets, folder_snapshots = _collect_jm_favorite_folder_targets(client, recovery, folders)
        return ToolResult.success(
            {
                "provider": "jmcomic",
                "collection": "favorites",
                "complete": True,
                "pages_fetched": sum(folder["pages_fetched"] for folder in folder_snapshots),
                "expected_total": len(targets),
                "favorites_seen": len(targets),
                "folder_memberships_seen": sum(folder["favorites_seen"] for folder in folder_snapshots),
                "folders": folder_snapshots,
                "target_policy": "series_and_follow",
                "following": len(targets),
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
                "asset_ids": [],
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
    progress = ProgressLogger(context.operation_log)
    failed_items = 0
    for item in candidates:
        item_result = await link_tools._sync_one_link_item(context, db_path, item, input_data)
        item_results.append(item_result)
        artifacts.extend({"type": "file", "path": path} for path in item_result["artifacts"])
        if item_result["status"] in {"failed", "partial"}:
            failed_items += 1
        progress.report(
            completed=len(item_results),
            pending=len(candidates) - len(item_results),
            failed=failed_items,
        )
    budget_exhausted = any(result.get("budget_exhausted") for result in item_results)
    paused_identities = {(str(result["platform"]), str(result["remote_id"])) for result in item_results if result.get("budget_exhausted")}
    packages = []
    refreshed_identities = {(item["platform"], item["remote_id"]) for item in (items if input_data.get("_refresh_comic_metadata") else candidates)}
    for item in _load_comic_items(db_path, {(item["platform"], item["remote_id"]) for item in items}):
        if (item["platform"], item["remote_id"]) in paused_identities:
            continue
        plan = _comic_package_plan(
            item=item,
            library_root=library_root,
            include_platform_layer=bool(input_data.get("include_platform_layer", True)),
            overwrite=bool(input_data.get("overwrite") or input_data.get("_rebuild_comic_package")),
            migrate_legacy=False,
            refresh_tracked=(item["platform"], item["remote_id"]) in refreshed_identities,
            preserve_tracked_path=bool(input_data.get("_incremental_sync") and item["platform"] == "jmcomic"),
        )
        if plan["status"] == "ready":
            try:
                package = _apply_comic_package(db_path=db_path, item=item, plan=plan, library_root=library_root)
                if package.get("suppressed"):
                    packages.append(
                        {
                            **_public_package_plan(plan),
                            "status": "skipped",
                            "reason": package["reason"],
                        }
                    )
                    continue
                packages.append({**_public_package_plan(plan), "status": "packaged", **package})
                artifacts.append({"type": "file", "path": package["target_path"]})
            except Exception as exc:
                packages.append({**_public_package_plan(plan), "status": "failed", "error": {"code": "comic_package_failed", "exception_type": type(exc).__name__}})
        else:
            packages.append(_public_package_plan(plan))
    downloaded = sum(result["status"] == "downloaded" for result in item_results)
    skipped_items = sum(result["status"] == "skipped" for result in item_results)
    failed = sum(result["status"] == "failed" for result in item_results)
    partial = sum(result["status"] == "partial" and not result.get("budget_exhausted") for result in item_results)
    files_skipped = sum(int(result.get("files_skipped", 0)) for result in item_results)
    package_failed = sum(package["status"] in {"failed", "blocked", "incomplete"} for package in packages)
    summary = {
        "resolved_items": len(items),
        "queued": len(candidates),
        "downloaded": downloaded,
        "skipped": skipped_items,
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
    data = {
        "dry_run": False,
        "db_path": str(db_path),
        "library_root": str(library_root),
        "asset_ids": assets.asset_ids_for_media_items(db_path, items),
        "summary": summary,
        "items": item_results,
        "packages": packages,
        "budget_exhausted": budget_exhausted,
    }
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
                    f"""
                    SELECT mf.id, mf.media_item_id, mf.file_key, mf.remote_url,
                           mf.local_path, mf.mime_type, mf.size_bytes, mf.checksum,
                           mf.status, mf.library_relative_path, mf.storage_layout,
                           mf.file_health, mf.source_timestamp, mf.verified_at,
                           mf.library_entry_id, le.state AS library_state,
                           le.trash_path AS library_trash_path,
                           le.display_name_override
                    FROM media_files mf
                    LEFT JOIN library_entries le ON le.id = mf.library_entry_id
                    WHERE mf.media_item_id IN ({placeholders})
                    ORDER BY mf.id
                    """,
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


def _jm_account_collection_key(context: ToolContext, client: JMComicClient | None = None) -> str:
    config = jm_auth.load_config(env=context.env, cwd=context.cwd)
    account_id = config.username or (client.session.username if client is not None else None)
    if not account_id:
        try:
            account_id = jm_auth.load_session(env=context.env, cwd=context.cwd).username
        except jm_auth.JMComicAuthError:
            account_id = None
    return _favorites_collection_key(account_id)


def _resolve_jm_favorite_folders(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    remote_folders: list[JMComicFavoriteFolder] | None = None,
) -> list[JMComicFavoriteFolder]:
    selectors = input_data.get("folders")
    if selectors is None:
        configured = str(context.env.get(FOLDERS_ENV) or "").strip()
        if configured:
            try:
                decoded = json.loads(configured)
            except json.JSONDecodeError:
                selectors = [part.strip() for part in configured.split(",") if part.strip()]
            else:
                selectors = decoded
        else:
            selectors = [ALL_FOLDER_NAME]
    if not isinstance(selectors, list) or not selectors:
        raise JMComicFavoriteFolderError(
            "jmcomic_folders_empty",
            "Provide at least one JMComic favorite folder; an empty selection is rejected to avoid stopping all follow accidentally.",
        )
    if any(not isinstance(selector, str) for selector in selectors):
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_selector_invalid",
            "JMComic favorite folder selectors must be names, numeric IDs, or trusted folder URLs.",
        )

    account_key = _jm_account_collection_key(context)
    db_path = _required_db_path(context, input_data)
    if isinstance(db_path, ToolResult):
        db_path = None
    aliases = (
        db.list_collection_scope_aliases(
            db_path,
            provider="jmcomic",
            account_key=account_key,
            scope_kind="favorite_folder",
        )
        if db_path is not None
        else []
    )
    aliases_by_name = {str(alias["name"]).casefold(): alias for alias in aliases}
    aliases_by_id = {str(alias["remote_id"]): alias for alias in aliases}
    remote_by_name: dict[str, JMComicFavoriteFolder] = {}
    ambiguous_remote_names: set[str] = set()
    for remote_folder in remote_folders or []:
        name_key = remote_folder.name.casefold()
        existing = remote_by_name.get(name_key)
        if existing is not None and existing.folder_id != remote_folder.folder_id:
            ambiguous_remote_names.add(name_key)
            continue
        remote_by_name[name_key] = remote_folder
    resolved: list[JMComicFavoriteFolder] = []
    seen_ids: set[str] = set()
    for selector in selectors:
        text = selector.strip()
        if not text:
            raise JMComicFavoriteFolderError(
                "jmcomic_folder_selector_invalid",
                "JMComic favorite folder selectors must not be empty.",
            )
        if text.casefold() in ALL_FOLDER_ALIASES:
            folder = all_folder()
        elif text.isdigit() or "://" in text:
            folder_id, canonical_url = parse_folder_locator(text)
            alias = aliases_by_id.get(folder_id)
            folder = JMComicFavoriteFolder(
                str(alias["name"]) if alias else (ALL_FOLDER_NAME if folder_id == "0" else f"folder:{folder_id}"),
                folder_id,
                canonical_url or (str(alias["url"]) if alias and alias.get("url") else None),
            )
        else:
            name_key = text.casefold()
            if name_key in ambiguous_remote_names:
                raise JMComicFavoriteFolderError(
                    "jmcomic_folder_name_ambiguous",
                    f"JMComic returned more than one favorite folder named: {text}",
                )
            remote_folder = remote_by_name.get(name_key)
            alias = aliases_by_name.get(name_key)
            if remote_folder is not None:
                folder = remote_folder
            elif alias is None:
                raise JMComicFavoriteFolderError(
                    "jmcomic_folder_unknown",
                    f"JMComic favorite folder name is not registered: {text}",
                ) from None
            else:
                folder = JMComicFavoriteFolder(
                    str(alias["name"]),
                    str(alias["remote_id"]),
                    str(alias["url"]) if alias.get("url") else None,
                )
        if folder.folder_id in seen_ids:
            continue
        seen_ids.add(folder.folder_id)
        resolved.append(folder)
    return resolved


def _resolve_jm_favorite_folders_with_discovery(
    context: ToolContext,
    input_data: dict[str, Any],
    client: JMComicClient,
    recovery: "_JMAuthRecovery",
) -> list[JMComicFavoriteFolder]:
    try:
        return _resolve_jm_favorite_folders(context, input_data)
    except JMComicFavoriteFolderError as exc:
        if exc.code != "jmcomic_folder_unknown":
            raise
    remote_folders = recovery.run(lambda: _remote_jm_favorite_folders(client))
    return _resolve_jm_favorite_folders(context, input_data, remote_folders=remote_folders)


def _remote_jm_favorite_folders(client: JMComicClient) -> list[JMComicFavoriteFolder]:
    page = client.get_favorites_page(page=1, folder_id="0")
    return [all_folder(), *parse_remote_folder_list(list(page.folders))]


def _collect_jm_favorite_folder_targets(
    client: JMComicClient,
    recovery: "_JMAuthRecovery",
    folders: list[JMComicFavoriteFolder],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    favorites_by_id: dict[str, Any] = {}
    membership_by_id: dict[str, list[dict[str, str | None]]] = {}
    folder_snapshots: list[dict[str, Any]] = []
    for folder in folders:
        collection = recovery.run(lambda folder_id=folder.folder_id: client.collect_favorites(folder_id=folder_id))
        folder_snapshots.append(
            {
                **folder.public_dict(),
                "complete": True,
                "pages_fetched": collection.pages_fetched,
                "favorites_seen": len(collection.items),
            }
        )
        for favorite in collection.items:
            favorites_by_id.setdefault(favorite.album_id, favorite)
            membership_by_id.setdefault(favorite.album_id, []).append(folder.public_dict())
    targets = []
    for album_id, favorite in favorites_by_id.items():
        targets.append(
            {
                "target_type": "album",
                "target_id": album_id,
                "metadata": {
                    "provider_work_id": favorite.provider_work_id,
                    "title": favorite.title,
                    "latest_photo_id": favorite.latest_photo_id,
                    "favorite_folders": membership_by_id[album_id],
                },
            }
        )
    return targets, folder_snapshots


def _commit_or_preview_snapshot(
    context: ToolContext,
    input_data: dict[str, Any],
    provider: str,
    collection_key: str,
    targets: list[dict[str, Any]],
    policy: str,
    metadata: dict[str, Any] | None = None,
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
        metadata={"policy": policy, **(metadata or {})},
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
