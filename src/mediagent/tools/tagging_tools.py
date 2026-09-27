"""Automated metadata tagging tools and post-intake finalization."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from mediagent.agent.llm import build_llm_client
from mediagent.agent.metadata_tagger import (
    MAX_SOURCES,
    PROMPT_VERSION,
    TAGGER_VERSION,
    MetadataTaggingError,
    build_metadata_snapshot,
    metadata_fingerprint,
    process_metadata_tagging,
)
from mediagent.core import asset_tagging_jobs, assets, db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, resolve_placeholders
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


AUTO_TAG_ENV = "MEDIAGENT_AUTO_TAG"
AUTO_TAG_BATCH_ENV = "MEDIAGENT_AUTO_TAG_BATCH_SIZE"
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 100
JOB_CONTRACT_VERSION = f"{TAGGER_VERSION}/{PROMPT_VERSION}"


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="library.asset.tags.auto",
                description="Generate equal-priority tags from safe Asset metadata using the configured LLM.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "asset_ids": {"type": "array", "items": {"type": "string"}},
                        "force": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_BATCH_SIZE},
                        "db_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_CREDENTIALS,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.NETWORK,
                ),
                dry_run_supported=False,
                hidden=True,
            ),
            handler=auto_tag_assets,
        )
    ]


async def auto_tag_assets(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _db_path(context, input_data)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        asset_ids_value = input_data.get("asset_ids")
        asset_ids = _unique_asset_ids(asset_ids_value) if asset_ids_value is not None else None
        outcome = await asyncio.to_thread(
            run_auto_tagging,
            context,
            resolved,
            asset_ids=asset_ids,
            enqueue_assets=asset_ids is not None,
            force=bool(input_data.get("force", False)),
            limit=_batch_size(context.env, input_data.get("limit")),
        )
    except ValueError as exc:
        return ToolResult.failure("invalid_auto_tag_request", str(exc), category=ErrorCategory.VALIDATION)
    except (OSError, sqlite3.Error) as exc:
        return ToolResult.failure(
            "asset_auto_tag_failed",
            "Automated Asset tagging could not update its durable state.",
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.DATABASE,
        )
    warnings = _outcome_warnings(outcome)
    if outcome["failed"]:
        return ToolResult.failure(
            "asset_auto_tag_partial",
            "Automated Asset tagging completed with one or more failed jobs.",
            data={"tagging": outcome},
            warnings=warnings,
            category=ErrorCategory.NETWORK,
        )
    if outcome["configuration_error"]:
        return ToolResult.failure(
            "invalid_llm_config",
            "Automated Asset tagging could not start with the configured LLM.",
            data={"tagging": outcome},
            warnings=warnings,
            category=ErrorCategory.VALIDATION,
        )
    return ToolResult.success({"tagging": outcome}, warnings=warnings)


async def finalize_add_result(
    context: ToolContext,
    input_data: dict[str, Any],
    result: ToolResult,
) -> ToolResult:
    """Best-effort tag Assets returned by a successful or partial intake.

    Tagging is enrichment. Its failures are durable and visible, but never
    replace the primary download/import result.
    """

    asset_ids = _unique_asset_ids(result.data.get("asset_ids"))
    try:
        requested = _auto_tag_requested(context.env, input_data.get("auto_tag"))
    except ValueError:
        result.data["tagging"] = {
            **_empty_outcome(enabled=False, requested=len(asset_ids)),
            "configuration_error": True,
        }
        result.warnings.append("Automated tagging was deferred; check MEDIAGENT_AUTO_TAG and retry.")
        return result
    disabled = _empty_outcome(enabled=False, requested=len(asset_ids))
    if not requested or context.dry_run or not asset_ids:
        disabled["enabled"] = requested and not context.dry_run
        result.data["tagging"] = disabled
        return result

    resolved = _db_path(context, input_data)
    if isinstance(resolved, ToolResult):
        result.data["tagging"] = {
            **_empty_outcome(enabled=True, requested=len(asset_ids)),
            "configuration_error": True,
        }
        result.warnings.append("Automated tagging was deferred because the database path is unavailable.")
        return result
    try:
        limit = _batch_size(context.env, None)
    except ValueError:
        try:
            outcome = await asyncio.to_thread(
                _enqueue_deferred_jobs,
                context,
                resolved,
                asset_ids,
            )
        except Exception:
            outcome = {
                **_empty_outcome(enabled=True, requested=len(asset_ids)),
                "deferred": len(asset_ids),
                "configuration_error": True,
            }
        result.data["tagging"] = outcome
        result.warnings.extend(_outcome_warnings(outcome))
        return result
    try:
        outcome = await asyncio.to_thread(
            run_auto_tagging,
            context,
            resolved,
            asset_ids=asset_ids,
            enqueue_assets=True,
            force=False,
            limit=limit,
        )
    except Exception:
        outcome = {
            **_empty_outcome(enabled=True, requested=len(asset_ids)),
            "deferred": len(asset_ids),
            "configuration_error": True,
        }
    result.data["tagging"] = outcome
    result.warnings.extend(_outcome_warnings(outcome))
    return result


def _enqueue_deferred_jobs(
    context: ToolContext,
    db_path: Path,
    asset_ids: list[str],
) -> dict[str, Any]:
    """Persist retryable work even when the batch runner is misconfigured."""

    provider, model = _llm_contract(context.env)
    outcome = _empty_outcome(enabled=True, requested=len(asset_ids))
    outcome["configuration_error"] = True
    for asset_id in asset_ids:
        snapshot = _snapshot_for_asset(db_path, asset_id)
        job = asset_tagging_jobs.enqueue(
            db_path,
            asset_id=asset_id,
            input_fingerprint=metadata_fingerprint(snapshot),
            provider=provider,
            model=model,
            prompt_version=JOB_CONTRACT_VERSION,
        )
        if job["status"] == "succeeded":
            outcome["unchanged"] += 1
        else:
            outcome["enqueued"] += 1
            outcome["deferred"] += 1
    return outcome


def run_auto_tagging(
    context: ToolContext,
    db_path: Path,
    *,
    asset_ids: list[str] | None,
    enqueue_assets: bool,
    force: bool,
    limit: int,
) -> dict[str, Any]:
    provider, model = _llm_contract(context.env)
    outcome = _empty_outcome(enabled=True, requested=len(asset_ids or []))
    work_asset_ids: list[str] | None = None
    if enqueue_assets:
        work_asset_ids = []
        for asset_id in asset_ids or []:
            snapshot = _snapshot_for_asset(db_path, asset_id)
            job = asset_tagging_jobs.enqueue(
                db_path,
                asset_id=asset_id,
                input_fingerprint=metadata_fingerprint(snapshot),
                provider=provider,
                model=model,
                prompt_version=JOB_CONTRACT_VERSION,
                force=force,
            )
            work_asset_ids.append(str(job["asset_id"]))
            if job["status"] == "succeeded":
                outcome["unchanged"] += 1
            else:
                outcome["enqueued"] += 1

    if enqueue_assets and outcome["enqueued"] == 0:
        return outcome
    if not enqueue_assets and not asset_tagging_jobs.has_ready(db_path):
        return outcome

    try:
        llm_client = build_llm_client(context.env)
    except (TypeError, ValueError):
        outcome["configuration_error"] = True
        outcome["deferred"] += outcome["enqueued"]
        return outcome

    owner = f"mediagent-{context.run_id}-{uuid.uuid4().hex[:8]}"
    lease_seconds = _lease_seconds(context.env)
    remaining_asset_ids = list(work_asset_ids or []) if enqueue_assets else None
    for _ in range(limit):
        claimed = asset_tagging_jobs.claim(
            db_path,
            owner=owner,
            limit=1,
            lease_seconds=lease_seconds,
            asset_ids=remaining_asset_ids,
        )
        if not claimed:
            break
        job = claimed[0]
        outcome["claimed"] += 1
        if not enqueue_assets:
            outcome["requested"] += 1
        elif remaining_asset_ids is not None:
            remaining_asset_ids = [
                asset_id
                for asset_id in remaining_asset_ids
                if asset_id != str(job["asset_id"])
            ]
        try:
            snapshot = _snapshot_for_asset(db_path, str(job["asset_id"]))
            fingerprint = metadata_fingerprint(snapshot)
            if (
                fingerprint != job["input_fingerprint"]
                or provider != job["provider"]
                or model != job["model"]
                or JOB_CONTRACT_VERSION != job["prompt_version"]
            ):
                _requeue_stale_job(
                    db_path,
                    job,
                    input_fingerprint=fingerprint,
                    provider=provider,
                    model=model,
                )
                outcome["deferred"] += 1
                continue
            generated = process_metadata_tagging(llm_client, snapshot)
            if generated.fingerprint != fingerprint:
                raise MetadataTaggingError("The tagging input changed during inference.")
            with db.connect(db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                latest_snapshot = _snapshot_for_asset_connection(
                    connection, str(job["asset_id"])
                )
                latest_fingerprint = metadata_fingerprint(latest_snapshot)
                if latest_fingerprint != fingerprint:
                    asset_tagging_jobs.requeue_stale_in_connection(
                        connection,
                        job_id=str(job["id"]),
                        lease_token=str(job["lease_token"]),
                        input_fingerprint=latest_fingerprint,
                        provider=provider,
                        model=model,
                        prompt_version=JOB_CONTRACT_VERSION,
                    )
                    completed = None
                else:
                    completed = asset_tagging_jobs.complete_in_connection(
                        connection,
                        job_id=str(job["id"]),
                        lease_token=str(job["lease_token"]),
                        tags=generated.tags,
                        provider=provider,
                        model=model,
                    )
            if completed is None:
                outcome["deferred"] += 1
                continue
            outcome["tagged"] += 1
            outcome["tags_added"] += len(completed["asset"].get("tags_added") or [])
        except MetadataTaggingError:
            if _fail_job(db_path, job, error_code="invalid_response", retryable=True):
                outcome["failed"] += 1
            else:
                outcome["deferred"] += 1
        except RuntimeError as exc:
            code = "llm_timeout" if "timed out" in str(exc).casefold() else "llm_api_error"
            if _fail_job(db_path, job, error_code=code, retryable=True):
                outcome["failed"] += 1
            else:
                outcome["deferred"] += 1
            break
        except ValueError:
            if _fail_job(db_path, job, error_code="tagging_state_error", retryable=False):
                outcome["failed"] += 1
            else:
                outcome["deferred"] += 1
        except (OSError, sqlite3.Error):
            if _fail_job(db_path, job, error_code="tagging_state_error", retryable=True):
                outcome["failed"] += 1
            else:
                outcome["deferred"] += 1
            break
    if enqueue_assets:
        outcome["deferred"] += max(0, outcome["enqueued"] - outcome["claimed"])
    return outcome


def _snapshot_for_asset(db_path: Path, asset_id: str) -> dict[str, Any]:
    with db.connect(db_path) as connection:
        return _snapshot_for_asset_connection(connection, asset_id)


def _snapshot_for_asset_connection(
    connection: sqlite3.Connection,
    asset_id: str,
) -> dict[str, Any]:
    canonical_id = assets.resolve_asset_id(connection, asset_id)
    if canonical_id is None:
        raise ValueError(f"Unknown Asset: {asset_id}")
    asset = assets.get_asset(connection, canonical_id)
    rows = connection.execute(
            """
            SELECT item.platform, item.media_type, item.author_name, item.metadata_json
            FROM asset_sources source
            JOIN media_items item ON item.id = source.media_item_id
            WHERE source.asset_id = ?
            ORDER BY item.platform, item.remote_id, item.id
            LIMIT ?
            """,
            (asset["id"], MAX_SOURCES),
        ).fetchall()
    sources: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        sources.append(
            {
                "platform": row["platform"],
                "media_type": row["media_type"],
                "author_name": row["author_name"],
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    return build_metadata_snapshot(asset, sources=sources)


def _fail_job(
    db_path: Path,
    job: Mapping[str, Any],
    *,
    error_code: str,
    retryable: bool,
) -> bool:
    try:
        asset_tagging_jobs.fail(
            db_path,
            job_id=str(job["id"]),
            lease_token=str(job["lease_token"]),
            error_code=error_code,
            error="Automated tagging failed.",
            retryable=retryable,
        )
    except asset_tagging_jobs.TaggingLeaseLostError:
        return False
    return True


def _requeue_stale_job(
    db_path: Path,
    job: Mapping[str, Any],
    *,
    input_fingerprint: str,
    provider: str,
    model: str,
) -> None:
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        asset_tagging_jobs.requeue_stale_in_connection(
            connection,
            job_id=str(job["id"]),
            lease_token=str(job["lease_token"]),
            input_fingerprint=input_fingerprint,
            provider=provider,
            model=model,
            prompt_version=JOB_CONTRACT_VERSION,
        )


def _empty_outcome(*, enabled: bool, requested: int) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "requested": requested,
        "enqueued": 0,
        "claimed": 0,
        "tagged": 0,
        "tags_added": 0,
        "unchanged": 0,
        "deferred": 0,
        "failed": 0,
        "configuration_error": False,
    }


def _outcome_warnings(outcome: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if outcome.get("configuration_error"):
        warnings.append("Automated tagging was deferred; check automatic tagging and LLM settings.")
    elif int(outcome.get("deferred") or 0):
        warnings.append(f"Automated tagging deferred {int(outcome['deferred'])} Asset job(s).")
    if int(outcome.get("failed") or 0):
        warnings.append(
            f"Automated tagging failed for {int(outcome['failed'])} Asset job(s); retry state was retained."
        )
    return warnings


def _auto_tag_requested(env: Mapping[str, str], override: Any) -> bool:
    if override is not None:
        return bool(override)
    raw = str(env.get(AUTO_TAG_ENV, "false")).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(f"{AUTO_TAG_ENV} must be true or false.")


def _batch_size(env: Mapping[str, str], override: Any) -> int:
    raw = override if override is not None else env.get(AUTO_TAG_BATCH_ENV, str(DEFAULT_BATCH_SIZE))
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ValueError(f"{AUTO_TAG_BATCH_ENV} must be an integer.") from exc
    if value < 1 or value > MAX_BATCH_SIZE:
        raise ValueError(f"{AUTO_TAG_BATCH_ENV} must be between 1 and {MAX_BATCH_SIZE}.")
    return value


def _lease_seconds(env: Mapping[str, str]) -> int:
    provider = str(env.get("MEDIAGENT_LLM_PROVIDER", "ollama")).strip().casefold()
    timeout_name = (
        "MEDIAGENT_OPENAI_TIMEOUT_SECONDS"
        if provider == "openai_compatible"
        else "MEDIAGENT_OLLAMA_TIMEOUT_SECONDS"
    )
    try:
        timeout = float(str(env.get(timeout_name, "60")))
    except ValueError:
        return asset_tagging_jobs.DEFAULT_LEASE_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return asset_tagging_jobs.DEFAULT_LEASE_SECONDS
    return min(
        86_400,
        max(asset_tagging_jobs.DEFAULT_LEASE_SECONDS, math.ceil(timeout) + 60),
    )


def _llm_contract(env: Mapping[str, str]) -> tuple[str, str]:
    provider = str(env.get("MEDIAGENT_LLM_PROVIDER", "ollama")).strip().casefold() or "ollama"
    if provider == "openai_compatible":
        model = str(env.get("MEDIAGENT_OPENAI_MODEL", "qwen3-8b")).strip()
    elif provider == "ollama":
        model = str(env.get("MEDIAGENT_OLLAMA_MODEL", "qwen3:8b")).strip()
    else:
        model = "unconfigured"
    return provider, model or "unconfigured"


def _unique_asset_ids(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    if not isinstance(values, Iterable):
        raise ValueError("Asset IDs must be provided as a list.")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        asset_id = str(value or "").strip()
        if not asset_id:
            continue
        if asset_id not in seen:
            result.append(asset_id)
            seen.add(asset_id)
    return result


def _db_path(context: ToolContext, input_data: Mapping[str, Any]) -> Path | ToolResult:
    raw = input_data.get("db_path")
    if raw:
        path = Path(resolve_placeholders(str(raw), context.env)).expanduser().resolve()
    elif context.db_path is not None:
        path = context.db_path
    else:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    if not path.is_file():
        return ToolResult.failure("missing_db", "Database does not exist.", category=ErrorCategory.DATABASE)
    try:
        ensure_inside(path, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_db_path", str(exc), category=ErrorCategory.FILESYSTEM)
    return path
