---
name: explicit_link_download
description: Resolve and download explicit user-provided media links.
allowed_tools:
  - link.media.sync
default_dry_run: false
risk_level: write_files_network
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - resolve explicit user-provided media URLs
  - download explicit user-provided media URLs
  - repair missing files for explicit URLs when requested
unsupported_intents:
  - account feed crawling
  - bookmark synchronization
  - inbox scanning
  - broad web crawling
---

## When To Use

Use this skill when the user provides one or more explicit URLs and asks Mediagent to resolve, preview, download, or sync media from those URLs.

Do not use this skill for account feeds, bookmarks, inbox scanning, broad crawling, or URLs the user did not explicitly provide.

## Inputs The User May Provide

The user may provide one URL, multiple URLs, a limit, retry behavior, repair behavior, sidecar metadata preference, overwrite preference, or execute intent.

## Tool Calling Strategy

Use `link.media.sync`.

Use `url` for one URL and `urls` for multiple URLs. Preserve explicit user URLs exactly except for normal JSON string escaping.

For explicit post or gallery links, "all media in this URL" is bounded by the provided URL and may be handled by this SKILL. Do not expand from a URL into unrelated pages or account feeds.

Conservative defaults:

- `write_sidecar_metadata`: false
- `retry_failed`: false
- `repair_missing_files`: false
- `overwrite`: false

## Example Dry-Run Action

```json
{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/post/1","write_sidecar_metadata":false,"retry_failed":false,"repair_missing_files":false},"dry_run":true,"reason":"Preview the explicit media link before downloading."}
```

## Common Errors

- `requires_auth`: explain that the URL or downstream provider needs authentication.
- `unsupported_media_type`: explain that no supported photo, video, or audio media was found.
- `ambiguous_candidates`: explain that the resolver found multiple possible targets and needs a more specific URL or future multi-candidate support.
- `unsafe_url`: explain that the URL was rejected by safety policy.
- `too_large`: explain that the media exceeds the configured size limit.

## Final Summary

Report how many links were considered, resolved, downloaded or planned, skipped, and failed. Include file artifact paths when available.
