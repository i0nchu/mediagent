# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Current Focus: systemd Timer Hardening

## Comic Source Follow-up

- [ ] Re-export a fresh nhentai browser cookie and repeat the complete favorites collect/sync live test; the previous user-verified cookie now returns HTTP 401.
- [ ] Live-test expired-cookie recovery and browser re-import; the provider rejected refresh with HTTP 403, so do not add password/CAPTCHA automation or assume automatic renewal.
- [x] Verify JM credential login/session reuse, three-page favorite pagination, a 42-album/1,081-chapter full dry-run, and one 108-page bounded real favorite sync with CBZ/dedupe checks.
- [x] Add system-level comic favorite timer examples with a shared run lock and summary-only journal output.
- [x] Recover a remotely expired JMComic session with one bounded credential login, checkpoint rotated cookies after collection/album resolution, and give initial full sync an 18-hour timeout.
- [x] Classify valid JMComic 1-12-pixel spacer strips as ignored non-content, exclude them from CBZ/page counts, and keep repair from retrying them.
- [x] Add remote-name/FID/URL multi-folder JMComic selection, local alias fallback, atomic union membership, selection-change follow semantics, and live verification of the 7-item named folder plus 49-item aggregate All view.
- [ ] Deploy the JMComic folder-selection feature with the server initially selecting the intended folder by numeric ID, then verify the service snapshot before relying on the recurring timer.

Goal: harden the existing Agent-mode timer deployment before adding another long-running source or scheduler layer.

- [ ] Add a deployment-oriented environment-check profile for the enabled Telegram inbox, Pixiv bookmark, and optional Instagram saved-media sources.
- [ ] Add a run lock or lease guard so overlapping timer runs fail cleanly before collection or download work begins.
- [ ] Add summary-only Agent Core output suitable for the systemd journal; omit full artifact and nested resolution payloads by default.
- [ ] Make Pixiv `stop_on_known` source-aware so an explicit Pixiv link downloaded through another source cannot stop bookmark sync prematurely.
- [ ] Apply one consistent timer-safe failure policy:
  - auth/session and checkpoint failures stop the current platform run
  - rate limits stop without tight retry loops
  - partial downloads do not advance durable source state
  - successful recurring runs remain deduplicated by DB/file state
- [ ] Add or update system-level deployment examples for hourly Telegram/Pixiv tasks and an optional conservative Instagram saved-media task.

## Acceptance Criteria

- [ ] A clean checkout can validate all enabled timer settings without contacting platforms.
- [ ] Two overlapping runs for the same source cannot download concurrently.
- [ ] Journal output contains one concise redacted final summary per run.
- [ ] Existing Telegram and Pixiv recurring commands continue from their intended source state without duplicate downloads.
- [ ] Instagram saved-media recurring sync uses `stop_on_known:true` with a bounded page cap and never invents an item limit.
- [ ] Auth, checkpoint, rate-limit, partial-download, and lock-contention paths have focused offline tests.
- [ ] `uv run --locked python -m unittest discover -s tests`, `uv lock --check`, and `git diff --check` pass.

## Deferred To V2 Or Later

- Long-running daemon process.
- Built-in or agentic scheduler.
- RuleSpec generation.
- Visual workflow editor.
- Long-term memory and multi-turn conversation state.
- Workspace-scoped command execution and broad library-management workflows.
- X explicit post-link support while tweet reads require paid credits.
- Additional authorized comic-source adapters using the normalized `metadata.comic` contract; keep provider access, page ordering, series identity, and policy boundaries adapter-specific.
