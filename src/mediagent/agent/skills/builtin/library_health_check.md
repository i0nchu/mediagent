---
name: library_health_check
description: Verify local media file health recorded in the Mediagent database.
allowed_tools:
  - library.file.verify
default_dry_run: false
risk_level: read_files_write_db
requires_initial_tool_call: false
supports_unbounded: false
supported_intents:
  - verify recorded local media file health
  - audit missing or corrupt library files
  - summarize database-backed file health
unsupported_intents:
  - delete media files
  - repair missing downloads
  - rescan remote providers
  - rebuild the library
---

## When To Use

Use this skill when the user asks to check, verify, audit, or summarize local media file health.

Do not use this skill to delete files, repair files, rescan remote providers, or download missing media.

## Inputs The User May Provide

The user may provide a platform, remote ID, status filter, limit, database path, or library root.

## Tool Calling Strategy

Use `library.file.verify`.

If the user asks for a broad check and gives no filters, use a conservative limit when appropriate.

## Example Dry-Run Action

```json
{"action":"call_tool","tool":"library.file.verify","input":{"limit":100},"dry_run":true,"reason":"Verify recorded local file health without deleting or downloading anything."}
```

## Common Errors

- `missing_db_path`: ask the user to configure the Mediagent database path.
- `unsafe_path`: explain that a path is outside allowed roots.

## Final Summary

Report valid, missing, corrupt, unknown, and checked file counts. Do not recommend deleting files unless the user explicitly asks for cleanup planning.
