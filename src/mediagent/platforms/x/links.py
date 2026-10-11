"""Exact X post resolver bridge into the managed link pipeline."""

from __future__ import annotations

from typing import Any

from mediagent.core.links import (
    ResolveRequest,
    Resolver,
    ResolverSpec,
    SafeURL,
    skipped_resolution,
)
from mediagent.platforms.x import fxembed


class XPostResolver(Resolver):
    spec = ResolverSpec(
        name="x_exact_post",
        allowed_domains=tuple(sorted(fxembed.X_HOSTS)),
        matching_rules="Exact X/Twitter status URLs, including photo/video links; all native attachments.",
        supports_auth=True,
        max_media_items=4,
    )

    def matches(self, safe_url: SafeURL) -> bool:
        # Own the domain so unsupported pages never fall through to preview HTML.
        return safe_url.host in fxembed.X_HOSTS

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        link = fxembed.parse_post_link(safe_url.normalized_url)
        if link is None:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="x_url_unsupported",
                origin_source="x",
                details={"retryable": False, "user_action_required": False},
            )
        try:
            post = fxembed.resolve_post(
                link,
                env=request.env or {},
                http_client=request.http_client,
                host_resolver=request.host_resolver,
                timeout=request.policy.timeout_seconds,
            )
        except fxembed.XEndpointError as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason=exc.code,
                origin_source="x",
                remote_id=link.post_id,
                details={
                    "error_code": exc.code,
                    "error_category": exc.category,
                    "status_code": exc.status_code,
                    "tool_failure": True,
                    "retryable": exc.retryable,
                    "user_action_required": exc.category in {"auth", "validation"},
                },
            )
        candidates = post["candidates"]
        metadata = post["metadata"]
        first = candidates[0]
        return {
            "status": "resolved",
            "original_url": safe_url.original_url,
            "normalized_url": safe_url.normalized_url,
            "canonical_url": link.canonical_url,
            "source_url": link.canonical_url,
            "aliases": [
                {"kind": "canonical", "url": link.canonical_url},
                {"kind": "url", "url": safe_url.normalized_url},
            ],
            "resolver": self.spec.name,
            "origin_source": "x",
            "remote_id": link.post_id,
            "media_type": first["media_type"],
            "mime_type": first["mime_type"],
            "extension": first["extension"],
            "size_bytes": None,
            "resolved_media_url": first["url"],
            "media_count": len(candidates),
            "media_candidates": candidates,
            "selected_candidate": first,
            "warnings": [],
            "source_timestamp": metadata["source_timestamp"],
            "details": {
                "x": metadata,
                "validation": "fxembed_v2",
                "post_scope": "all_resources",
            },
        }
