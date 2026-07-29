# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep detailed status, verification history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Completed Baseline

The following foundations are complete enough to treat as the current baseline:

- Python package layout under `src/mediagent/`
- Tool contract, registry, and CLI bridge
- Bottom tools for env, DB, paths, run records, media items, media files, HTTP download, metadata writing, sync cursors, storage path planning, and library verification
- Credential/auth foundation with redaction and credential-file boundaries
- Universal scanner-friendly storage layout: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`
- X auth and bookmark collection with fixture/fake-client tests, pending live verification
- Pixiv auth, bookmark collection, and deterministic `pixiv.bookmarks.sync`
- Pixiv bounded live layout verification with 100 bookmark items / 624 photo files using `scanner-friendly-v2`
- Telegram media-source foundation: explicit local `telegram.auth.login`, Telethon-backed user-session config, auth status, dialog listing, message/link-inbox collection, Telegram-specific media download, deterministic message sync, CLI examples, and fake-client tests
- Telegram real login, curated link-inbox collection, small photo/video download, and rerun dedupe have been live-verified
- Telegram stream-safe real downloads and one-hour video live verification are complete for the current phase
- Phase 16 undocumented Telegram inbox link resolver foundation exists behind experimental boundaries: URL queueing, URL safety, direct media / generic single-media HTML / Imgur single-page / Pixiv artwork-link resolver behavior, origin-source storage metadata, link-safe download, and regression tests
- Phase 17/18 Reddit explicit-link resolver foundation exists for credential-light single-media links: direct `i.redd.it` images, direct `v.redd.it` MP4 video-only files, Reddit post/share links, bounded anonymous HTML, `old.reddit.com` fallback with static `over18=1`, structured skips for unsupported gallery/manifest cases, Reddit metadata preservation, Telegram-inbox live verification, dedupe verification, and file verification
- Conservative cleanup/recovery foundation exists through `core.cleanup.media_state`, with dry-run planning, manifest output, explicit apply confirmation, quarantine-before-DB-reset behavior, and credential path protection
- Reddit foundation exists: OAuth config/auth tools, saved-listing collector, media parser for image/gallery/video/direct media shapes, CLI examples, credential path safety, cursor path safety, and fake-client tests

Do not expand completed phases here. Add only short baseline notes when they directly affect future work.

## Current Focus: Phase 18 Link Resolver Hardening And Multi-File Readiness

Goal: harden the link-first resolver path after the first successful Reddit live test, without turning it into an unrestricted crawler.

The current resolver path proves the desired shape:

```text
explicit user-provided link
-> URL normalization and uniqueness check
-> resolver registry
-> normalized media item
-> existing sync/download/storage pipeline
```

### 18A. Finish Reddit Single-Media Coverage

- [ ] Add fake-client coverage for `redd.it/<post_id>` short URLs that redirect to post pages.
- [ ] Add fake-client coverage for direct `old.reddit.com` input links.
- [ ] Add fake-client coverage for `shreddit-screenview-data` JSON extraction.
- [ ] Add tests proving preview/thumbnail Reddit URLs are ignored when a clear original `i.redd.it` image exists.
- [ ] Add structured skip tests for no-media pages, blocked pages, deleted/removed pages, login-required pages, quarantined pages, and ambiguous multi-image pages.
- [x] Add Telegram inbox sync fake-client coverage proving Reddit links download under `library/reddit/...` while Telegram remains only `ingested_from`.
- [x] Add direct `v.redd.it` MP4 support before generic direct-media fallback.
- [x] Add Reddit post/legacy-page extraction for explicit `v.redd.it/...DASH_*.mp4` candidates.
- [x] Add tests proving Reddit MP4 links map to `video`, `v0`, and `library/reddit/video/...`.

### 18B. Prepare The Multi-File Resolver Contract

- [ ] Keep the current public result shape compatible with one resolved media item.
- [ ] Draft an internal resolver result shape that can later return multiple files from one input link.
- [ ] Map the future multi-file result into the existing media item `metadata.files` format.
- [ ] Do not enable Reddit galleries or multi-stream video muxing until the multi-file shape is covered by unit tests.
- [ ] Keep storage layout unchanged: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`.

