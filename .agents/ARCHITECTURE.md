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

## Current Link-First Flow

```text
explicit URL source
-> link.queue.upsert
-> URL safety and canonicalization
-> resolver chain
-> media candidates
-> deterministic candidate selection
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

This is now the primary product direction. URL sources may be CLI JSON, queued DB rows, Telegram inbox links, future workflow steps, or future Agent/SKILL calls.

`link_queue.normalized_url` is only the first intake dedupe layer. Resolvers must also emit canonical aliases and source/media identity where possible, so short links, canonical post links, old site links, provider watch URLs, and direct media URLs do not create duplicate downloads for the same content.

`link_queue` has a schema-v7 lifecycle foundation for cron or daemon usage. It is the URL resolution queue, not the file-download lifecycle:

```text
queued
-> resolving
-> resolved
```

Permanent skips and retryable failures stay distinct:

```text
skipped
failed
deferred
```

The schema now carries retry counts, last error, retryable flag, next attempt time, source provenance merge fields, batch limit support at the tool layer, and lease columns. `link.media.sync` uses active claim/lease behavior for queued runs and schedules retryable failures with bounded `next_attempt_at` backoff. Explicit URL and explicit `link_id` runs bypass queue claiming by design.

A successful `link.media.sync` run may resolve and download in one tool call, but the link row remains `resolved` once URL resolution is complete. Download progress and final file state live in `media_items` and `media_files`, including `downloaded`, `partial`, and `failed` states.

`MediaCandidate` must not persist credential-bearing request headers. Persistable download hints should be allowlisted and non-secret, such as a public `Referer` when required. Runtime-only headers such as `Authorization`, `Cookie`, signed URL tokens, session headers, and CSRF headers must stay in memory through a download context reference and must not be written to SQLite, sidecar metadata, logs, or snapshots.

Multi-candidate resolution is now supported for simple static file groups such as Reddit galleries. The current contract records group id, required files, optional files, partial-success status, and `metadata.files` mapping for those static groups. Muxed video/audio tracks and more complex multi-file posts remain deferred.

## Existing Collector Flow

```text
platform collector output
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http or platform-specific downloader
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

`pixiv.bookmarks.collect`, `pixiv.bookmarks.sync`, `telegram.messages.collect`, and `telegram.messages.sync` remain useful implemented flows. X bookmark collection and Reddit saved collection exist with fixture/fake-client coverage, but they are not the current expansion path unless the user explicitly resumes auth-assisted account collection.

## Future Policy Layer

RuleSpec is a planned policy layer, not an implemented runtime feature.

The intended future shape is:

```text
explicit URL source or collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM or Agent Core integrations may help users turn natural-language intent into RuleSpec files, but daemon and cron execution should run stored deterministic rules. Platform adapters should not contain quality scoring or content-preference logic.

## Deferred Workflow Layer

`src/mediagent/workflows/` exists as a placeholder. Do not implement Workflow V1 unless the user explicitly chooses it or the next platform foundations prove that the shared sync contract is stable enough.
