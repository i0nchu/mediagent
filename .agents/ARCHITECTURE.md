# Mediagent Architecture

## Product Boundary

Mediagent collects and downloads media. It does not manage a media library, browse media, repost content, or provide a gallery UI.

## Package Layout

```text
src/mediagent/
  cli.py
  core/
  tools/
  platforms/
  workflows/
```

## Core Layer

`src/mediagent/core/` contains shared primitives:

- `tooling.py`: `ToolSpec`, `ToolContext`, `ToolResult`, permissions, registry errors, and `ToolRegistry`
- `db.py`: SQLite schema and persistence helpers
- `filesystem.py`: path placeholder expansion, normalization, and write-boundary checks
- `http.py`: small HTTP client abstraction for testable downloads
- `auth.py`: credential references, credential JSON helpers, and redacted auth session models
- `rate_limit.py`: shared rate-limit metadata extraction
- `redaction.py`: secret redaction helpers
- `schema.py`: lightweight JSON-schema-compatible input validation

Core code must not contain platform-specific API behavior.

## Tool Layer

`src/mediagent/tools/` contains agent-callable tools.

Every tool must:

- declare a stable `ToolSpec`
- declare permissions
- declare dry-run support
- expose JSON-compatible input and output schemas
- return `ToolResult`
- avoid leaking secrets

The CLI, future workflows, and future Agent Core must call tools through the same registry.

## Platform Layer

`src/mediagent/platforms/` is reserved for platform-specific clients, auth, and parsers.

Platform directories:

- `platforms/x/`
- `platforms/pixiv/`
- `platforms/telegram/`
- `platforms/reddit/`

Platform modules should normalize their output into the shared media item shape before handing data to bottom tools.

`platforms/x/` currently contains:

- `auth.py`: OAuth 2.0 Authorization Code with PKCE helpers, token refresh, credential file support, and auth status checks
- `client.py`: X API calls for `/2/users/me` and authenticated-user bookmarks
- `parser.py`: conversion from X tweet/media expansions into normalized media items

`platforms/pixiv/` currently contains:

- `auth.py`: local OAuth/PKCE setup helpers, explicit refresh-token auth, token refresh, credential file support, and auth session modeling
- `client.py`: Pixiv App API calls for user detail, bookmarked illustrations, and ugoira metadata
- `parser.py`: conversion from Pixiv works into normalized media items, including multi-page works and ugoira metadata

`platforms/telegram/` currently contains:

- `auth.py`: Telethon-compatible user-session configuration, session-path safety, and safe auth-session modeling
- `client.py`: Telegram client boundary with fake-client hooks and lazy Telethon usage
- `parser.py`: conversion from Telegram message/media shapes into normalized media items, including grouped media/albums

`platforms/reddit/` currently contains:

- `auth.py`: Reddit OAuth config, token exchange/refresh/status, credential file helpers, and Reddit rate-limit metadata parsing
- `client.py`: Reddit OAuth API calls for `/api/v1/me` and authenticated-user saved listings
- `parser.py`: conversion from saved listing entries into normalized media items for first-version image/gallery/video/direct-media shapes

## CLI Flow

```text
CLI args
-> read JSON input
-> create ToolContext
-> find tool in ToolRegistry
-> validate input
-> run tool
-> print JSON or human-readable output
-> return stable exit code
```

Exit codes:

- `0`: success
- `1`: runtime failure
- `2`: validation or user input error

## Current Tool Execution Flow

```text
platform collector output
-> media.item.upsert
-> status filtering
-> download.http
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

`x.bookmarks.collect`, `pixiv.bookmarks.collect`, `telegram.messages.collect`, and `reddit.saved.collect` handle collection and normalization. Pixiv and Telegram also have deterministic sync wrappers for the full collect-to-download path: `pixiv.bookmarks.sync` and `telegram.messages.sync`. X and Reddit still use manual CLI/tool composition until they receive sync helpers or Workflow V1 exists.

## Future Policy Layer

RuleSpec is a planned policy layer, not an implemented runtime feature.

The intended future shape is:

```text
platform collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM or Agent Core integrations may help users turn natural-language intent into RuleSpec files, but daemon and cron execution should run stored deterministic rules. Platform adapters should not contain quality scoring or content-preference logic.

## Deferred Workflow Layer

`src/mediagent/workflows/` exists as a placeholder. Do not implement Workflow V1 unless the user explicitly chooses it or the next platform foundations prove that the shared sync contract is stable enough.
