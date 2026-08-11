---
name: instagram_saved_sync
description: Sync media from the configured Instagram saved collection.
allowed_tools:
  - instagram.auth.status
  - instagram.auth.ensure_session
  - instagram.saved.collect
  - instagram.saved.sync
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - sync newly saved Instagram media
  - sync Instagram saved media with an explicit limit or max_pages
  - download all saved Instagram media
  - retry failed or repair missing Instagram saved files
unsupported_intents:
  - download an explicit Instagram post or Reel URL
  - arbitrary Instagram profile crawling
  - Instagram search or hashtag crawling
---

## When To Use

Use this skill only for the configured account's saved-media feed. Explicit post and Reel URLs belong to `instagram_link_download`.

One saved post is one item; carousels may produce multiple required files.

For recurring updates, call `instagram.saved.sync` with `stop_on_known:true`, a conservative `max_pages`, and no invented item limit. For “all saved media,” use `full_sync:true`, `stop_on_known:false`, omit `limit` and `max_pages`, and normally set `store_cursor:false`. Never reinterpret an unbounded request as a bounded request.

Use `retry_failed:true` for recorded failures and `repair_missing_files:true` when downloaded records may have missing files. Authentication repair must remain bounded; do not loop login attempts.

Example full sync:

```json
{"action":"call_tool","tool":"instagram.saved.sync","input":{"full_sync":true,"stop_on_known":false,"store_cursor":false,"write_sidecar_metadata":false},"dry_run":false,"reason":"Download all saved Instagram posts without an invented bound."}
```

Report collected posts, queued/downloaded/partial/failed items, files, bytes, and whether a cursor was safely stored.
