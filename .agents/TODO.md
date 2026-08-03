# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Current Focus: Remaining Missing-File Policy Decision

Goal: decide how to handle the 6 historical Reddit file records that still point to missing local files after the bounded repair run.

The explicit repair path is implemented and live-tested. It repaired the resolvable missing files. The remaining records are not normal downloader failures; their source URLs now hit a Reddit login wall and resolve as `requires_auth:login_required`.

## Decision Tasks

- Decide whether to leave the 6 remaining Reddit rows as known historical missing records.
- Decide whether to reset or quarantine those records with `core.cleanup.media_state`.
- Decide whether Reddit login-wall repair is worth new resolver/auth work, or should stay deferred with Reddit OAuth/saved collection.
- Do not run broad repair against the full live DB without a fresh dry-run and explicit user approval.

## Acceptance Notes

- Current live verification should stay at 669 valid files and 6 missing files unless the user chooses cleanup or new Reddit auth/resolver work.
- No agent should treat the remaining 6 rows as newly discovered downloadable media without first resolving the Reddit login-wall limitation.
- The repair feature itself is considered complete; future work is product policy or provider capability, not the original DB-state bug.

## Deferred Candidates

- X explicit post-link feasibility.
- Instagram session-status TTL and long-running cron verification.
- Telegram inbox promotion from experimental wrapper to documented URL input source.
- Reddit/Redgifs follow-up only if new explicit-link examples require it.
- Workflow V1 after link-first provider adapters remain stable through repeated runs.
- Agent Core / SKILL integration after deterministic tools and workflow boundaries are stable.
