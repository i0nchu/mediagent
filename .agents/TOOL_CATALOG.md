# Mediagent Tool Catalog

Use this catalog to understand the currently registered tools. Inspect exact schemas with:

```bash
uv run --locked mediagent tools inspect <tool-name> --json
```

Run a tool with:

```bash
uv run --locked mediagent tools run <tool-name> --input examples/tools/<tool-name>.json --json
```

Add `--dry-run` when the tool supports safe preview.

## Agent Core V1 Commands

Agent Core V1 is a local preview that calls the same `ToolRegistry` through SKILL allowlists. It is not a separate platform layer and not a scheduler.

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect <skill-name> --json
uv run --locked mediagent agent run "<natural language task>" --dry-run --json
uv run --locked mediagent agent run "<natural language task>" --json
```

Built-in SKILLs:

- `explicit_link_download`
- `instagram_link_download`
- `library_health_check`
- `pixiv_bookmark_sync`
- `telegram_inbox_download`

Agent runs reject unsupported tasks before tool calls when no SKILL clearly matches. They also return structured `llm_request_failed` errors for Ollama transport failures and strip hallucinated destination path fields unless the user explicitly provided those paths.

## Auth Tools

### `auth.session.status`

Reports whether a configured provider session is usable without exposing secrets. For `provider: "x"`, this delegates to X auth status.

Permissions:

- `network`
- `read_credentials`

### `auth.session.refresh`

Refreshes a provider auth session through the platform adapter. For X, refreshed tokens can be written to `credential_output_path` or `X_CREDENTIALS_FILE`.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `auth.session.revoke`

Returns explicit local credential revocation guidance. It does not revoke remote sessions automatically and requires confirmation.

Permissions:

- `read_credentials`
- `write_credentials`

## Core Tools

### `core.env.check`

Validates required environment variables and configured paths.

Permissions:

- `read_env`

### `core.db.init`

Initializes the local SQLite database and creates tables for runs, media items, media files, sync cursors, auth sessions, and future workflows.

Permissions:

- `read_files`
- `write_db`

### `core.cleanup.media_state`

Plans or applies conservative live-test media-state cleanup. Planning mode does not mutate files or SQLite. Apply mode requires `confirm: true`, moves existing media files into quarantine first, then removes matching media file rows and resets matching media items to `discovered`. Credential paths are protected and are not actionable cleanup files.

Permissions:

- `read_env`
- `read_db`
- `write_db`
- `read_files`
- `write_files`

### `core.path.prepare`

Resolves and validates a filesystem target path. It rejects unsafe paths outside configured write roots.

Permissions:

- `read_env`
- `read_files`
- `write_files`

### `core.run.record`

Records a tool or workflow run summary in SQLite. Secrets are redacted before storage.

Permissions:

- `write_db`

### `core.sync_cursor.get`

Reads a persistent platform sync cursor such as an X bookmark pagination token.

Permissions:

- `read_db`

### `core.sync_cursor.set`

Writes a persistent platform sync cursor. Metadata is redacted before storage.

Permissions:

- `write_db`

## Media Tools

### `media.item.upsert`

Upserts discovered media items by `platform + remote_id`.

Permissions:

- `write_db`

Required item fields:

- `platform`
- `remote_id`
- `media_type`

Supported media types:

- `photo`
- `video`
- `audio`

### `media.item.filter_new`

Filters discovered media items before download. Downloaded, skipped, failed, known, and new items are summarized separately.

Permissions:

- `read_db`

### `media.item.set_status`

Updates a known media item status intentionally. This is the explicit parent-item status path used by deterministic sync helpers after file downloads finish.

Permissions:

- `write_db`

### `media.file.upsert`

Upserts a local media-file record for a known media item. It records remote URL, local path, library-relative path, storage layout, MIME type, byte size, checksum, health, and status.

Permissions:

- `write_db`

## Storage And Library Tools

### `storage.path.plan`

Plans a deterministic scanner-friendly library path for one normalized media file.

Default layout:

```text
<platform>/<media_type>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
```

Library root resolution uses explicit `library_root`, then `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`, then `MEDIAGENT_LIBRARY_DIR`, then `${MEDIAGENT_DATA_DIR}/library`. A platform-specific root is already scoped to one platform, so it does not add a duplicate platform directory by default.

Permissions:

- `read_env`
- `read_files`
- `write_files`

### `library.file.verify`

Verifies known local media files from SQLite by checking local existence, size, and checksum. It marks file health as `valid`, `missing`, `corrupt`, or `unknown`. It never deletes files and never contacts source platforms.

Permissions:

- `read_db`
- `write_db`
- `read_files`

## Download And Metadata Tools

### `download.http`

Downloads one remote file to a safe local path. Supports dry-run, bounded attempts, `.partial` finalization, checksum output, MIME validation, content-length validation, custom request headers, and rate-limit metadata.

Permissions:

- `network`
- `read_files`
- `write_files`

### `metadata.write`

Writes normalized JSON metadata next to downloaded files. Secrets are redacted before writing.

Permissions:

- `write_files`

## Pixiv Tools

### `pixiv.auth.login`

Starts or completes explicit local Pixiv OAuth/PKCE setup. Without `code` or `callback_url`, it returns a login URL and code verifier. With `code` or `callback_url` plus `code_verifier`, it exchanges the short-lived callback code for tokens and can write a credential JSON file under configured write roots.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `pixiv.auth.status`

Validates configured Pixiv credentials without exposing secrets. It can verify a usable access token with user ID, or check whether token refresh succeeds without writing credentials.

Permissions:

- `network`
- `read_credentials`

### `pixiv.auth.refresh`

Refreshes Pixiv App API credentials from an explicit refresh token and can write a credential JSON file under configured write roots.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `pixiv.link.resolve`

Resolves one user-provided Pixiv artwork URL or `illust_id` into normalized downloadable media candidates without downloading files. It uses the configured Pixiv session, does not start browser login by itself, and returns structured auth errors such as `pixiv_auth_missing_credentials` when the user needs to run `pixiv.auth.login` or `pixiv.auth.refresh`.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `pixiv.bookmarks.collect`

Collects bookmarked Pixiv illustrations and manga for the configured account. It normalizes single-page works, multi-page works, and ugoira metadata into shared media items and can store a per-restrict cursor in SQLite.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`
- `write_db`

