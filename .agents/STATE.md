# Mediagent Current State

## 2026-08-14 Comic Source Update

- SQLite schema is version 8 with atomic source collection snapshots and active/inactive memberships.
- `platforms/nhentai/` supports exact gallery resolution, ordered image manifests, complete favorite pagination, reusable browser-cookie sessions, and session refresh/persistence with mode `0600`.
- `platforms/jmcomic/` supports strict album/photo/trusted-cover links, encrypted mobile API envelopes, reusable login sessions, complete album/favorite manifests, and deterministic vertical-slice image restoration.
- JMComic sessions can be created from configured username/password or loaded from an optional Netscape `cookies.txt`. Cookie-file paths preserve their format and mode `0600`; explicit `jmcomic.auth.login` ignores an invalid old session and replaces it after successful credential login.
- JMComic favorite collect/sync now recovers a remotely expired session with at most one credential login per run. A recovered or rotated session is atomically checkpointed after collection and each album resolution instead of waiting for the whole long run to finish; safe summary fields report recovery/checkpoint outcomes without session contents.
- JMComic transport decodes bounded gzip/deflate API responses before JSON/AES envelope parsing. A sanitized live public-album probe verified the current endpoint returns gzip and now decodes album `349717` successfully.
- JMComic segment-count hashing now uses the filename stem, matching the maintained upstream decoder. Album `349717` page `00001.webp` previously produced 18 segments but correctly produces 10. Explicit comic `overwrite` now requeues terminal downloaded items so affected pages and CBZ packages can be rebuilt atomically.
- `comic.link.sync` always applies exact direct-link scope. `nhentai.favorites.sync` uses exact gallery targets; `jmcomic.favorites.sync` follows only active favorite albums.
- `nhentai.favorites.collect` and `jmcomic.favorites.collect` provide summary-only complete-snapshot diagnostics without downloads or membership changes. JM credential login/session reuse and a live three-page collection of 42 favorite albums are verified. The current nhentai browser cookie returns HTTP 401 and must be re-exported before its live collection can be repeated.
- A full JM favorites dry-run expanded those 42 albums into 1,081 chapters and 49,137 planned page downloads. A bounded real favorites sync committed all 42 active memberships, downloaded and verified 108/108 pages for one selected album, packaged one valid CBZ with `ComicInfo.xml`, and reran with zero downloads plus one existing CBZ. Large identity lookups are chunked below SQLite limits, and favorites sync processes one target at a time so earlier albums remain durable if a later target fails.
- The first production JM bootstrap attempted all 49,137 pages: 49,080 became valid, 57 remained failed across 20 partial chapters, and 1,061 complete chapters received CBZ archives. A forced credential login confirmed the next run could commit the 42-item snapshot and resume. This exposed the expired-session persistence gap fixed above.
- SQLite connections use a 30-second busy timeout. System-level comic favorite timer examples use a shared non-blocking run lock and compact `--summary-json` output; JMComic has an 18-hour initial-full-sync timeout. Follow is periodic rerunning of `jmcomic.favorites.sync`, not a resident daemon.
- Shared link intake now dispatches recognized nhentai/JMComic links to the exact comic adapter before generic HTML resolution. This covers direct `link.media.sync`, queued links, Telegram inbox input, and future inboxes built on the same queue/tool boundary; Telegram provenance is retained without creating follow state.
- Comic pages use stable identities and `comic-pages`; complete chapters are atomically packaged as Kavita-oriented CBZ files with `ComicInfo.xml`. One-chapter JM albums retain a series layout so a later new chapter does not move the original archive.
- Favorite removal stops follow state but does not delete media. Incomplete collection snapshots are never committed.
- The locked offline suite passes 346 tests after this update.

## Implemented

