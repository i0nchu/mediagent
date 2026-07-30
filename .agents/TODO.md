# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Current Focus: Phase 21 Pixiv Explicit Link Resolver

Goal: make user-provided Pixiv artwork URLs work through the same link-first pipeline as Instagram, Reddit, Redgifs, and generic public links.

Primary flow:

```text
Pixiv artwork URL or artwork id
-> Pixiv URL/id normalization
-> existing Pixiv auth/session handling
-> Pixiv artwork detail fetch
-> normalized media candidates
-> link.media.sync
-> scanner-friendly storage
```

This phase is about explicit links, not bookmark/feed discovery. Existing Pixiv bookmark sync remains supported, but new work should reuse the shared resolver/download/storage pipeline wherever possible.

## Product Scope

- [ ] Support `https://www.pixiv.net/artworks/<illust_id>`.
- [ ] Support localized artwork paths such as `https://www.pixiv.net/en/artworks/<illust_id>`.
- [ ] Support direct `illust_id` input in the Pixiv-specific tool.
- [ ] Treat one artwork URL as the whole artwork.
- [ ] For multi-page illustration/manga works, resolve every original page by default.
- [ ] Preserve page selection hints, if present, only as metadata unless an explicit future option changes behavior.
- [ ] Reuse existing Pixiv item identity: `platform = "pixiv"` and `remote_id = <illust_id>`.
- [ ] Make explicit-link downloads dedupe against items/files previously downloaded by `pixiv.bookmarks.sync`.

## Non-Goals

- [ ] Do not add Pixiv feed, following-user, ranking, search, tag, recommendation, or user-profile collection in this phase.
- [ ] Do not add media browsing, gallery UI, reposting, commenting, bookmarking, liking, or account mutation.
- [ ] Do not convert ugoira frames into video in this phase.
- [ ] Do not implement broad Pixiv HTML scraping if the App API detail path is available.
- [ ] Do not start Workflow V1, built-in scheduling, RuleSpec, or Agent Core work from this phase.

## 21A. Resolver Contract

- [ ] Add a Pixiv resolver named `pixiv_artwork_link` to the core link resolver registry.
- [ ] Normalize accepted URLs to `https://www.pixiv.net/artworks/<illust_id>`.
- [ ] Emit aliases for equivalent localized Pixiv artwork URLs.
- [ ] Return `LinkResolution` with `origin_source: "pixiv"`, canonical URL, remote id, resolver name, source timestamp, author metadata, and media candidates.
- [ ] Convert every resolved artwork file into a `MediaCandidate` with stable `file_index`, `part`, `media_type`, `mime_type`, `extension`, `content_identity`, and source timestamp.
- [ ] Keep one Pixiv artwork as one media item with multiple file candidates when the artwork has multiple pages.
- [ ] Preserve title, caption, tags, author id/name, create date, dimensions, Pixiv type, page count, `x_restrict`, visibility, and bookmark/view counts when available.
- [ ] Return structured skips for unsupported URLs, missing artwork id, private/deleted/unavailable artwork, rate limits, auth failure, unsupported media type, and resolver failure.

## 21B. Pixiv API And Auth Boundary

- [ ] Add `pixiv_client.get_illust_detail` using Pixiv App API artwork detail behavior.
- [ ] Reuse existing Pixiv credential loading and refresh behavior from `pixiv.auth.status` / `pixiv.auth.refresh`.
- [ ] `pixiv_artwork_link` may use an already configured Pixiv session, but must not start browser login by itself.
- [ ] Missing or invalid credentials should return agent-decidable errors that recommend `pixiv.auth.login` or `pixiv.auth.refresh` when appropriate.
- [ ] Keep credential files inside allowed write roots.
- [ ] Redact access tokens, refresh tokens, authorization codes, client secrets, and raw upstream auth payloads from outputs, logs, metadata, and tests.
- [ ] Respect Pixiv rate-limit or temporary block responses with structured rate-limit errors and no tight retry loops.