Pixiv collection does not download files by itself. For a full deterministic bookmark download, use `pixiv.bookmarks.sync`. For manual one-file downloads, use the returned `metadata.files[].url` values with `download.http`; Pixiv image downloads usually need:

```json
{"Referer":"https://www.pixiv.net/"}
```

### `pixiv.bookmarks.sync`

Collects Pixiv bookmarks, upserts and filters media items, plans scanner-friendly storage paths, downloads each selected `metadata.files[]` entry with `.partial` finalization, records local media files, and updates parent item status to `downloaded`, `partial`, or `failed`. JSON sidecar metadata is opt-in through `write_sidecar_metadata`.

When `media_types` filtering is used, sync cursors are scoped by filter, such as `bookmarks:public:photo`, so filtered syncs do not mutate the unscoped bookmark cursor.

For timer-style recurring sync, use `stop_on_known:true` with a bounded `max_pages`. This scans from the newest bookmarks and stops after a page containing an already known terminal item, avoiding repeated full bookmark scans while still relying on SQLite media item state for dedupe.

For explicit full bookmark rebuilds, use `full_sync:true`, omit `limit` and `max_pages`, and set `stop_on_known:false`. Direct CLI/tool calls without `full_sync:true` keep the conservative one-page default.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`
- `read_db`
- `write_db`
- `read_files`
- `write_files`

Pixiv explicit artwork URLs can also be downloaded directly through `link.media.sync`. This path treats one artwork URL as one media item, resolves all pages by default, dedupes against `pixiv.bookmarks.sync`, applies the required Pixiv `Referer` at download time, and keeps runtime headers out of persisted metadata.

## Telegram Tools

Telegram is treated as a media source, not a notification, forwarding, or chat-management platform. The implementation uses a user MTProto session through Telethon-compatible boundaries. Telegram session files are credentials.

### `telegram.auth.login`

Starts or completes explicit local Telegram user-session login. `start` requests a Telegram login code and returns the `phone_code_hash` needed for the second step. `complete` accepts the login code plus `phone_code_hash`, supports optional 2FA through `password_ref`, and writes only the configured session file under allowed credential/data roots.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `telegram.auth.status`

Validates configured Telegram user-session credentials without exposing secrets. It checks `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_FILE` / `MEDIAGENT_DATA_DIR`, then reports safe session/account status only.

Permissions:

- `network`
- `read_credentials`

### `telegram.dialogs.list`

Lists selectable dialogs visible to the configured user session. It returns safe chat identifiers, display titles, chat type, username when available, and access hints. It does not return message text or media bytes.

Permissions:

- `network`
- `read_credentials`

### `telegram.messages.collect`

Collects media-bearing messages from explicit chats or message links and normalizes photos, videos, audio, voice/audio documents, and media documents into shared media items. It can read and store per-source cursors, but does not download media bytes.

When `extract_message_links: true` is set, this tool scans collected message text/captions for Telegram message links, resolves those linked original messages, and normalizes their media too. This supports a curated link-inbox channel where the user posts links to media worth downloading. Linked source cursors are not advanced.

Permissions:

- `network`
- `read_credentials`
- `read_db`
- `write_db`

### `telegram.media.download`

Downloads one Telegram media object through the Telegram client boundary to a safe local path. It supports dry-run, direct streaming to `.partial`, checksum output, MIME validation, finalization, timeout enforcement, and path safety.

Permissions:

- `network`
- `read_credentials`
- `read_files`
- `write_files`

### `telegram.messages.sync`

Collects selected Telegram media messages, upserts and filters media items, plans scanner-friendly storage paths, downloads media through `telegram.media.download`, records local media files, updates parent item status, and advances per-source scoped cursors only after successful durable processing.

Default source selection is explicit. Use a trusted chat selector such as Saved Messages, a private collection channel, an allowlisted group/channel, or explicit message links. Do not scan all dialogs by default.

For curated Telegram usage, point `chat` at the user's private collection channel and set `extract_message_links: true`. The configured user session must be able to access each linked original message; Mediagent does not bypass protected or inaccessible chats.

Small curated media download, one-hour linked video download, scanner-friendly layout placement, `library.file.verify`, and rerun dedupe were live-verified on 2026-07-24 UTC.

Permissions:

- `network`
- `read_credentials`
- `read_db`
- `write_db`
- `read_files`
- `write_files`

## Core Link Tools

These tools are the stable entry points for the Phase 19 link-first product path.

- `link.queue.upsert`: queues one or more explicit URLs with normalized URL dedupe and source provenance merge. It does not resolve or download media.
- `link.media.sync`: resolves explicit URLs or queued link records, claims ready queued records for cron/daemon runs, schedules retryable deferred records, converts clear media candidates into normalized media items, dedupes known items, plans storage paths, downloads files, writes optional sidecar metadata, records media-file state, and updates parent item status. `retry_auth_skipped:true` explicitly reclaims old `requires_auth` / `login_wall` rows after a platform session becomes usable.

Public CLI shortcut:

```bash
mediagent link sync <url> --json
```

This shortcut delegates to `link.media.sync`; it is the stable non-Telegram entry point for user-provided links.

`link.media.sync` is deterministic and callable from Python, CLI, cron, workflows, and future Agent/SKILL integrations. It must keep writes under configured project-local roots and must not persist credential-bearing headers from resolver candidates.

Known platform page domains with dedicated resolvers are reserved from generic fallback. Unsupported Instagram pages, Pixiv non-artwork pages, and Imgur gallery/album-style pages return structured platform skips instead of being parsed by `generic_html_media`.

## Instagram Tools

Instagram support is explicit-link first. It uses a saved local session only for resolving user-provided public post/Reel URLs. It does not scan feeds, saved posts, stories, profiles, messages, comments, likes, follows, or account activity.

One Instagram post URL represents the whole post. Carousel posts download every media resource by default; `img_index` is retained only as source metadata unless a future explicit option changes that behavior. Signed Instagram CDN URLs are runtime-only download data and must not be persisted to SQLite, sidecar metadata, logs, snapshots, or tool output.

### `instagram.auth.status`

Validates the configured saved Instagram session without exposing cookies, session IDs, username, password, or raw private API payloads. Session paths are checked against configured project-local roots before fake-client callbacks, real-client loads, or network work.

Permissions:

- `read_env`
- `read_credentials`
- `network`

### `instagram.auth.login`

Creates or replaces a saved local Instagram session from explicit local username/password credentials. The session file must live under allowed credential/data roots and is written with restrictive permissions.

Permissions:

- `read_env`
- `read_credentials`
- `write_credentials`
- `network`

### `instagram.auth.ensure_session`

Checks the saved session and attempts bounded low-frequency relogin only when credentials exist and cooldown allows it. User-action states such as checkpoint and 2FA stop automation; rate-limit and temporary-block states should defer work.

Permissions:

- `read_env`
- `read_credentials`
- `write_credentials`
- `network`

### `instagram.link.resolve`

Resolves one public Instagram `/p/<shortcode>/`, `/reel/<shortcode>/`, or `/tv/<shortcode>/` URL into normalized media candidates. It never performs password login by itself. Non-Instagram hosts or missing shortcodes return `instagram_media_unsupported`; missing/invalid sessions return agent-decidable auth errors such as `instagram_session_missing`, `instagram_session_invalid`, or `instagram_login_required`.

Permissions:

- `read_env`
- `read_credentials`
- `network`

## Experimental Link Tools

These tools remain experimental helper surfaces while the public preview/compatibility story is settled. Use `--include-experimental` for listing and `--allow-experimental` for inspect/run.

- `link.resolve.preview`: safely previews one explicit URL without downloading. It supports direct media, bounded single-media HTML, and small provider-specific resolver behavior where implemented.
- `link.resolve.to_media_item`: converts a resolved link candidate into a normalized media item for the existing storage/download pipeline.

Do not treat these experimental names as stable public API yet. Promotion must preserve aliases for existing live-test commands and update examples, this catalog, `RUNBOOK.md`, and localized handoff files together.

## Agent-Only Low-Profile Skills

- `telegram_inbox_download`: lets Agent Core process the configured Telegram inbox without documenting the direct tool entry points as public workflow commands. The underlying tools are hidden stable surfaces: not listed by default, but callable by name for users or agents that already know them.
- `telegram.inbox.sync_links`: accepts `full_sync:true` for selected-inbox full-source scans. In that mode the tool does not apply the default 100-message scan limit; URL/media/file dedupe still happens in the tool layer. Inbox `t.me` / `telegram.me` message links bridge to Telegram-native message sync with inbox provenance and structured protected/inaccessible skips, while external URLs keep using the link resolver pipeline. `retry_auth_skipped:true` retries old auth-dependent rows ingested from Telegram.

## Reddit Tools

Reddit auth/saved tools exist, but they are currently deferred legacy/advanced capability. The active product direction is explicit-link resolution with anonymous/bounded behavior first, plus Redgifs as the next no-auth provider foundation. Do not build on saved collection unless the user explicitly resumes auth-assisted account collection.

The saved-collection slice only reads OAuth identity/history data and collects direct media candidates. It does not post, comment, vote, save/unsave, moderate, chat, scan subreddits, scrape HTML pages, or run third-party extractors.

### `reddit.auth.start`

Generates a Reddit OAuth authorization URL for `identity` + `history` saved-media access.

Permissions:

- `read_env`

### `reddit.auth.exchange`

Exchanges a Reddit OAuth callback code for tokens and can write the credential JSON file under configured write roots. Raw tokens, client secrets, and authorization codes are not returned.

Permissions:

- `read_env`
- `network`
- `write_credentials`

### `reddit.auth.refresh`

Refreshes Reddit OAuth access credentials from `REDDIT_CREDENTIALS_FILE` or `refresh_token_ref`, preserving the refresh token when Reddit does not return a replacement.

Permissions:

- `read_env`
- `network`
- `read_credentials`
- `write_credentials`

### `reddit.auth.status`

Validates the configured Reddit access token with a safe account/status response.

Permissions:

- `read_env`
- `network`
- `read_credentials`

### `reddit.saved.collect`

Collects media-bearing Reddit saved items for `username` or `me`, supports `after` pagination and optional cursor storage, and normalizes supported submissions/comments into shared media items. It does not download files or write media-file records.

First-version parser support includes Reddit-hosted single images, Reddit gallery images, Reddit-hosted video fallback URLs, and direct external image/video URLs with stable file extensions. Unsupported embeds and comments without direct media are skipped.

Permissions:

- `read_env`
- `network`
- `read_credentials`
- `write_db`

## X Tools

### `x.auth.start`

Generates an X OAuth 2.0 Authorization Code with PKCE URL, state, code verifier, and code challenge.

Permissions:

- none

### `x.auth.exchange`

Exchanges an X OAuth authorization code for tokens. It returns only redacted session metadata. Raw tokens can be written to `credential_output_path` or `X_CREDENTIALS_FILE`.

Permissions:

- `network`
- `write_credentials`

### `x.auth.refresh`

Refreshes X OAuth tokens using an explicit refresh token, `refresh_token_ref`, or configured credential file. It preserves an existing refresh token when X does not return a replacement.

Permissions:

- `network`
- `read_credentials`
- `write_credentials`

### `x.auth.status`

Validates X token presence, expiration, required scopes, and authenticated user ID through `/2/users/me`.

Permissions:

- `network`
- `read_credentials`

### `x.bookmarks.collect`

Fetches media-bearing X bookmarks for the authenticated user, normalizes them into media items, returns rate-limit metadata, and can store the bookmark pagination cursor in SQLite.

Permissions:

- `network`
- `read_credentials`
- `write_db`

## Credential Notes

- X credentials may come from `X_ACCESS_TOKEN` / `X_REFRESH_TOKEN` or from `X_CREDENTIALS_FILE`.
- Pixiv credentials may come from `PIXIV_CREDENTIALS_FILE`, `PIXIV_REFRESH_TOKEN`, or `PIXIV_ACCESS_TOKEN`. Prefer `pixiv.auth.login` for first-time local setup.
- Telegram credentials come from `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_FILE`; the session file is a credential and should live under `${MEDIAGENT_DATA_DIR}/credentials/`. Prefer `telegram.auth.login` for first-time local setup.
- Reddit credentials may come from `REDDIT_CREDENTIALS_FILE` or token environment variables. Use `reddit.auth.start` + `reddit.auth.exchange` only when explicitly validating the deferred auth-assisted path, and always use a unique descriptive `REDDIT_USER_AGENT`.
- Instagram credentials come from `INSTAGRAM_ACCOUNT`, `INSTAGRAM_SECRET`, and `INSTAGRAM_SESSION_FILE`; the session file is a credential and should live under `${MEDIAGENT_DATA_DIR}/credentials/`. Prefer `instagram.auth.ensure_session` before link sync and use `instagram.auth.login` only for explicit local session creation.
- `X_CREDENTIALS_FILE`, `PIXIV_CREDENTIALS_FILE`, `TELEGRAM_SESSION_FILE`, `REDDIT_CREDENTIALS_FILE`, and `INSTAGRAM_SESSION_FILE` should point to explicit files controlled by the user.
- Token exchange and refresh outputs do not include raw tokens.
- SQLite run records must never store raw access tokens, refresh tokens, cookies, sessions, or bot tokens.