### 18C. Next Provider Link Resolvers

- [ ] Plan an explicit Pixiv artwork-link resolver that can reuse existing Pixiv auth and artwork parsing instead of bookmark access.
- [ ] Plan an explicit X post-link resolver separately from X bookmark APIs, with clear handling for login walls and anti-bot limits.
- [ ] Keep generic HTML resolver conservative: one clear public media file only, no JavaScript execution, no credential scraping, no page dumps.

### 18D. Reddit Deferred Scope

- [ ] Keep Reddit OAuth live verification pending while credentials are unavailable.
- [ ] Defer `reddit.saved.sync` until explicit-link behavior and the collector output shape are stable.
- [ ] Defer Reddit galleries until the resolver contract cleanly supports one link producing multiple files.
- [ ] Defer Reddit audio muxing, DASH/HLS manifest handling, and full multi-file `v.redd.it` support until the ffmpeg/dependency strategy and multi-file resolver contract are stable.
- [ ] Do not add Reddit posting, commenting, voting, save/unsave, moderation, or chat-management features.

### 18E. Reddit Video Mux And Managed FFmpeg Plan

- [ ] Decide whether Mediagent should manage a project-local ffmpeg binary, accept an explicit `MEDIAGENT_FFMPEG_PATH`, or support both.
- [ ] Add a tool-safe ffmpeg capability check that reports version and supported codecs without modifying PATH.
- [ ] Plan one media item with multiple source files so Reddit video and audio tracks can be downloaded separately, then muxed into one final file.
- [ ] Keep direct single MP4 video-only downloads supported while muxing is unavailable.
- [ ] Add tests proving audio-only MP4 candidates are not saved as user-facing video files.

## Side Decisions

- [ ] X live OAuth verification remains pending because API access may require paid credits.
- [ ] Plan X and Pixiv explicit-link resolvers after Phase 18 hardening, so inbox automation can download from explicit post/artwork links without relying on bookmark/feed access.
- [ ] Discuss whether Pixiv needs additional source tools beyond bookmarks, such as following-user works or explicit artwork IDs.
- [ ] Keep Telegram link resolver behavior undocumented until the user explicitly decides to promote it.
- [ ] Keep Instagram deferred until Pixiv, Telegram, Reddit, and X boundaries are stable.

## Design Decision: Option B Hidden Telegram Link Resolver

Goal: implement the Telegram inbox external-link feature as a bounded resolver pipeline, not as a domain allowlist and not as an unrestricted crawler.

This keeps the feature useful for user-curated Telegram inboxes while keeping the security and maintenance surface bounded.

Target shape:

```text
Telegram inbox message
-> external URL extraction
-> URL normalization and uniqueness check
-> resolver chain
-> normalized media item
-> existing sync/download/storage pipeline
```

Implementation boundaries:

- Telegram remains the ingest source and provenance only; storage platform comes from the resolved `origin_source`.
- `link_queue.normalized_url` is the uniqueness key, so duplicate pasted links do not create duplicate work.
- Resolver behavior must stay behind experimental/undocumented tool boundaries until explicitly promoted.
- Do not require a domain allowlist for public HTML pages.
- First-version resolvers should stay limited to public HTTPS direct media URLs, bounded public HTML parsing for one clear media target, and small explicit provider adapters only when needed.
- Direct media support includes bounded image/video MIME types, including `.mov` / `video/quicktime`.
- Public HTML pages may be supported only when the resolver can deterministically identify exactly one downloadable media file.
- Login-required pages, multi-media pages, JavaScript-rendered pages, URL shortener expansion beyond safe redirects, and unknown providers are skipped with structured reasons.
- The actual GET download must repeat URL safety checks, redirect validation, MIME validation, and byte limits instead of trusting the preview result.
- Metadata should store the resolved source, original Telegram provenance, normalized URL, checksum, MIME type, and file size, but never raw message text, credentials, cookies, or page dumps.

Testing targets:

- URL normalization and `normalized_url` uniqueness.
- Userinfo, malformed URLs, unsafe schemes, localhost/private IPs, unresolved hosts, redirect limits, unsupported MIME, oversized responses, and redirect-to-non-media rejection.
- Direct media and `.mov` handling.
- Generic HTML media discovery from `og:image`, `og:video`, `twitter:image`, `twitter:player:stream`, `<video>`, `<source>`, `<a>`, and direct media URLs embedded in page data.
- Single-media provider resolution and multi-media provider skip behavior.
- Telegram inbox collection without storing raw message text.
- Dry-run no writes, no DB mutation, and no media-file creation.
- Hidden experimental boundary: normal tool listing hides these tools, normal inspect/run rejects them, and top-level help does not expose the hidden command path.

## Completed In This Phase: Phase 16 Undocumented Telegram Inbox Link Resolver

- [x] Added hidden experimental tool boundaries and CLI routing for Telegram inbox link sync.
- [x] Added URL normalization, unique `link_queue` storage, and SQLite schema version 6.
- [x] Added safe resolver behavior for direct media URLs, public single-image Imgur pages, and Pixiv artwork-link identification.
- [x] Added strict URL safety for schemes, userinfo, malformed URLs, DNS/private IPs, redirects, MIME types, `.mov`, and max media size.
- [x] Added link-safe GET download for Phase 16 instead of the generic downloader.
- [x] Preserved `origin_source` as the storage platform and Telegram as ingest provenance.
- [x] Added regression tests for all Phase 16 acceptance criteria and security issues recorded in `ISSUES.md`.
- [x] Ran isolated live network smoke verification; real Telegram inbox sync was also exercised but found 0 external URLs to download.

## Completed In This Phase: Cleanup / Recovery Foundation

- [x] Added `core.cleanup.media_state` as a conservative live-test cleanup/recovery tool.
- [x] `mode: "plan"` previews matching media items/files without mutating files or SQLite.
- [x] `mode: "apply"` requires `confirm: true`.
- [x] Apply mode moves existing media files into quarantine before resetting matching DB state.
- [x] The tool requires a platform selector and supports optional `remote_id` and `status` selectors.
- [x] Credential paths are protected and are not included as actionable cleanup files.
- [x] Tests cover dry-run no mutation, selector validation, credential protection, quarantine-before-reset, confirmation, and path safety.

## Later: RuleSpec Policy Layer

Do this after deterministic platform sync behavior remains stable across Pixiv and Telegram.

Goal: let users describe source selection and filtering rules without hard-coding each platform's curation model.

Proposed flow:

```text
platform collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

An LLM or Agent Core may help users translate natural-language intent into RuleSpec, but scheduled daemon runs should execute stored deterministic rules rather than asking an LLM to improvise every time.

## Later: Workflow, Scheduling, And Agentic Composition

- [ ] Add YAML Workflow V1 only after deterministic sync behavior is stable across Pixiv and Telegram.
- [ ] Keep cron/systemd as the scheduler until headless workflows are reliable.
- [ ] Add SKILL documentation for tool discovery and safe usage.
- [ ] Add Agent Core that calls the same registry, not platform internals.
- [ ] Add an agentic scheduler only after deterministic scheduling is reliable.

## Completed In This Phase: Telegram Large-Media Hardening

- [x] Real Telethon downloads stream directly to the planned `.partial` file instead of returning `bytes`.
- [x] Tool-level finalization validates the completed `.partial`, computes checksum in chunks, then atomically moves to final path.
- [x] `timeout_seconds` is enforced around the real Telegram download call.
- [x] Fake-client tests cover streaming to `.partial` and cleanup when streaming fails.
- [x] The one-hour Telegram video was downloaded to `${MEDIAGENT_DATA_DIR}/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4`.
- [x] Re-running the same Telegram sync skipped the completed long video.
- [x] `library.file.verify` checked 627 files with 627 valid.

## Explicit Non-Goals For Now

- [ ] Do not build a visual workflow editor before headless Workflow V1 is useful.
- [ ] Do not build LLM Agent Core before bottom/platform tool contracts are stable.
- [ ] Do not build a built-in scheduler before cron-compatible execution is reliable.
- [ ] Do not build media browsing, library management, sharing, forwarding, reposting, or chat-management features.