## 21C. Public Tool Surface

- [ ] Add `pixiv.link.resolve` for inspecting one Pixiv artwork URL or id without downloading.
- [ ] Register the Pixiv resolver so `link.media.sync` can download Pixiv artwork URLs directly.
- [ ] Keep `pixiv.link.resolve` platform-bound: non-Pixiv hosts must not resolve through this tool.
- [ ] Add `examples/tools/pixiv.link.resolve.json`.
- [ ] Update `TOOL_CATALOG.md` and `RUNBOOK.md` in all three languages with the Pixiv explicit-link commands.
- [ ] Keep the CLI path simple: users should be able to run `mediagent link sync <pixiv artwork url>` after credentials are configured.

## 21D. Download And Storage Behavior

- [ ] Use the existing `link.media.sync` orchestration for upsert, dedupe, path planning, download, sidecar metadata, file records, and item status.
- [ ] Ensure Pixiv image downloads use the required safe Pixiv `Referer` behavior.
- [ ] Do not persist credential-bearing headers or raw tokens.
- [ ] Store files under the current scanner-friendly layout.
- [ ] Under the shared root, paths should look like `library/pixiv/photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>`.
- [ ] Under `MEDIAGENT_PIXIV_LIBRARY_DIR`, paths should omit the duplicate platform layer and look like `photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>`.
- [ ] Use existing media/file status rules: reruns skip downloaded files, failed items can be retried only when requested, and partial multi-page downloads mark the item `partial`.

## 21E. Ugoira Policy

- [ ] Reuse existing ugoira metadata parsing if it is available through the detail flow.
- [ ] Represent first-version ugoira output as the source zip candidate only, matching the current Pixiv bookmark-sync capability.
- [ ] Mark ugoira metadata clearly so later tooling can convert or index it.
- [ ] If detail-based ugoira resolution cannot be implemented safely in this phase, return `unsupported_media_type` rather than inventing partial conversion behavior.

## 21F. Tests

- [ ] Unit-test Pixiv artwork URL/id parsing and canonicalization.
- [ ] Unit-test localized URL alias handling.
- [ ] Unit-test `pixiv_client.get_illust_detail` request shape with fake HTTP.
- [ ] Unit-test single-page artwork resolution.
- [ ] Unit-test multi-page artwork resolution and candidate ordering.
- [ ] Unit-test ugoira zip candidate or structured skip behavior.
- [ ] Unit-test auth missing, auth refresh failure, rate limit, deleted/private artwork, and unsupported URL errors.
- [ ] Unit-test `pixiv.link.resolve` platform boundary and secret redaction.
- [ ] Unit-test `link.media.sync` with a Pixiv artwork URL, including Pixiv `Referer`, dedupe against existing Pixiv bookmark records, sidecar metadata, and scanner-friendly layout.
- [ ] Unit-test dry-run behavior proving no DB or file writes.

## 21G. Verification

- [ ] Run the full default test suite with `uv run --locked python -m unittest discover -s tests`.
- [ ] Run `uv lock --check`.
- [ ] Inspect `pixiv.link.resolve` through CLI JSON.
- [ ] Dry-run one Pixiv artwork URL through `pixiv.link.resolve`.
- [ ] Dry-run one Pixiv artwork URL through `link.media.sync`.
- [ ] Defer live bulk verification until this platform can be checked together with the other long-running platform checks.

## Later Candidates

These are not part of Phase 21:

- [ ] X explicit post-link feasibility.
- [ ] Instagram session-status TTL and long-running cron verification.
- [ ] Telegram inbox promotion from experimental wrapper to documented URL input source.
- [ ] Reddit/Redgifs follow-up only if new explicit-link examples require it.
- [ ] Workflow V1 after link-first provider adapters remain stable through repeated runs.
- [ ] Agent Core / SKILL integration after deterministic tools and workflow boundaries are stable.