- Package layout exists under `src/mediagent/`.
- `main.py` is a thin startup entry.
- Console script `mediagent = mediagent.cli:main` is configured in `pyproject.toml`.
- Tool contract exists in `src/mediagent/core/tooling.py`.
- Tool registry exists through `src/mediagent/tools/defaults.py`.
- CLI bridge exists in `src/mediagent/cli.py`.
- Agent Core V1 exists under `src/mediagent/agent/` with SKILL loading, strict JSON action parsing, Ollama integration, tool allowlist enforcement, dry-run/execute boundaries, and compact redacted tool-result feedback.
- Built-in English agent SKILL files exist under `src/mediagent/agent/skills/builtin/`.
- Agent CLI commands exist: `mediagent agent run`, `mediagent agent skills list`, and `mediagent agent skills inspect`.
- SQLite schema initialization exists in `src/mediagent/core/db.py`.
- Current SQLite schema version is `8`, with idempotent migration support for old media item/file tables, stable `link_queue` lifecycle/retry/provenance fields, and comic source collection memberships.
- Filesystem safety helpers exist in `src/mediagent/core/filesystem.py`.
- Secret redaction helpers exist in `src/mediagent/core/redaction.py`.
- HTTP abstraction exists in `src/mediagent/core/http.py`.
- Core URL intake and resolver helpers exist in `src/mediagent/core/links.py`.
- Reddit public-link parsing helpers exist in `src/mediagent/platforms/reddit/links.py`.
- Credential and auth-session primitives exist in `src/mediagent/core/auth.py`.
- Rate-limit metadata parsing exists in `src/mediagent/core/rate_limit.py`.
- X platform support exists under `src/mediagent/platforms/x/` for OAuth PKCE, token refresh, user status, bookmark API calls, and media parsing.
- Pixiv platform support exists under `src/mediagent/platforms/pixiv/` for local OAuth/PKCE setup, explicit refresh-token auth, token refresh, bookmark API calls, multi-page media parsing, and ugoira metadata preservation.
- Telegram platform support exists under `src/mediagent/platforms/telegram/` for Telethon-backed user-session configuration, explicit login boundaries, session status boundaries, dialog listing, message collection/link-inbox boundaries, media normalization, and Telegram-specific media download.
- Telegram numeric dialog selectors returned by `telegram.dialogs.list` can be passed back to collect/sync tools as strings or explicit object IDs.
- Reddit platform support exists under `src/mediagent/platforms/reddit/` for OAuth config/auth helpers, saved-listing API calls, and media parsing for first-version image/gallery/video/direct-media shapes.
- Reddit explicit-link support exists through the `reddit_media_link` resolver for direct `i.redd.it` image URLs, direct `v.redd.it` MP4 video-only URLs, Reddit post/share links, bounded anonymous HTML, `old.reddit.com` fallback with static non-secret `over18=1`, static galleries, preview-fallback galleries, and structured skips for manifest/login-wall cases.
- Instagram platform support exists under `src/mediagent/platforms/instagram/` for saved-session auth boundaries, explicit local login, bounded session repair, post/Reel URL parsing, and post-level resource normalization.
- Instagram explicit-link support exists through the `instagram_media_link` resolver for public `/p/<shortcode>/`, `/reel/<shortcode>/`, and `/tv/<shortcode>/` URLs using a configured saved local session.
- Known platform page domains with dedicated resolvers are guarded by `reserved_platform_page`, so unsupported Instagram pages, Pixiv non-artwork pages, and Imgur gallery/album-style pages return structured skips instead of falling through to generic HTML/media resolution. Existing live DB/library rows with `instagram_com` are historical residue from before this guard.
- Deterministic sync helpers exist in `src/mediagent/core/sync.py`.
- Universal storage planning exists in `src/mediagent/core/storage.py`.
- The default shared-root storage layout is `scanner-friendly-v2`: `<platform>/<storage_category>/<yyyy>/<mm>/<filename>`. Storage category normally equals media type; Pixiv manga source pages remain photo files but use `comic-pages`, while packaged CBZ files use `comic`.
- Platform-specific library roots are supported through `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`, for example `MEDIAGENT_PIXIV_LIBRARY_DIR`.
- Platform-specific roots are treated as already scoped to that platform, so they omit the extra platform directory by default.
- Pixiv bookmark sync now performs collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update.
- Pixiv artwork normalization preserves `work_type: illustration|comic|animation`; official `type:manga` source pages store under `pixiv/comic-pages/...`, deterministic CBZ packages under `pixiv/comic/...`, and `illust` remains under `pixiv/photo/...` even when multi-page.
- `pixiv.comics.package` packages complete downloaded manga pages into atomic, deterministic Kavita-oriented CBZ archives with `ComicInfo.xml`; one-shots have unique series identities, real series share a directory, and `migrate_legacy:true` rebuilds V1 archives then moves old copies to `.trash/mediagent-comic-v1`. `pixiv.bookmarks.sync` can opt in through `package_comics:true`.
- Pixiv invisible stubs and `s.pximg.net/.../limit_*.png` placeholder-only responses are marked unavailable and are not downloaded.
- `pixiv.bookmarks.sync` supports explicit `repair_missing_files:true`; default reruns still skip downloaded DB items even if external cleanup moved their files to `.trash`.
- Pixiv bookmark sync stores scoped cursors when `media_types` filtering is used, such as `bookmarks:public:photo`.
- Telegram message sync stores per-source scoped cursors when durable processing succeeds, such as `messages:saved_messages:photo-video`.
- Low-profile Telegram inbox link resolver support exists as hidden stable tools for Agent SKILL usage. It treats Telegram as ingest provenance and uses the resolved `origin_source` for media items and storage layout.
- Conservative cleanup/recovery support exists through `core.cleanup.media_state` for planning media-state cleanup and quarantining files before DB reset.
- Media file records use a stable non-null `file_key` so upserts remain idempotent even when `remote_url` or `local_path` is missing.
- Media file records can store library-relative paths, storage layout version, file health, source timestamp, and verification timestamp.
- Stable JSON examples exist under `examples/tools/`.
- Fake HTTP and recorded-response fixtures exist under `tests/fixtures/`.
- Tests exist under `tests/`.

## Implemented Tools

