# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Current Focus: systemd Timer Hardening

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
