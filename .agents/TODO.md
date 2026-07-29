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
- Phase 19 first stable link layer exists: schema-v7 `link_queue` lifecycle fields, active claim/lease and retry scheduling, source provenance merge, stable `link.queue.upsert`, stable `link.media.sync`, public `mediagent link sync <url>` CLI entry point, Redgifs direct/watch resolver foundation, Reddit static/preview gallery support, Reddit external-provider delegation, multiple file candidates for simple static groups, resolver header sanitization before persistence, and regression tests
- Conservative cleanup/recovery foundation exists through `core.cleanup.media_state`, with dry-run planning, manifest output, explicit apply confirmation, quarantine-before-DB-reset behavior, and credential path protection
- Phase 19 live verification is complete for the public `mediagent link sync <url>` entry point, Redgifs direct/watch links, Reddit-to-Redgifs delegation, anonymous Reddit single-file photo/GIF links, and one Reddit multi-image gallery through preview fallback. The latest compatibility wrapper rerun resolved 12/13 inbox links, skipped 1 expected X/auth link, downloaded 2 new delegated Redgifs MP4 files, and left 0 failed/partial downloads. The phase19 live-test library currently has 5 Redgifs MP4 files and 6 Reddit photo/GIF/JPEG files.
- Reddit foundation exists: OAuth config/auth tools, saved-listing collector, media parser for image/gallery/video/direct media shapes, CLI examples, credential path safety, cursor path safety, and fake-client tests. Treat this as deferred legacy/advanced capability unless the user explicitly resumes auth-assisted account collection.

Do not expand completed phases here. Add only short baseline notes when they directly affect future work.

## Completed Focus: Phase 19 Link-First Resolver Hardening

The operational Phase 19 slice is complete. Unchecked items inside this section are post-19 promotion, future provider planning, or deferred policy/test follow-ups; they are not blockers for the current stable link-first baseline.

Goal: make explicit user-provided links the primary product path.

The old auth-first path is no longer the main direction:

```text
auth
-> account bookmarks / saved items / feeds
-> automatic discovery
-> download
```

The new primary path is:

```text
explicit URL source
-> URL normalization and uniqueness check
-> link queue lifecycle control
-> safe resolver chain
-> normalized media candidates
-> deterministic candidate selection
-> existing media/download/storage pipeline
```

Pixiv bookmark sync remains supported as an exception because it is already implemented and useful. New platform work should start from explicit-link resolution before account collection.

### 19A. Public Link Tool Surface

- [x] Promote the current hidden link resolver work from a Telegram-only secret feature into a first-class core link workflow.
- [x] Keep the CLI surface conservative while stabilizing it; the implementation lives in core link tools, not inside Telegram-only code.
- [x] Add `link.queue.upsert` for URL intake and normalized-URL dedupe.
- [x] Add schema-v7 queue fields for lifecycle, retry metadata, source provenance, and future leases.
- [x] Keep permanent skips separate from retryable failures; login walls, unsupported domains, unsafe URLs, and ambiguous pages should not be retried indefinitely.
- [x] Merge source provenance when the same URL is submitted from multiple sources, such as CLI, Telegram inbox, workflow, and future Agent/SKILL calls.
- [x] Add `link.media.sync` as the deterministic orchestration tool: queue/read URLs, resolve, upsert media items, filter known items, plan storage paths, download files, write metadata, and record file state.
- [x] Allow URL input from CLI JSON, queued `link_id` records, Telegram inbox links, future workflow steps, and future Agent/SKILL calls.
- [x] Dry-run mode must not write files, mutate DB state, or create media-file records.
- [x] JSON output is stable enough for cron, workflows, and future agents in the current single-worker path.
- [x] Activate queue claim/lease behavior so concurrent cron or daemon runs do not process the same queued link.
- [x] Add retry scheduling with `next_attempt_at`, bounded attempts, and retryable skip handling.
- [ ] Promote or replace `link.resolve.preview` and `link.resolve.to_media_item` after the public preview/debug API is settled.

### 19B. Resolver Contract