- `auth.session.status`
- `auth.session.refresh`
- `auth.session.revoke`
- `core.env.check`
- `core.db.init`
- `core.cleanup.media_state`
- `core.path.prepare`
- `core.run.record`
- `core.sync_cursor.get`
- `core.sync_cursor.set`
- `download.http`
- `library.file.verify`
- `link.queue.upsert`
- `link.media.sync`
- `link.resolve.preview` (experimental)
- `link.resolve.to_media_item` (experimental)
- `media.file.upsert`
- `media.item.upsert`
- `media.item.filter_new`
- `media.item.set_status`
- `metadata.write`
- `storage.path.plan`
- `pixiv.auth.login`
- `pixiv.auth.status`
- `pixiv.auth.refresh`
- `pixiv.link.resolve`
- `pixiv.bookmarks.collect`
- `pixiv.bookmarks.sync`
- `pixiv.library.reconcile`
- `pixiv.comics.package`
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `instagram.saved.collect`
- `instagram.saved.sync`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links` (hidden stable)
- `telegram.inbox.sync_links` (hidden stable)
- `telegram.dialogs.list`
- `telegram.messages.collect`
- `telegram.media.download`
- `telegram.messages.sync`
- `reddit.auth.start`
- `reddit.auth.exchange`
- `reddit.auth.refresh`
- `reddit.auth.status`
- `reddit.saved.collect`
- `x.auth.start`
- `x.auth.exchange`
- `x.auth.refresh`
- `x.auth.status`
- `x.bookmarks.collect`

## Latest Agent Core V1 State

- Agent Core V1 is LLM-driven, not a deterministic intent planner. The selected model chooses SKILL actions through a strict JSON action protocol.
- Supported actions are `call_tool`, `final`, and `ask_user`.
- The first LLM backend is Ollama. Default local settings are `MEDIAGENT_LLM_PROVIDER=ollama`, `MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434`, and `MEDIAGENT_OLLAMA_MODEL=qwen3:8b`.
- Built-in SKILL files are intentionally written in English and do not assume the user's language. The LLM is expected to understand and respond to the user's natural language.
- Built-in skills are `explicit_link_download`, `instagram_link_download`, `library_health_check`, `pixiv_bookmark_sync`, and `telegram_inbox_download`.
- SKILL frontmatter now exposes explicit intent boundaries through `supported_intents`, `unsupported_intents`, `requires_initial_tool_call`, and `supports_unbounded`.
- Agent Core supports full-source tasks only when the selected SKILL documents a full-sync mode. Telegram inbox and Pixiv bookmark SKILLs now support "all/complete/until-exhausted" requests through explicit `full_sync:true` tool inputs, while the prompt tells the model not to invent count/page limits for those tasks.
- Pixiv bookmark sync SKILL text now states that `limit` means bookmark item count, not downloaded file count; multi-page artworks may produce more files than the item limit.
- Telegram inbox SKILL text now describes the selected inbox workflow boundary, lets the tool use `MEDIAGENT_TELEGRAM_INBOX_*` when no explicit selector is named, and explicitly does not inspect inbox existence/configuration in V1.
- `telegram.inbox.collect_links` and `telegram.inbox.sync_links` can now use `MEDIAGENT_TELEGRAM_INBOX_KEY` plus `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`, `MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`, or `MEDIAGENT_TELEGRAM_INBOX_CHAT` as the default inbox selector for Agent Core, cron, and systemd timer runs.
- `mediagent agent run "<task>"` defaults to execute mode. `--dry-run` is an explicit preview/development mode, and the runner normalizes tool actions to the global runtime mode so the model cannot silently downgrade execute runs into dry-run previews.
- LLM transport failures are returned as structured `llm_request_failed` agent errors instead of Python tracebacks.
- Skill selection supports `unsupported_task` / tool-gap outcomes before any tool call when no SKILL clearly matches.
- Agent Core strips `library_root`, `target_dir`, and `target_path` values that were not explicitly present in the user task, and rejects explicit destination paths outside configured write roots.
- Long-running progress/logging and structured streaming remain deferred to V2 or later.
- The current local `qwen3:8b` model was verified against fake tools. It correctly selected `explicit_link_download` for an English explicit-link task and `telegram_inbox_download` for a Traditional Chinese inbox task, produced valid `call_tool` actions, respected global run mode, and returned `final` after successful tool feedback.
- `telegram_inbox_download` now requires an initial tool call for action tasks. A live Ollama dry-run for `同步一次inbox的內容` selected the inbox SKILL and called hidden stable `telegram.inbox.sync_links` without `--allow-experimental`.
- A live Ollama dry-run for `我目前有存在的 telegram inbox 嗎？` returned structured `unsupported_task` with `skill: null` and no tool steps.

## Latest Clean-State Agent Full-Source Verification

- On 2026-08-05 UTC, the active `/home/ion/projects/mediagent/mediagent-data/library` and `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` were deleted and rebuilt without backup. Credentials and session files under `mediagent-data/credentials/` were preserved.
- `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"` selected `telegram_inbox_download` and called `telegram.inbox.sync_links` with `full_sync:true`, `store_cursor:false`, and no invented `limit` / `max_messages`.
- First Telegram run: 31 links collected/considered, 27 resolved, 4 skipped links, 27 items queued/downloaded, 79 files downloaded, 474005235 bytes written, 0 failed, 0 partial.
- Telegram rerun: 31 links considered, 27 resolved, 4 skipped links, 27 skipped items, 0 queued, 0 files downloaded, 0 bytes written.
- `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"` selected `pixiv_bookmark_sync` and called `pixiv.bookmarks.sync` with `full_sync:true`, `stop_on_known:false`, `store_cursor:false`, and no invented `limit` / `max_pages`.
- First Pixiv run: 11 pages scanned, 309 items collected/discovered, `collection_stop_reason:end_of_feed`, 307 items queued/downloaded, 2 skipped items, 1758 files downloaded, 2946174301 bytes written, 0 failed, 0 partial.
- Pixiv rerun: 11 pages scanned, 309 collected/discovered, 309 skipped, 0 queued, 0 files downloaded, 0 bytes written.
- `library.file.verify` reported 1837 checked files, 1837 valid, 0 missing, 0 corrupt, and 0 unknown. The active library size after verification was about 3.2G, and the active DB size was about 2.8M.
- DB summary after verification: downloaded media items by platform included Pixiv 309, Redgifs 10, Instagram 8, Reddit 3, and a few generic/source-host items. Downloaded media files included Pixiv 1800, Instagram 18, Redgifs 10, Reddit 5, and source-host/generic files.
- During Telegram inbox runs, Instagram resolver output still printed large `JSONDecodeError in public_request` HTML diagnostics to stdout/stderr. The runs succeeded, but this remains evidence for the open summary-only/logging hardening task.

## Latest systemd Timer MVP State

