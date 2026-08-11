# Mediagent Focused TODO

This file tracks upcoming implementation work only. Keep completed status, live-test history, and resolved issues in `STATE.md`, `ISSUES.md`, and `RUNBOOK.md`.

When updating this TODO, update the Traditional Chinese and Japanese copies in the same change:

- `.agents_zh_tw/TODO.md`
- `.agents_jp/TODO.md`

## Remaining Focus: Instagram Saved Media Live Verification

The offline foundation is implemented and recorded in `STATE.md`. Remaining work is limited to the operator-controlled live-test gate below.

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
