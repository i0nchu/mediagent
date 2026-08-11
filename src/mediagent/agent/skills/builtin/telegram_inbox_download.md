---
name: telegram_inbox_download
description: Download, sync, preview, or full-scan media links from the configured Telegram inbox workflow.
allowed_tools:
  - telegram.auth.status
  - telegram.inbox.collect_links
  - telegram.inbox.sync_links
default_dry_run: false
risk_level: write_files_network
requires_initial_tool_call: true
supports_unbounded: true
supported_intents:
  - sync new media links from the selected Telegram inbox workflow
  - download all downloadable media links from the selected Telegram inbox workflow
  - full scan the configured Telegram inbox workflow, including t.me and telegram.me message links
  - preview what the configured Telegram inbox workflow would download
  - inspect Telegram inbox link batches when explicitly requested
unsupported_intents:
  - arbitrary Telegram chat scanning outside the configured inbox workflow
  - checking whether a Telegram inbox exists
  - sending or forwarding messages
---

## When To Use

Use this skill when the user asks to download, sync, fetch, preview, inspect, process, or full-scan media links from the selected Telegram inbox workflow.

The configured inbox workflow is the safe Telegram boundary. A request to fully scan, completely scan, or scan all content in "telegram inbox" means the configured inbox workflow, not arbitrary Telegram crawling.

Do not use this skill for arbitrary Telegram chat scanning, sending messages, forwarding messages, editing messages, or Telegram media that is not from the selected inbox workflow.

Do not use this skill to answer whether an inbox exists or how it is configured. Agent Core V1 has no Telegram inbox configuration inspection tool.

## Inputs The User May Provide

The user may provide a message limit, dry-run or execute intent, sidecar metadata preference, retry behavior, repair behavior, or full-source intent.

If the user asks for all, complete, or until-exhausted inbox processing, call the sync tool in full-source mode. Do not invent a numeric `limit` or `max_messages` for an "all" task. The tool layer handles URL dedupe, media item dedupe, downloaded-state dedupe, and filesystem safety.

If the user does not provide values, use conservative defaults:

- omit `chat`/`chats` so the tool can use `MEDIAGENT_TELEGRAM_INBOX_*`; only pass `chat` when the user explicitly names an inbox selector
- omit `limit` unless the user explicitly asks for a link count
- use `full_sync`: true when the user asks for all/complete/until-exhausted inbox media
- use `store_cursor`: false for full-source rebuild tasks; recurring update tasks may omit it so the tool can store cursors
- `write_sidecar_metadata`: false
- `retry_failed`: false
- `retry_auth_skipped`: false; set true only after a downstream platform session becomes usable and the user wants old auth-dependent skips retried
- `repair_missing_files`: false

## Tool Calling Strategy

Use `telegram.inbox.sync_links` for the normal inbox download workflow.

Use `telegram.inbox.collect_links` only when the user explicitly asks to inspect links without resolving or downloading media.

Use `telegram.auth.status` only when a previous tool result reports missing, invalid, or expired Telegram credentials.

## Example Dry-Run Action

```json
{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"full_sync":true,"store_cursor":false,"write_sidecar_metadata":false,"retry_failed":false,"retry_auth_skipped":false,"repair_missing_files":false},"dry_run":true,"reason":"Preview all downloadable media links from the configured Telegram inbox."}
```

## Example Execute Action

```json
{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"full_sync":true,"store_cursor":false,"write_sidecar_metadata":false,"retry_failed":false,"retry_auth_skipped":false,"repair_missing_files":false},"dry_run":false,"reason":"The user requested all downloadable inbox media, so the inbox sync may scan the configured inbox history and update state."}
```

## Common Errors

- `telegram_missing_credentials`: ask the user to complete Telegram login.
- `missing_telegram_inbox_chat`: ask the user to set `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`, `MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`, or provide an explicit chat selector.
- `requires_auth`: explain which downstream platform requires authentication.
- `unsafe_url`: report that a link was rejected by URL safety policy.
- `target_conflict`: explain that a target file already exists outside known Mediagent state.
- `rate_limited`: suggest waiting before retrying.
- `link_media_sync_failed`: summarize failed items and skip reasons.

## Final Summary

Summarize links considered, links resolved, files planned or downloaded, skipped links, important skip reasons, and artifact paths when available.
