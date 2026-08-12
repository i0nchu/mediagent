---
name: pixiv_bookmark_sync
description: Sync Pixiv bookmarks through the existing Pixiv foundation tools.
allowed_tools:
  - pixiv.auth.status
  - pixiv.auth.refresh
  - pixiv.bookmarks.sync
  - pixiv.comics.package
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - sync Pixiv bookmarks with an explicit bounded limit
  - sync newly added Pixiv bookmarks with stop-on-known scanning
  - download all downloadable media from Pixiv bookmarks
  - download Pixiv bookmark media with explicit limit or max_pages
  - check or refresh Pixiv authentication when needed
unsupported_intents:
  - arbitrary Pixiv crawling
  - rankings or search
  - following-list downloads
---

## When To Use

Use this skill when the user asks to sync or download Pixiv bookmarks.

Do not use this skill for arbitrary Pixiv crawling, following lists, rankings, search, or non-bookmark sources.

## Inputs The User May Provide

The user may provide a limit, max pages, public/private restrict value, tag, sidecar metadata preference, retry behavior, stop-on-known behavior, full-source intent, or execute intent.

`limit` means bookmark item count, not downloaded file count. A single Pixiv artwork can produce many downloaded files when it has multiple pages or animation metadata.

If the user asks for all bookmarks, complete sync, or until-exhausted bookmark download, call `pixiv.bookmarks.sync` with `full_sync:true`, without `limit` or `max_pages`, set `stop_on_known:false`, and usually set `store_cursor:false`. The tool layer handles downloaded-state dedupe and filesystem safety.

Conservative defaults:

- `restrict`: `public`
- for recurring update tasks: `max_pages`: 5 and `stop_on_known`: true
- for full-source tasks: use `full_sync`: true, omit `max_pages`, omit `limit`, and use `stop_on_known`: false
- `include_ugoira_metadata`: true
- `write_sidecar_metadata`: false
- `retry_failed`: false
- `package_comics`: true

## Tool Calling Strategy

Use `pixiv.bookmarks.sync` with `package_comics:true` for the normal workflow so new official Pixiv manga are also packaged as CBZ. Use `pixiv.comics.package` to package already-downloaded legacy manga without downloading them again.

For recurring sync/update tasks, call `pixiv.bookmarks.sync` with `stop_on_known:true`, a bounded `max_pages`, and no invented `limit`. This scans from the newest bookmarks and stops when it reaches an already known terminal item.

When the user gives a count such as "first 50 bookmark items", pass that value as `limit` and explain in the final summary that actual files may exceed the item limit.

Do not use `max_pages` to reinterpret an unbounded "all" request. For full-source tasks, let the tool paginate until the Pixiv bookmark feed ends.

Use `pixiv.auth.status` only when the user asks to check credentials or a sync result reports an auth problem.

Use `pixiv.auth.refresh` only when an auth result clearly reports an expired session and refresh is available.

## Example Dry-Run Action

```json
{"action":"call_tool","tool":"pixiv.bookmarks.sync","input":{"restrict":"public","full_sync":true,"stop_on_known":false,"store_cursor":false,"include_ugoira_metadata":true,"write_sidecar_metadata":false,"retry_failed":false,"package_comics":true},"dry_run":true,"reason":"Preview all downloadable Pixiv bookmark media and comic packages before writing files."}
```

## Common Errors

- `pixiv_auth_missing_credentials`: ask the user to run Pixiv login.
- `pixiv_auth_expired`: refresh credentials if available, otherwise ask the user to log in again.
- `rate_limited`: suggest waiting before retrying.

## Final Summary

Report items collected, new items, downloaded or planned files, skipped or failed items, and artifact paths when available.