- [x] Define `MediaCandidate` with JSON-compatible fields: `url`, `media_type`, `mime_type`, `extension`, `size_bytes`, `source`, `quality_rank`, `file_index`, `content_identity`, `persistable_headers`, `download_context_ref`, and `details`.
- [x] Treat `persistable_headers` as an allowlisted, non-secret set only. `Referer` may be persisted when required for public media delivery; `Authorization`, `Cookie`, bearer tokens, signed URL secrets, session headers, and CSRF headers must stay runtime-only and must never be written to SQLite, sidecar metadata, logs, or snapshots.
- [x] Strip credential-bearing candidate headers before persisting link resolution state.
- [x] Define `LinkResolution` with `status`, `skip_reason`, `original_url`, `normalized_url`, `canonical_url`, `aliases`, `final_url`, `origin_source`, `resolver_chain`, `auth_used`, `media_candidates`, `selected_candidate`, `warnings`, and `details`.
- [x] Require resolvers to emit a canonical source identity when available, such as `platform + remote_id`, provider media id, canonical post URL, or direct content URL.
- [x] Support multiple internal candidates for simple static file groups.
- [x] Define simple multi-candidate group semantics for static file groups: group id, required files, optional files, candidate ordering, partial-success status, and `metadata.files` mapping.
- [x] Use structured skip reasons such as `requires_auth`, `login_wall`, `unsupported_domain`, `unsupported_media_type`, `unsupported_multi_media`, `javascript_required`, `blocked`, `unsafe_url`, `too_large`, and `ambiguous_candidates`.
- [x] Preserve enough metadata for debugging and indexing, but never store raw HTML dumps, raw Telegram message text, cookies, tokens, or credential-bearing headers.
- [x] Keep storage layout unchanged: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`.

### 19C. Canonical Dedupe

- [x] Treat `link_queue.normalized_url` as only the first intake dedupe layer, not the final media identity.
- [x] Add a first link alias strategy so `redd.it/<id>`, `reddit.com/r/.../comments/<id>/...`, `old.reddit.com/...`, provider watch URLs, and direct media URLs can point to the same queued link or resolved source.
- [x] Use resolver output to dedupe at link aliases and `platform + remote_id` media item layers; known file records and checksums prevent target re-downloads.
- [x] Preserve all known source URLs as provenance without creating duplicate download work.
- [x] Ensure reruns can update resolution metadata for an existing link without resetting completed media-file state.

### 19D. Generic Resolver

- [x] Resolve direct public media URLs before fetching full HTML.
- [x] Support bounded image/video/audio MIME checks, including `.mov` / `video/quicktime`.
- [x] Revalidate redirects, final URL, MIME type, and size with HEAD, range GET, or bounded GET fallback.
- [x] Parse bounded public HTML for `og:image`, `og:video`, `twitter:image`, `twitter:player:stream`, `<video>`, `<source>`, direct media anchors, and simple JSON-LD/media URL fields.
- [x] Score candidates so obvious originals/full-size media beat thumbnails, icons, avatars, and decorative images.
- [x] Download only when one clear media candidate can be selected deterministically.
- [x] Return `ambiguous_candidates` or `unsupported_multi_media` instead of downloading when a page exposes multiple plausible media files.
- [x] Do not execute JavaScript, solve CAPTCHA, bypass DRM, scrape credentials, or keep page dumps.

### 19E. Reddit Resolver

- [x] Keep anonymous resolution first: direct `i.redd.it`, direct `v.redd.it` MP4, Reddit post/share links, `redd.it/<id>`, and `old.reddit.com` fallback.
- [x] Detect login walls, blocked pages, and no-media pages with structured skip reasons.
- [ ] Expand structured skip coverage for deleted/removed/quarantined pages when real examples or fixtures are available.
- [x] Do not implement Reddit auth fallback in the current phase; unresolved login-wall posts should skip with `login_wall` or `external_source_hidden`.
- [x] Parse publicly visible Reddit metadata fields when available, such as `url_overridden_by_dest`, `secure_media`, `media_embed`, `preview`, `reddit_video`, and static gallery metadata.
- [x] If publicly visible Reddit metadata points to an external URL, delegate that URL back into the resolver chain instead of writing one-off domain logic inside Reddit.
- [x] Keep Redgifs as the priority provider adapter because the live test proved Reddit rich-video posts commonly delegate there.
- [x] Let unknown external providers fall back to the Generic Resolver.
- [x] Support static Reddit image galleries when public HTML exposes direct `i.redd.it` candidates.
- [x] Keep DASH/HLS muxing and multi-file `v.redd.it` support deferred until the multi-candidate contract is tested.
- [x] Do not add Reddit posting, commenting, voting, save/unsave, moderation, or chat-management features.

### 19F. Redgifs Foundation

Goal: make Redgifs a stable no-auth provider adapter so direct Redgifs links can be downloaded now, and future `reddit link -> Redgifs link` discovery can reuse the same downstream path.

- [x] Add a dedicated Redgifs resolver for public `redgifs.com/watch/<id>` and known Redgifs host variants.
- [x] Normalize Redgifs URLs to a canonical watch URL and stable remote id.
- [x] Extract direct MP4 candidates from bounded public Redgifs watch-page HTML.
- [x] Prefer clear video candidates such as `media.redgifs.com/<Id>.mp4` or `media.redgifs.com/<Id>-silent.mp4` over preview images and decorative assets.
- [x] Record `audio_status` as `unknown`, `silent`, or `not_detected` without promising muxed audio.
- [x] Validate direct Redgifs media with the same redirect, MIME, size, and URL safety checks used by Generic Resolver and `download.http`.
- [x] Map resolved items to `origin_source: "redgifs"`, `media_type: "video"`, file key `v0`, and storage path `library/redgifs/video/<yyyy>/<mm>/...`.
- [x] Preserve upstream provenance when Redgifs is reached from another resolver, such as Telegram inbox or future Reddit delegation.
- [x] Return structured skips for unavailable videos, region blocks, login/age gates, JavaScript-only pages, ambiguous multi-media pages, unsupported MIME, and oversized media.
- [x] Do not use Redgifs API credentials or third-party API access in this phase.
- [x] Do not scrape creator profiles, searches, feeds, related videos, comments, or account data.
- [x] Live-test with direct and Reddit-delegated Redgifs links from Telegram inbox; five Redgifs watch links resolved and downloaded MP4 files under the phase19 live-test library.

### 19G. Post-19 Connected Provider Adapters

- [ ] Keep Imgur single-media support but migrate it into the same provider-adapter pattern.
- [ ] Plan Pixiv artwork-link resolution separately from Pixiv bookmark sync.
- [ ] Plan X post-link resolution separately from X bookmark APIs and assume login wall / anti-bot failures may remain normal skip states.
- [ ] Keep Instagram deferred until the generic, Redgifs, and Reddit resolver contracts are stable.

### 19H. Deferred Auth Fallback Policy

- [ ] Do not implement Reddit app-only auth in the current phase.
- [ ] Keep Reddit user OAuth and script password grant as later optional local-only fallbacks, not as the primary project direction.
- [ ] If Reddit API approval becomes available later, revisit optional metadata-only fallback for explicit Reddit links.
- [ ] Any future Reddit auth fallback must read only metadata for user-provided explicit links and must not read saved items, feeds, subreddits, comments, votes, or account history.
- [ ] Any future Reddit Data API use must use a registered OAuth token, a unique descriptive `REDDIT_USER_AGENT`, and rate-limit backoff from `X-Ratelimit-Used`, `X-Ratelimit-Remaining`, and `X-Ratelimit-Reset`.
- [ ] Respect Reddit's current free Data API guidance of 100 QPM per OAuth client id, averaged over a 10-minute window, unless the official policy changes.
- [ ] Do not attempt to bypass Reddit API limits, login walls, deleted content, removed content, or access controls.
- [ ] If Reddit metadata is stored via API fallback, add a retention/deletion strategy for deleted Reddit user content before promoting the feature.

References:

- Reddit Data API Wiki: <https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki>
- Reddit Data API Terms: <https://redditinc.com/policies/data-api-terms>

### 19I. Promotion And Compatibility

- [x] Decide stable public tool names for queue intake and sync orchestration: `link.queue.upsert` and `link.media.sync`.
- [x] Keep `telegram.inbox.sync_links` as a wrapper so existing live-test commands do not break.
- [x] Update examples for `link.queue.upsert` and `link.media.sync`.
- [x] Update `TOOL_CATALOG.md`, `RUNBOOK.md`, `ARCHITECTURE.md`, and localized handoff files for the stable core link tools.
- [x] Keep normal tool listing conservative; stable link tools are public, while experimental Telegram inbox and preview helpers still require explicit opt-in flags.
- [x] Document exit codes, JSON result shape, dry-run behavior, queue behavior, and structured skip reasons for promoted link tools.

### 19J. Verification And Post-19 Test Follow-Ups

- [x] Unit-test URL normalization, canonicalization, and normalized URL uniqueness.
- [x] Unit-test initial link queue lifecycle metadata, retryable vs permanent skips, source provenance merge, and batch limits.
- [x] Unit-test active retry scheduling and concurrent claim behavior after claim/lease execution is implemented.
- [x] Unit-test alias/canonical/media-item dedupe across distinct Reddit links and provider/direct media identities.
- [x] Unit-test that credential-bearing headers are not persisted to SQLite through link resolution state.
- [ ] Extend secret persistence tests to metadata sidecars, logs, snapshots, signed runtime download data, and `download_context_ref` once runtime-only download contexts exist.
- [x] Unit-test SSRF protections: unsafe schemes, userinfo, localhost/private IPs, unresolved hosts, redirect limits, and redirect-to-private-target.
- [x] Unit-test direct media resolution for images, GIF, MP4, WebM, MOV, and audio MIME types.
- [x] Unit-test generic HTML candidate parsing, thumbnail rejection, ambiguous candidate skip, and no-JS behavior.
- [x] Unit-test Redgifs URL normalization, watch-page extraction, direct MP4 candidate selection, preview rejection, unavailable video skip, and live-test fixture parsing.
- [x] Unit-test Reddit external URL delegation to Redgifs after that delegation is implemented.
- [x] Unit-test Reddit anonymous fallback, login-wall detection, static gallery resolution, and structured skips.
- [x] Unit-test multi-candidate planning fixtures for partial success, required-file failure, and `metadata.files` mapping for static file groups.
- [ ] Unit-test Reddit rate-limit metadata parsing and backoff behavior before any Reddit API fallback is promoted.
- [x] Unit-test `link.media.sync` dry-run no writes and rerun dedupe.
- [x] Live-test only with explicit user-provided URLs and project-local output paths under `${MEDIAGENT_DATA_DIR}`.

## Side Decisions And Post-19 Guidance

These items guide future work and should not be treated as unfinished Phase 19 implementation.

- [ ] Treat auth-assisted account collection as optional legacy/advanced behavior; Pixiv bookmark sync remains the only current exception.
- [ ] Prioritize explicit-link resolvers over saved/bookmark/feed collectors for Reddit, X, Instagram, and future platforms.
- [ ] Defer Reddit auth fallback until the no-auth Generic Resolver, Redgifs foundation, and Reddit anonymous resolver are stable.
- [ ] X live OAuth verification remains pending because API access may require paid credits.
- [ ] Plan X and Pixiv explicit-link resolvers after Phase 19 core link tools, so inbox automation can download from explicit post/artwork links without relying on bookmark/feed access.
- [ ] Discuss whether Pixiv needs additional source tools beyond bookmarks, such as following-user works or explicit artwork IDs.
- [ ] Promote Telegram inbox link behavior only as one URL input source after core link tools exist.
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
- `link_queue.normalized_url` is the first intake uniqueness key. Resolver canonical aliases and final media identity must prevent duplicate downloads when different URLs point to the same content.
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
explicit URL source or collector
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
