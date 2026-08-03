# Mediagent Current State

## Implemented

- Package layout exists under `src/mediagent/`.
- `main.py` is a thin startup entry.
- Console script `mediagent = mediagent.cli:main` is configured in `pyproject.toml`.
- Tool contract exists in `src/mediagent/core/tooling.py`.
- Tool registry exists through `src/mediagent/tools/defaults.py`.
- CLI bridge exists in `src/mediagent/cli.py`.
- SQLite schema initialization exists in `src/mediagent/core/db.py`.
- Current SQLite schema version is `7`, with idempotent migration support for old media item/file tables and the stable `link_queue` lifecycle/retry/provenance fields.
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
- Deterministic sync helpers exist in `src/mediagent/core/sync.py`.
- Universal storage planning exists in `src/mediagent/core/storage.py`.
- The default shared-root storage layout is `scanner-friendly-v2`: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`.
- Platform-specific library roots are supported through `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`, for example `MEDIAGENT_PIXIV_LIBRARY_DIR`.
- Platform-specific roots are treated as already scoped to that platform, so they omit the extra platform directory by default.
- Pixiv bookmark sync now performs collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update.
- Pixiv bookmark sync stores scoped cursors when `media_types` filtering is used, such as `bookmarks:public:photo`.
- Telegram message sync stores per-source scoped cursors when durable processing succeeds, such as `messages:saved_messages:photo-video`.
- Undocumented Telegram inbox link resolver support exists behind experimental boundaries. It treats Telegram as ingest provenance and uses the resolved `origin_source` for media items and storage layout.
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
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links` (experimental)
- `telegram.inbox.sync_links` (experimental)
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

## Latest Repair-Mode State

- `link.media.sync` supports explicit file-health-aware repair with `repair_missing_files: true`.
- `telegram.inbox.sync_links` and `telegram.messages.sync` expose the same option as compatibility paths over their existing sync logic.
- Default reruns remain conservative: downloaded items are skipped unless repair mode is explicitly enabled.
- Repair mode queues downloaded items only when required file records are missing/corrupt/unhealthy or when a DB row says `downloaded` but `local_path` no longer exists.
- Dry-run repair uses the same candidate selection and returns `planned_downloads` without writing files or mutating the DB.
- Focused regression tests cover missing-file queueing, healthy downloaded skip behavior, unchanged default reruns, and dry-run no-write planning.
- Live DB dry-run repair planning on 2026-08-03 UTC considered 12 unique source URLs from 14 missing downloaded file records. It resolved/planned 8 repair downloads across 4 providers, skipped 4 links during resolution, wrote 0 bytes, downloaded 0 files, and left the live DB at 675 downloaded file records with the same 14 files missing on disk.
- A bounded non-dry repair run on 2026-08-03 UTC used the same 12-source scope, downloaded 8 repaired files across Danbooru, nhentai, Redgifs, and rule34, wrote 76755767 bytes, and had 0 failed/partial items.
- Post-repair `library.file.verify` reports 669 valid files, 6 missing files, 0 corrupt, and 0 unknown across 675 downloaded file records. The remaining 6 missing rows are all Reddit records from 4 unique source URLs; a diagnostic dry-run reports `requires_auth:login_required` for those sources.

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
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
PIXIV_ACCESS_TOKEN= PIXIV_REFRESH_TOKEN= PIXIV_CREDENTIALS_FILE= uv run --locked mediagent tools run pixiv.link.resolve --input examples/tools/pixiv.link.resolve.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

The latest local full suite has 200 passing tests.

Phase 16 Telegram inbox link resolver verification:

- `link.resolve.preview`, `link.resolve.to_media_item`, `telegram.inbox.collect_links`, and `telegram.inbox.sync_links` are implemented as experimental tools.
- Normal `mediagent tools list` hides experimental tools; `--include-experimental` shows them.
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
- Telegram real downloads now stream directly to `.partial`, enforce `timeout_seconds` around the Telethon download call, compute checksums in chunks, and finalize with an atomic move.

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

## Not Implemented

- Workflow V1 runner
- built-in scheduler
- cron examples
- live X OAuth verification with a real X account and app credentials
- live Reddit OAuth/saved-collection verification with a real Reddit account and app credentials, now deferred unless auth-assisted collection is explicitly resumed
- Reddit audio muxing, DASH/HLS manifest handling, and complex multi-file `v.redd.it` support
- `reddit.saved.sync`, now deferred unless auth-assisted collection is explicitly resumed
- Pixiv localhost callback server
- Instagram feed, saved-post, stories, profile scraping, messaging, posting, comments, likes, follows, and broad account collection
- Instagram session status TTL and extra edge-case fixtures for checkpoint/2FA/rate-limit/thumbnail-only Reel cases
- LLM Agent Core
- visual workflow editor

## Next Recommended Task

File-health-aware repair mode is implemented and bounded live repair has restored the resolvable missing files. The next recommended task is deciding how to handle the 6 remaining historical Reddit rows that now hit `requires_auth:login_required`: leave them as known missing, reset/quarantine them with cleanup tooling, or defer them until Reddit auth/resolver work resumes.

Treat Reddit OAuth/saved collection and X live auth verification as deferred legacy/advanced paths. Do not start Workflow V1 or Agent Core until the link-first provider-adapter contract remains stable through at least one more provider adapter or repeated cron-style runs, unless the user explicitly chooses workflow work next.