- Telegram inbox sync is the first timer-deploy target, but formal deployment should invoke it through `mediagent agent run "<task>"` rather than by calling deterministic tools directly.
- `.env.example` now documents `MEDIAGENT_TELEGRAM_INBOX_KEY` plus `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`, `MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`, or `MEDIAGENT_TELEGRAM_INBOX_CHAT` for default inbox selection.
- Local `.env` was updated with non-secret Telegram inbox selector values for the current live test: `MEDIAGENT_TELEGRAM_INBOX_KEY=mediagent_inbox` and the numeric inbox chat id.
- `telegram.inbox.collect_links` and `telegram.inbox.sync_links` can run without explicit `chat`/`chats` input when the default inbox env vars are configured.
- Live Telegram inbox execute verification on 2026-08-04 UTC used selector key `mediagent_inbox`, read existing cursor `links:mediagent_inbox=34`, collected 3 new links, resolved 3 links, downloaded 3 video files, wrote 40603018 bytes, and stored cursor `links:mediagent_inbox=38`.
- A follow-up dry-run and Agent Core execute run for `幫我同步更新下載 telegram inbox 中的內容` found 0 new links and 0 queued downloads after cursor `38`, confirming rerun cursor continuation for the current inbox.
- Pixiv bookmark sync now supports timer-safe `stop_on_known` scanning. When enabled, it starts from the newest bookmarks, scans up to bounded `max_pages`, and stops after a page containing a known terminal media item.
- In `stop_on_known` mode, Pixiv sync does not store the API pagination cursor when it stops on a known item, so the cursor cannot be mistaken for a Telegram-style continuation cursor.
- Agent Core Pixiv recurring sync now uses `pixiv.bookmarks.sync` with `stop_on_known:true` and bounded `max_pages` instead of an invented default item `limit`.
- Live Pixiv Agent Core dry-run on 2026-08-04 UTC for `幫我同步更新下載 pixiv bookmark 中的內容` scanned 1 page, collected 30 known bookmark items, reported `collection_stop_reason: known_item_seen`, queued 0 downloads, and wrote 0 files.
- A direct Pixiv dry-run with an alternate `MEDIAGENT_LIBRARY_DIR` also queued 0 downloads, confirming that changing library root does not reset DB-based media item dedupe.
- `deploy/systemd/user/` now contains local example user units, timers, JSON inputs, and a minimal runbook for Telegram inbox sync and Pixiv bookmark sync.
- Clean-state user-systemd verification on 2026-08-05 UTC removed the old library/live-test outputs, backed up the old SQLite DB to `mediagent-data/backups/mediagent.sqlite3.20260805014915.bak`, initialized schema v7, and kept credential files intact.
- The previous Agent Core failure for exact full-source tasks `下載所有 telegram inbox 內所有可下載的媒體來源` and `下載 pixiv bookmark 所有可下載媒體來源` has been addressed in code. The next verification step is a clean DB/library rebuild using those exact natural-language tasks.
- `systemctl --user start mediagent-telegram-inbox.service` succeeded on the clean DB: first run collected 31 links, resolved 27, skipped 4, downloaded 79 files, wrote 474005235 bytes, and stored cursor `links:mediagent_inbox=39`; second run found 0 new links and downloaded 0 files.
- `systemctl --user start mediagent-pixiv-bookmarks.service` succeeded after the Telegram run: first run scanned 1 page, collected 30 bookmarks, skipped 1 already-known explicit Pixiv item from Telegram, downloaded 29 bookmark items as 293 files, wrote 447025170 bytes, and did not store an API pagination cursor because it stopped on a known item; second run queued 0 and skipped 30.
- Post-verification library state: 372 downloaded file records, 372 valid files, 0 missing, 0 corrupt, and 0 unknown. The rebuilt library is about 880M.

## Latest Repair-Mode State

- Pixiv now has an offline `pixiv.library.reconcile` plan/apply flow. It updates legacy work-type metadata, atomically moves existing manga source pages from `photo` or legacy `comic` to `comic-pages`, moves sidecars with their media, quarantines known placeholder downloads, updates DB paths, and requires `confirm:true` for apply.
- Local development DB plan verification found 309 Pixiv items: 26 comic, 280 illustration, 3 animation, and 17 unavailable placeholder records, with 0 blocked actions. The local library no longer contained 245 legacy comic source files at their recorded paths, so those require opt-in repair rather than an in-place move.
- Files under `.trash` are treated as missing library files and are never moved back automatically. `repair_missing_files:true` downloads a new copy to the planned library path while leaving `.trash` untouched.
- The locked offline suite passes 271 tests, including Pixiv work classification, unavailable placeholder rejection, reconciliation plan/apply/confirmation, atomic comic-page/sidecar moves, placeholder quarantine, missing-file repair, Kavita one-shot/series CBZ metadata and layout, V1 quarantine migration, long-Unicode path safety, missing-source refusal, DB recording, rerun reuse, and bookmark-sync packaging integration.

- `link.media.sync` supports explicit file-health-aware repair with `repair_missing_files: true`.
- `telegram.inbox.sync_links` and `telegram.messages.sync` expose the same option as compatibility paths over their existing sync logic.
- Default reruns remain conservative: downloaded items are skipped unless repair mode is explicitly enabled.
- Repair mode queues downloaded items only when required file records are missing/corrupt/unhealthy or when a DB row says `downloaded` but `local_path` no longer exists.
- Dry-run repair uses the same candidate selection and returns `planned_downloads` without writing files or mutating the DB.
- Focused regression tests cover missing-file queueing, healthy downloaded skip behavior, unchanged default reruns, and dry-run no-write planning.
- Live DB dry-run repair planning on 2026-08-03 UTC considered 12 unique source URLs from 14 missing downloaded file records. It resolved/planned 8 repair downloads across 4 providers, skipped 4 links during resolution, wrote 0 bytes, downloaded 0 files, and left the live DB at 675 downloaded file records with the same 14 files missing on disk.
- A bounded non-dry repair run on 2026-08-03 UTC used the same 12-source scope, downloaded 8 repaired files across Danbooru, nhentai, Redgifs, and rule34, wrote 76755767 bytes, and had 0 failed/partial items.
- Post-repair `library.file.verify` reports 669 valid files, 6 missing files, 0 corrupt, and 0 unknown across 675 downloaded file records. The remaining 6 missing rows are all Reddit records from 4 unique source URLs; a diagnostic dry-run reports `requires_auth:login_required` for those sources.

