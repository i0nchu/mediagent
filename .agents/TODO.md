# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Recently Completed Gate: Clean-State Agent Full-Source Verification

Goal: prove Agent Core can interpret deployment-style natural-language tasks without downgrading "all" into arbitrary limits.

Completed before returning to timer hardening:

- [x] Rebuild the active SQLite DB and `mediagent-data/library` without preserving old live-test state.
- [x] Run `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"` in execute mode.
- [x] Confirm the selected tool uses `telegram.inbox.sync_links` with `full_sync:true` and no invented `limit` / `max_messages`.
- [x] Run `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"` in execute mode.
- [x] Confirm the selected tool uses `pixiv.bookmarks.sync` with `full_sync:true`, `stop_on_known:false`, and no invented `limit` / `max_pages`.
- [x] Run the same two tasks again and confirm tool-layer dedupe prevents duplicate downloads.
- [x] Verify downloaded file health with `library.file.verify`.

## Current Focus: Agent-Mode systemd Timer Deploy MVP

Goal: make Mediagent deployable as a conservative timer-driven background service that invokes Agent Core before building a long-running daemon.

Production timer entries should call `mediagent agent run "<task>"`, not direct deterministic tools. Deterministic tools remain the safe bottom layer for Agent Core, regression tests, debugging, and explicit operator verification.

The first agent-mode service target is Telegram inbox sync because it represents recurring content intake: scan the configured inbox, resolve new links, download supported media, store DB/file state, and continue from the stored cursor on the next run.

The second timer-safe source is Pixiv bookmark sync. Pixiv does not expose a simple "messages after cursor" model like Telegram, so the service path should scan from the newest bookmarks, stop when it reaches an already known terminal item, and use a bounded `max_pages` safety cap.

## P0 Gate: Telegram Inbox Message-Link Bridge

- [x] Route public `t.me/<channel>/<message_id>` and private `t.me/c/<chat>/<message_id>` inbox links through Telegram message sync while external URLs keep using the link resolver pipeline.
- [x] Preserve inbox chat/message/date/run provenance on Telegram-native media and return structured skips for protected or inaccessible linked messages.
- [x] Add `retry_auth_skipped` to `telegram.inbox.sync_links` and `link.media.sync` so old `requires_auth` / `login_wall` queue rows can be retried after sessions become usable.
- [x] Cover public, private, inaccessible, protected, mixed external-plus-Telegram, and auth-retry paths with fake-client tests.
- [ ] Run one bounded live inbox check with a public link, an accessible private link, an inaccessible link, and one restored downstream platform session; do not reset the production DB manually.

## Remaining Deployment MVP Tasks

- [ ] Add a deployment-oriented environment check profile for:
  - `MEDIAGENT_DATA_DIR`
  - `MEDIAGENT_DB_PATH`
  - `MEDIAGENT_LIBRARY_DIR`
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`
  - `TELEGRAM_SESSION_FILE`
  - `MEDIAGENT_TELEGRAM_INBOX_KEY`
  - one of `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`, `MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`, or `MEDIAGENT_TELEGRAM_INBOX_CHAT`
- [ ] Add a run-lock or lease guard so overlapping timer runs cannot process the same inbox concurrently.
- [ ] Add summary-only service output for `systemd` Agent Core runs. Current full JSON output is too large for journal because it includes full artifact lists and nested resolution payloads.
- [ ] Make Pixiv `stop_on_known` source-aware so explicit Pixiv links downloaded from another source do not prematurely stop bookmark sync during clean-state rebuilds.
- [ ] Add a timer-safe failure policy:
  - auth/session failures stop the current run
  - rate limits stop the current run without tight retry loops
  - partial downloads do not advance the Telegram cursor

## Acceptance Criteria

- [x] A clean checkout can be configured from `.env.example`.
- [ ] `core.env.check` or an equivalent CLI path can detect missing Telegram inbox deployment settings.
- [ ] A dry-run agent-mode timer command resolves the configured inbox without requiring the user to pass `chat` in the tool input.
- [x] An execute agent-mode timer command can download new inbox media and store `links:<inbox_key>` cursor state.
- [x] A second run starts after the stored cursor and does not re-download the same inbox links.
- [x] Pixiv bookmark timer runs scan newest bookmarks, stop on known terminal items, and do not re-download already downloaded artworks when `MEDIAGENT_LIBRARY_DIR` changes.
- [ ] Overlapping timer runs are prevented or fail cleanly before downloading.
- [x] The runbook explains where downloaded files are stored.

## Deferred To V2 Or Later

- Long-running daemon process.
- Built-in scheduler.
- Agentic scheduler.
- RuleSpec generation.
- Visual workflow editor.
- Long-term memory.
- Multi-turn conversation state.
- Broad autonomous planning beyond the selected SKILL.
- Workspace-scoped command execution.
- Library rebuild / management workflows.
- Long-running progress or structured streaming.
- X explicit post-link support, because X API tweet reads currently require paid credits.
