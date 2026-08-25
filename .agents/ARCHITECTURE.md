# Mediagent Architecture

## Product Boundary

Mediagent collects and downloads media. It does not manage a media library, browse media, repost content, or provide a gallery UI.

## Package Layout

```text
src/mediagent/
  cli.py
  agent/
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

## Agent Core V1 Layer

`src/mediagent/agent/` contains the current local Agent Core V1 preview:

- `skills/`: Markdown SKILL loading and built-in English SKILL files
- `llm/`: the Ollama client boundary
- `prompts.py`: strict JSON skill/action prompt builders
- `actions.py` and `schema.py`: action parsing and structured agent run contracts
- `core.py`: the SKILL-scoped run loop

Agent Core V1 is not a scheduler and not a broad autonomous planner. It must choose a SKILL, call only tools allowed by that SKILL through `ToolRegistry`, reject unsupported tasks before tool calls when no SKILL clearly matches, normalize model dry-run choices to the global runtime mode, and strip destination paths that were not explicitly provided by the user.

## Platform Layer

`src/mediagent/platforms/` is reserved for platform-specific clients, auth, and parsers.

Platform directories:

- `platforms/x/`
- `platforms/pixiv/`
- `platforms/telegram/`
- `platforms/reddit/`
- `platforms/instagram/`

Platform modules should normalize their output into the shared media item shape before handing data to bottom tools.

`platforms/x/` currently contains:

- `auth.py`: OAuth 2.0 Authorization Code with PKCE helpers, token refresh, credential file support, and auth status checks
- `client.py`: X API calls for `/2/users/me` and authenticated-user bookmarks
- `parser.py`: conversion from X tweet/media expansions into normalized media items

`platforms/pixiv/` currently contains:

- `auth.py`: local OAuth/PKCE setup helpers, explicit refresh-token auth, token refresh, credential file support, and auth session modeling
- `client.py`: Pixiv App API calls for user detail, bookmarked illustrations, artwork detail, and ugoira metadata
- `links.py`: explicit artwork URL/id normalization and Pixiv artwork-detail resolution for the shared link-first resolver pipeline
- `parser.py`: conversion from Pixiv works into normalized media items, including `illustration` / `comic` / `animation` work types, multi-page works, unavailable-placeholder rejection, and ugoira metadata

`platforms/telegram/` currently contains:

- `auth.py`: Telethon-compatible user-session configuration, session-path safety, and safe auth-session modeling
- `client.py`: Telegram client boundary with fake-client hooks and lazy Telethon usage
- `parser.py`: conversion from Telegram message/media shapes into normalized media items, including grouped media/albums

`platforms/reddit/` currently contains:

- `auth.py`: Reddit OAuth config, token exchange/refresh/status, credential file helpers, and Reddit rate-limit metadata parsing
- `client.py`: Reddit OAuth API calls for `/api/v1/me` and authenticated-user saved listings
- `parser.py`: conversion from saved listing entries into normalized media items for first-version image/gallery/video/direct-media shapes

`platforms/instagram/` currently contains:

- `auth.py`: saved-session status, explicit local username/password login, bounded ensure-session behavior, credential path safety, and agent-decidable auth/session error mapping
- `links.py`: Instagram post/Reel/tv URL parsing, canonical identity, whole-post resource normalization, and runtime-only signed CDN download URL handling
- `client.py` and `parser.py`: one-page saved-feed access and whole-post saved-media normalization with opaque cursors

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

The shared link intake performs dedicated comic dispatch before the generic resolver chain. Supported nhentai gallery and JMComic album/photo/cover links received through `link.media.sync`, queued `link_queue` rows, or `telegram.inbox.sync_links` all enter `comic.link.sync` with exact scope. Telegram remains ingest provenance; it does not change the source platform or create favorite/follow state. Future inbox implementations should enqueue links and call `link.media.sync` instead of implementing provider routing themselves.

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

Instagram uses the same link-first contract with a platform session boundary. One Instagram `/p/<shortcode>/`, `/reel/<shortcode>/`, or `/tv/<shortcode>/` URL represents the whole post. Carousel resources are normalized as multiple files under one media item, while signed Instagram CDN URLs stay runtime-only and are not persisted as canonical media identity.

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

Pixiv keeps physical media type separate from work type. Manga source pages remain `media_type: photo`, while `metadata.work_type: comic` selects `comic-pages`; the Kavita-oriented CBZ packaging layer writes archives under `comic/<series-directory>/`. One-shots receive a unique series identity plus `Number=1`, `Count=1`, and `Format=One-Shot`; real series share one directory and use normalized `Series`, `Number`, optional `Volume`, and optional `Count`. Normal illustrations use `illustration` / `photo`, and ugoira uses `animation` / `video`.

The descriptor and CBZ writer in `core/comics.py` are platform-neutral. Pixiv, nhentai, and JMComic now share normalized `metadata.comic` and the `comic-pages` -> `comic` packaging contract. Future authorized-source adapters may join the same flow when they provide reliable `work_type:comic`, ordered pages, series/chapter/volume identity, and work metadata; multi-image count alone must not imply a comic.

## Future Policy Layer

## Comic Target And Collection Flow

Comic source pages remain `media_type: photo`; `metadata.work_type: comic` and `storage_category: comic-pages` select the comic pipeline. `metadata.comic` is provider-neutral and carries stable work/series/chapter identity. A complete healthy page set is atomically packaged under `comic/` as a CBZ with `ComicInfo.xml`.

Direct URL scope is deterministic and never creates follow state. Account favorites commit a complete collection snapshot in one SQLite transaction. An incomplete or failed pagination must not deactivate old memberships. JMComic first completes every selected folder, unions memberships by album ID, and commits that effective inbox; changing the folder set deactivates only albums absent from the new union and never deletes downloaded pages or CBZ files. Active JM album memberships are re-resolved on later favorite syncs to discover new chapters; nhentai favorite galleries are exact.

For album-scoped JMComic resolution, the album episode list owns chapter numbering; the per-photo payload supplies pages and title but cannot downgrade the chapter number when its series list lags. Duplicate raw numbers are disambiguated before normalized items reach DB/package layers. Historical repair resolves every represented album, compares current manifest identity with DB and CBZ `ComicInfo.xml`, produces a read-only manifest, then on confirmed apply rebuilds only affected archives from healthy tracked source pages and quarantines replaced CBZ files. Provider/network or source-health gaps block apply before mutation.

Schema v8 adds `source_collections` and `source_collection_memberships`; schema v9 adds `source_collection_scope_aliases` for account-scoped human names mapped to remote collection scopes. Stable per-page file keys prevent rotating CDN URLs from creating duplicate media-file rows. Credential-bearing headers, cookies, API tokens, and JM decode runtime metadata remain outside persisted media metadata.

RuleSpec is a planned policy layer, not an implemented runtime feature.

The intended future shape is:

```text
explicit URL source or collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM or Agent Core integrations may help users turn natural-language intent into explicit tool calls or future RuleSpec files, but daemon and cron execution should run stored deterministic rules. Platform adapters should not contain quality scoring or content-preference logic.

## Deferred Workflow Layer

`src/mediagent/workflows/` exists as a placeholder. Do not implement Workflow V1 unless the user explicitly chooses it or the next platform foundations prove that the shared sync contract is stable enough.