## Telegram Inbox Message-Link Bridge State

- `telegram.inbox.sync_links` now separates external URLs from Telegram message links in one inbox message. External URLs retain the shared resolver/download path; public and private `t.me` / `telegram.me` message links delegate to Telegram-native collect/sync/download logic.
- Telegram-native items retain the inbox chat ID, source message ID/date, collector run ID, and merged source provenance without persisting inbox message text.
- Protected, missing, private, deleted, or otherwise inaccessible linked messages return per-link structured skips instead of aborting the inbox run.
- `retry_auth_skipped:true` on `link.media.sync` retries old auth-dependent queue rows; the same option on `telegram.inbox.sync_links` is scoped to Telegram-ingested rows. Both paths claim rows with leases and require explicit operator intent.
- Fake-client regressions cover public, private, inaccessible, protected, mixed external-plus-Telegram, and old auth-skip retry paths. No live download was performed for this implementation.

## Verified

Last known verification commands:

```bash
uv run --locked python -m unittest discover -s tests
uv lock --check
uv run --locked mediagent tools list --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect reddit.saved.collect --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools inspect pixiv.link.resolve --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
PIXIV_ACCESS_TOKEN= PIXIV_REFRESH_TOKEN= PIXIV_CREDENTIALS_FILE= uv run --locked mediagent tools run pixiv.link.resolve --input examples/tools/pixiv.link.resolve.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

The latest local full suite has 260 passing tests.

Phase 16 Telegram inbox link resolver verification:

- `link.resolve.preview` and `link.resolve.to_media_item` are implemented as experimental tools. `telegram.inbox.collect_links` and `telegram.inbox.sync_links` are hidden stable tools for Agent SKILL usage.
- Normal `mediagent tools list` hides experimental tools and hidden low-profile tools; `--include-experimental` shows experimental tools, while hidden tools remain callable by name.
- Normal `mediagent tools run link.resolve.to_media_item` rejects execution with `experimental_tool_not_allowed`.
- Top-level `mediagent --help` does not expose the hidden `experimental` command path.
- Tests cover URL normalization, `normalized_url` uniqueness, userinfo rejection, malformed URL skip behavior, unsafe schemes, localhost/private IP rejection, unresolved host rejection, redirect limits, unsupported MIME rejection, `.mov` / `video/quicktime`, generic single-media HTML discovery, HEAD-forbidden HTML fallback, X age/login wall skip behavior, Imgur single-page resolution, ambiguous multi-media skip, Pixiv artwork-link `requires_auth`, duplicate Telegram URL queueing, origin-source storage layout, Telegram provenance metadata without raw message text, safe GET redirect revalidation, oversized GET body rejection, and MOV redirect-to-non-media rejection.
- Isolated live network smoke verification resolved and downloaded `https://www.gstatic.com/webp/gallery/1.jpg` into a temporary scanner-friendly path: `gstatic_com/photo/2026/07/20260728__gstatic_com__url_3e125a8d7d4f4d6e6dea2830__p0.jpg`, 44891 bytes, `image/jpeg`, checksum present, DB file record written, metadata sidecar written, temporary directory cleaned up.
- Real Telegram auth status was usable. Real Telegram inbox sync was exercised against the local `inbox` channel using integer chat selector `3779502941`. Phase 16 live verification proved generic public HTML handling for nhentai/Danbooru and X login-wall skipping. Reddit short links are now handled by Phase 17, below.

Phase 17/18 Reddit explicit-link resolver verification:

- Fake-client tests cover direct `i.redd.it`, direct `v.redd.it` MP4 video-only resolution, modern Reddit `shreddit-post` extraction, modern JS verification fallback to `old.reddit.com`, explicit `v.redd.it/...DASH_*.mp4` extraction from Reddit pages, highest DASH MP4 candidate selection, gallery skip behavior, and direct `v.redd.it/<id>` manifest skip behavior.
- Reddit MP4 resolutions map to `media_type: "video"`, `part: "v0"`, `library/reddit/video/...`, and metadata marks `audio_status: "not_merged"` / `mux_required: true` until audio muxing is implemented.
- Telegram inbox sync fake-client coverage proves Reddit MP4 links download under `library/reddit/video/...` while Telegram remains only `ingested_from`.
- Real Telegram auth status was usable on 2026-07-29 UTC.
- Real Telegram inbox sync against chat selector `3779502941` collected 5 external links and resolved 4. The X link was skipped as `requires_auth` with `login_or_age_gate`.
- The Reddit share link resolved through `reddit_media_link` using `old.reddit.com` fallback and downloaded one JPEG to `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/reddit/photo/2026/07/20260728__reddit__t3_1v8yi6w__p0.jpg`.
- The same live run also downloaded one rule34 PNG, one nhentai JPEG, and one Danbooru PNG into `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/<platform>/photo/2026/07/...`.
- Second-run dedupe succeeded with 0 queued downloads and 0 bytes written.
- `library.file.verify` checked 4 live-test files: 4 valid, 0 missing, 0 corrupt, 0 unknown.

Phase 19 link-first live verification:

- Stable core link tools `link.queue.upsert` and `link.media.sync` are implemented and discoverable without experimental flags.
- Public CLI entry point `mediagent link sync <url>` delegates to `link.media.sync`, so non-Telegram link automation uses the same resolver/download/storage pipeline as the Telegram inbox compatibility wrapper.
- Public CLI live smoke re-ran a known Redgifs URL through `mediagent link sync <url>`; it resolved through the same pipeline, skipped the already-downloaded item, and wrote 0 duplicate bytes.
- Queued `link.media.sync` runs claim ready links with `lease_owner` / `lease_expires_at`, ignore non-expired leases from other workers, and schedule retryable failures as `deferred` records with bounded `next_attempt_at` backoff.
- Reddit explicit links can delegate a single publicly visible external post URL back into the resolver chain. Redgifs delegated results keep Redgifs storage/layout while preserving Reddit upstream metadata.
- Telegram inbox compatibility wrapper `telegram.inbox.sync_links` was live-run on 2026-07-29 UTC against chat selector `3779502941` with `store_cursor:false` and output root `/home/ion/projects/mediagent/mediagent-data/live-test-phase19/library`.
- The first run collected 13 external links, resolved 9, queued/downloaded 6 new media items, skipped 4 links with structured reasons, and had 0 failed/partial downloads.
- A previously skipped Reddit gallery link was re-run through `link.media.sync`; anonymous `old.reddit.com` public HTML exposed `preview.redd.it` candidates, and preview fallback downloaded 3 JPEG files for `t3_1v8boac`.
- The latest compatibility-wrapper rerun collected 13 links, resolved 12, skipped 1 expected X/auth link, downloaded 2 new Reddit-delegated Redgifs MP4 files, skipped 10 already-known items, and had 0 failed/partial downloads.
- Downloaded files in the Phase 19 live-test library are 5 Redgifs MP4 videos and 6 Reddit photo/GIF/JPEG files under `library/redgifs/video/2026/07/...` and `library/reddit/photo/2026/07/...`, totaling 211178527 bytes.
- `library.file.verify` with platform selectors confirmed 5/5 Redgifs files valid and 6/6 Reddit files valid. No `.partial` or `.tmp` files remained.

Phase 20 Instagram foundation verification:

- Stable `instagram.auth.status`, `instagram.auth.login`, `instagram.auth.ensure_session`, and `instagram.link.resolve` are implemented, registered in the default tool registry, and covered by fake-client regression tests.
- The saved Instagram session at `/home/ion/projects/mediagent/mediagent-data/credentials/instagram_session.json` exists locally with `0600` permissions and must be treated as a credential.
- `instagram.link.resolve` is platform-bound: non-Instagram direct media is rejected with `instagram_media_unsupported`, and out-of-root saved-session paths return `unsafe_credential_path` before fake-client callbacks, real-client loads, or network work.
- One Instagram post URL represents the whole post. Carousel/multi-resource posts download every resource by default; `img_index` is preserved only as source metadata unless a future explicit option changes that behavior.
- Instagram CDN media URLs are signed/expiring runtime data. They are used only during the download run and are not persisted to SQLite, sidecar metadata, logs, snapshots, or tool output.
- Direct formal-tool live verification on 2026-07-30 UTC resolved 3/3 user-provided Instagram links with 0 auth/rate-limit/checkpoint failures, then `link.media.sync` downloaded 9 files under `/home/ion/projects/mediagent/mediagent-data/library/instagram/`: 7 JPEG photos and 2 MP4 videos.
- The two direct `/p/<shortcode>/` links were carousels: one downloaded 3 JPEG resources, and one downloaded 4 JPEG resources plus 1 MP4 resource. The direct `/reel/<shortcode>/` link downloaded 1 MP4 resource.
- Telegram inbox live verification on 2026-07-30 UTC collected user-posted Instagram links, resolved 3/3 selected Reel links, downloaded 3 MP4 files under `/home/ion/projects/mediagent/mediagent-data/library/instagram/video/2026/07/`, and a rerun skipped all 3 already-downloaded items with 0 duplicate bytes.
- Filesystem verification showed valid JPEG/MP4 container types, no `.partial` or `.tmp` files under the Instagram library root, and correct mixed-carousel layout: photo resources under `instagram/photo/...`, video resources under `instagram/video/...`.
- SQLite/sidecar checks found 6 Instagram media items and 12 media-file rows for the direct plus inbox live tests, all using stable Instagram post/resource URLs rather than signed CDN hosts.

Reddit foundation verification:

- `reddit.auth.start`, `reddit.auth.exchange`, `reddit.auth.refresh`, and `reddit.auth.status` are implemented and discoverable through CLI.
- `reddit.saved.collect` is implemented and discoverable through CLI.
- Fake-client tests cover auth URL generation, token exchange credential-file writing, refresh token preservation, status checks, redaction, generic user-agent rejection, unsafe credential paths, saved-listing normalization, cursor storage, dry-run no DB writes, unsafe collector DB paths, media-type filtering, saved comment skip, and unsupported embed skip.
- `reddit.saved.collect` returns normalized media items only and does not write `media_files`.
- Reddit auth/saved live verification is deferred unless auth-assisted account collection is explicitly resumed.

Cleanup/recovery foundation verification:

- `core.cleanup.media_state` is covered for dry-run planning with no file or DB mutation.
- Apply mode requires `confirm: true`.
- Apply mode quarantines existing media files before resetting matching media items to `discovered` and removing matching media file rows.
- Credential paths are protected and are not exposed as actionable cleanup file paths.
- Unsafe quarantine paths are rejected.

Telegram foundation verification:

- `telegram.auth.login` is covered for login-code start, complete with `password_ref`, dry-run without configured Telegram credentials, missing code/hash validation, inline password rejection, and secret redaction.
- `telegram.auth.status` is covered for missing config, unsafe session paths, usable fake sessions, and secret redaction.
- `telegram.dialogs.list` is covered for filtered dialog listing without message/media content.
- `telegram.messages.collect` is covered for explicit chat selection, media type filtering, protected-content exclusion, album/grouped media normalization, private/public message-link parsing, curated link-inbox extraction, linked media resolution, and scoped cursor storage.
- `telegram.media.download` is covered for safe writes, `.partial` finalization, checksum output, MIME validation, and path safety.
- `telegram.media.download` is covered for malformed direct and nested `download_ref` input returning `telegram_download_missing_ref` instead of a generic runtime error.
- `telegram.messages.sync` is covered for collect -> upsert -> status filter -> storage path plan -> Telegram-specific download -> file record -> item status update -> scoped cursor storage.
- `telegram.messages.sync` is covered for download cancellation after `.partial` creation: it records failed file/item/run state and removes the partial file.
- Telegram dry-run sync with a fake client proves no DB or library files are written.
- Telegram real login, auth status, curated link-inbox collection, small media download, long-video download, layout placement, `library.file.verify`, and rerun dedupe were live-verified on 2026-07-24 UTC.
- Telegram real downloads now stream directly to `.partial`, treat `timeout_seconds` as a no-progress idle timeout, compute checksums in chunks, and finalize with an atomic move.

Deterministic Pixiv sync verification:

- `pixiv.bookmarks.sync` is covered with fake-client tests for successful multi-file download, second-run skip behavior, dry-run planning with no writes, partial failure, path safety, Pixiv `Referer`, scanner-friendly storage layout, file records, item status updates, and safe cursor advancement.
- `pixiv.bookmarks.sync` has regression coverage for photo-only sync cursor storage after media-type filtering.
- `storage.path.plan` has regression coverage for platform-specific library roots.
- `storage.path.plan` and `pixiv.bookmarks.sync` have regression coverage for the `scanner-friendly-v2` platform layer and for avoiding duplicate platform directories under platform-specific roots.
- Old-style SQLite DBs missing `media_items.downloaded_at` are migrated by `core.db.init` / tool initialization before `media.item.set_status` updates downloaded state.

Phase 21 Pixiv explicit-link implementation verification completed on 2026-08-03 UTC:

- `pixiv.link.resolve` is implemented as a stable public tool for one Pixiv artwork URL or `illust_id`.
- The core `pixiv_artwork_link` resolver uses Pixiv artwork detail, produces normalized media candidates, supports multi-page works, preserves ugoira zip candidates, and returns structured Pixiv auth/rate-limit/unavailable errors.
- `link.media.sync` can consume Pixiv artwork URLs directly, dedupe against existing Pixiv bookmark-sync items/files, and apply the required Pixiv `Referer` without persisting runtime headers.
- Fake-client tests cover URL/id parsing, localized aliases, artwork detail request shape, multi-page resolution, ugoira zip candidates, missing credentials, unsafe credential-file paths, `pixiv.link.resolve` platform boundary, Pixiv `Referer`, and bookmark-sync dedupe.
- CLI inspect works for `pixiv.link.resolve` and `link.media.sync`. A no-credential dry-run returns structured `pixiv_auth_missing_credentials` with `recommended_tool: "pixiv.auth.login"`.

Phase 21 Telegram inbox live verification completed on 2026-08-03 UTC:

- Interpreted the natural-language task "download all new media in inbox" as `telegram.inbox.sync_links` with the configured inbox chat, cursor storage enabled, and the shared link resolver/download pipeline.
- First live run collected 27 external links, considered 27, resolved 24, skipped 3, queued 9 new media items, downloaded 9 items / 22 files, wrote 134098941 bytes, and had 0 partial / 0 failed items.
- Pixiv explicit links resolved through `pixiv_artwork_link`: `112418327` downloaded 4 files under `library/pixiv/photo/2023/10/...`; `137814756` resolved to 38 already-known valid files and was skipped by dedupe.
- The second live run collected 0 links and downloaded 0 files, confirming cursor advancement for the inbox path.
- `library.file.verify` checked 675 DB file records: 661 valid, 14 missing, 0 corrupt, 0 unknown. The 14 missing rows were older already-recorded link-first live-test files, not files downloaded by this run.
- The 22 newly downloaded artifact paths all exist. Pixiv persisted media metadata has no runtime headers or tokens, and Pixiv link resolution rows no longer persist `runtime_headers` or runtime `download_context` keys.

Live Pixiv verification completed on 2026-07-21 UTC:

- `pixiv.auth.status` returned a usable session for the user-provided account.
- `pixiv.bookmarks.collect` returned 30 public bookmark items.
- `download.http` downloaded one JPEG bookmark image to `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test/143734851_p0.jpg`.
- Download verification: 330936 bytes, `image/jpeg`, checksum `sha256:72c9988b5d32786423966ff7aae99166041b532571a83f7e4bda1adcd442e2fe`.

Phase 11 live storage verification completed on 2026-07-22 UTC:

- Removed the old live Pixiv download output under `/home/ion/projects/mediagent/mediagent-data/media`.
- Reset Pixiv media item/file/cursor state in `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` while preserving credentials.
- Re-collected 11 Pixiv public bookmark pages: 309 raw bookmark items, 306 photo items, 1797 image files.
- Re-downloaded all 1797 image files to `/home/ion/projects/mediagent/mediagent-data/library` using `scanner-friendly-v1`.
- Public library verification: 1797 media files, 0 JSON sidecars, 0 `.partial` files.
- SQLite verification: schema version `5`, 306 Pixiv photo items marked `downloaded`, 1797 Pixiv media files marked `downloaded`, all with `library_relative_path`, `storage_layout = scanner-friendly-v1`, and `file_health = valid`.
- `library.file.verify` checked 1797 files: 1797 valid, 0 missing, 0 corrupt, 0 unknown.
- Second-run dedupe check: 306 Pixiv photo items would be skipped, 0 would be downloaded again.
- Committed `pixiv.bookmarks.sync` was then run non-dry with `max_pages = 20` and `media_types = ["photo"]`; it scanned 11 pages, skipped all 306 downloaded photo items, queued 0 downloads, and recorded one successful tool run in SQLite.

Pixiv live artifact cleanup completed on 2026-07-24 UTC:

- Removed the old single-file Pixiv smoke output under `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test`.
- Removed the old full Pixiv live library output under `/home/ion/projects/mediagent/mediagent-data/library`.
- Removed the empty `/home/ion/projects/mediagent/mediagent-data/pixiv` directory.
- Reset Pixiv `media_items`, `media_files`, and `sync_cursors` in `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` to 0.
- Preserved `/home/ion/projects/mediagent/mediagent-data/credentials/pixiv-oauth.json`.

Phase 13 Telegram + Pixiv layout live verification ran on 2026-07-24 UTC:

- Telegram `telegram.auth.login` was completed with a user-provided app code, and `telegram.auth.status` reported a usable session.
- Telegram `telegram.dialogs.list` found the user-controlled collection channel.
- Telegram `telegram.messages.collect` resolved 3 curated message links into 3 media items: one long private video, one small video/GIF-style file, and one photo.
- The long private Telegram video initially exposed the real-download buffering issue and was marked `failed`, then was successfully retried after stream-safe download support landed.
- Telegram direct link sync downloaded 2 small media files to the shared scanner-friendly library:
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov`
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg`
- Re-running the same Telegram direct link sync queued 0 downloads and skipped 2 already-downloaded items.
- Telegram stream-safe long-video sync downloaded `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4`, wrote 660481192 bytes, and failed 0 files.
- Re-running the same long-video sync queued 0 downloads, skipped 1 already-downloaded item, and wrote 0 bytes.
- Bounded Pixiv sync with `max_pages = 4`, `limit = 100`, and `media_types = ["photo"]` collected 120 raw bookmark items, discovered 100 photo targets, downloaded 100 items / 624 files, wrote 1131771564 bytes, and failed 0 files.
- Pixiv files landed under `/home/ion/projects/mediagent/mediagent-data/library/pixiv/photo/2026/...` using `scanner-friendly-v2`.
- `library.file.verify` checked 627 files across Telegram and Pixiv: 627 valid, 0 missing, 0 corrupt, 0 unknown.
- Filesystem verification found 624 Pixiv files, 3 Telegram files, and 0 `.partial` files.
- A Pixiv second dry-run with the same bounded input queued 0 downloads and skipped 100 already-downloaded items.

## Instagram Saved Media Foundation

Instagram saved-media foundation and bounded local live verification completed on 2026-08-11 UTC:

- Added a sequential one-page saved-feed client with opaque pagination and structured session, checkpoint, and rate-limit failures.
- Added `instagram.saved.collect` and `instagram.saved.sync`, whole-post normalization for photos, Reels/videos, and carousels, runtime-only signed URLs, shared scanner-friendly storage/download/status/repair behavior, safe cursor advancement, and sidecar support.
- Registered the tools, added bounded/recurring/full JSON examples, and added an English `instagram_saved_sync` Agent SKILL that keeps saved-feed intent separate from explicit Instagram links and preserves unbounded “all saved media” requests.
- Review hardening prevents explicit DB paths outside configured write roots and prevents page-limit truncation from skipping unreturned posts behind an opaque cursor.
- The locked offline suite passes 260 tests, including pagination, dedupe, carousel resources, CLI example inputs, partial failure, cursor safety, dry-run isolation, auth/rate-limit errors, downloading, retry, repair, and Agent intent boundaries.
- A local-only bounded live run used one saved-feed page and synchronized the first 2 posts. Both were Reels/videos; 2 files totaling 16,746,907 bytes downloaded successfully.
- The second identical run queued and downloaded 0 files, skipped both healthy items, and `library.file.verify` reported 2 valid, 0 missing, and 0 corrupt files.
- SQLite inspection found 0 persisted runtime CDN/session/auth markers. The dedicated local live-test DB, library, and temporary output were removed afterward. The bounded sample did not contain a carousel, so real carousel downloading remains covered offline rather than by this live run.

## Not Implemented

- Workflow V1 runner
- built-in scheduler
- cron examples
- live X OAuth verification with a real X account and app credentials
- live Reddit OAuth/saved-collection verification with a real Reddit account and app credentials, now deferred unless auth-assisted collection is explicitly resumed
- Reddit audio muxing, DASH/HLS manifest handling, and complex multi-file `v.redd.it` support
- `reddit.saved.sync`, now deferred unless auth-assisted collection is explicitly resumed
- Pixiv localhost callback server
- Instagram stories, profile scraping, messaging, posting, comments, likes, follows, and broad account collection outside the saved-feed boundary
- Instagram session status TTL and extra edge-case fixtures for checkpoint/2FA/rate-limit/thumbnail-only Reel cases
- visual workflow editor

## Next Recommended Task

Proceed with the systemd timer-hardening focus in `TODO.md`: deployment environment validation, overlapping-run protection, concise journal output, source-aware Pixiv stop-on-known behavior, and one consistent timer-safe failure policy. Reddit OAuth/saved collection and X live auth verification remain deferred legacy/advanced paths.
