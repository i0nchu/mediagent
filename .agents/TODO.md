# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Current Focus: Instagram Saved Media Foundation

Goal: add deterministic Instagram saved-media collection and synchronization tools, following the proven Pixiv bookmark architecture while respecting Instagram saved-session and private-API constraints.

The normal source workflow is:

`saved feed -> normalize posts/resources -> upsert items -> dedupe/status filter -> plan storage -> download -> record files and item state`

Saved-media logic belongs in the Instagram platform and tool layers. It must reuse the shared downloader, storage planner, database state, repair behavior, and session boundary instead of adding a second download pipeline.

### 1. Platform Client And Normalization

- [ ] Add a bounded Instagram saved-feed client operation that reads one page at a time from the configured saved session.
- [ ] Return page items plus the opaque next-page cursor without exposing cookies, authorization headers, signed media URLs, or raw session settings.
- [ ] Normalize photos, Reels/videos, and carousel posts into the existing media item/file model.
- [ ] Treat one saved post as one media item and include every downloadable carousel resource as a file candidate.
- [ ] Preserve stable source identity, shortcode/media ID, author, source timestamp, canonical post URL, caption-safe metadata, resource index, and media type.
- [ ] Keep runtime download URLs and credential-bearing request context in memory only.
- [ ] Map login expiry, checkpoint/challenge, rate limit, private/unavailable media, and temporary request failures to existing structured Instagram error codes.

### 2. `instagram.saved.collect`

- [ ] Add a stable deterministic collector for saved Instagram posts.
- [ ] Support bounded `limit` and `max_pages` inputs for operator tests and controlled runs.
- [ ] Support pagination until feed exhaustion when an explicit full collection is requested without an arbitrary item limit.
- [ ] Expose collection summaries including pages fetched, raw posts, normalized items, resource counts, next cursor, and stop reason.
- [ ] Do not download files or mutate media item/file state.
- [ ] Dry-run must validate configuration and describe the request without calling Instagram or writing state.
- [ ] Use the existing saved-session boundary and return actionable auth errors without automatically performing an unbounded login loop.

### 3. `instagram.saved.sync`

- [ ] Add a stable sync tool that composes collection with the existing DB, storage, download, and status helpers.
- [ ] Support `full_sync`, `stop_on_known`, `limit`, `max_pages`, `store_cursor`, `retry_failed`, `repair_missing_files`, and `write_sidecar_metadata` with Pixiv-compatible semantics where practical.
- [ ] For recurring sync, scan from the newest saved posts and stop after reaching an already known terminal item; do not rely on an old pagination cursor as the only source of truth.
- [ ] For explicit full sync, continue until feed exhaustion and let tool-layer item/file dedupe skip healthy completed media.
- [ ] Store durable cursor/source state only after a successful, untruncated boundary; partial or failed runs must not advance it.
- [ ] Reuse scanner-friendly storage under `<library_root>/instagram/<media_type>/<yyyy>/<mm>/...`.
- [ ] Preserve complete-post behavior: all resources from a carousel are downloaded before the parent item is marked downloaded.
- [ ] Record partial and failed file/item states so later `retry_failed` and `repair_missing_files` runs can recover them.
- [ ] Return concise summaries for collected, known, queued, downloaded, partial, failed, repaired, skipped, files, and bytes.

### 4. Agent And CLI Integration

- [ ] Register both tools in the default tool registry and expose machine-readable inspect schemas.
- [ ] Add stable JSON examples for bounded collect, recurring sync, and explicit full sync.
- [ ] Add an English `instagram_saved_sync` SKILL that lets Agent Core distinguish saved-media sync from explicit-link download.
- [ ] Ensure natural-language requests for "all saved Instagram media" do not gain an invented `limit` or `max_pages`.
- [ ] Keep explicit post/Reel URL requests routed through the existing Instagram link-download SKILL.

### 5. Safety And Rate Limits

- [ ] Use conservative sequential page requests and no concurrent Instagram feed crawling in V1.
- [ ] Stop the current run on rate limit, checkpoint/challenge, or invalid session instead of tight retries.
- [ ] Never persist account passwords, session cookies, signed CDN query parameters, or raw private-API payloads.
- [ ] Keep default tests fully offline with fake clients and minimized fixtures containing no private saved content or identifiable account data.

## Automated Verification

- [ ] Unit tests cover an empty saved feed, one photo, one Reel/video, one multi-resource carousel, and pagination.
- [ ] Collector tests cover bounded limits, feed exhaustion, dry-run no-network behavior, and structured auth/rate-limit failures.
- [ ] Sync tests cover first download, second-run dedupe, stop-on-known recurring sync, full sync, partial carousel failure, retry, missing-file repair, safe storage paths, and cursor non-advancement on failure/truncation.
- [ ] Agent tests cover bounded requests, recurring update requests, and unbounded "all saved media" requests.
- [ ] `uv run --locked python -m unittest discover -s tests` passes.
- [ ] `uv lock --check` passes.
- [ ] `git diff --check` passes.

## Local Live-Test Gate

- [ ] Use only `/home/ion/projects/mediagent` configuration, DB, temporary library, and saved Instagram session. Never access `/data/services` or `/data/nas` during development verification.
- [ ] Check the saved session once and collect only one bounded page without logging private URLs or account details.
- [ ] Sync a small bounded number of saved posts into a dedicated local live-test library.
- [ ] Confirm a carousel downloads every resource and a Reel/video produces a valid file when those shapes are present in the bounded sample.
- [ ] Run the same bounded sync again and confirm healthy files are deduplicated with zero duplicate downloads.
- [ ] Run `library.file.verify` against the dedicated live-test scope.
- [ ] Remove local live-test media, DB, and temporary output after recording a redacted summary.
- [ ] Merge the feature branch into `main` only after automated verification and this bounded live test pass.

## After This Focus

- Finish the systemd deployment MVP environment-check profile.
- Add a run lock or lease guard for overlapping timer runs.
- Add summary-only Agent Core output for systemd journal usage.
- Make Pixiv `stop_on_known` source-aware.
- Add the documented timer-safe auth, rate-limit, and cursor failure policy.

## Deferred To V2 Or Later

- Long-running daemon process.
- Built-in or agentic scheduler.
- RuleSpec generation.
- Visual workflow editor.
- Long-term memory and multi-turn conversation state.
- Workspace-scoped command execution and broad library-management workflows.
- X explicit post-link support while tweet reads require paid credits.
