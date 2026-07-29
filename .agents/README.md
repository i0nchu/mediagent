# Mediagent Agent Onboarding

This directory is the default English handoff package for humans and AI agents.

Localized handoff packages live in:

- `.agents_zh_tw/`
- `.agents_jp/`

## Read Order

1. `STATE.md` to understand the current project state and next recommended work.
2. `ARCHITECTURE.md` to understand the package layout and data flow.
3. `TOOL_CATALOG.md` to see what tools already exist.
4. `RUNBOOK.md` to run tests and smoke checks safely.
5. `ISSUES.md` to see current caveats.
6. `TODO.md` to choose the next implementation task.
7. `RULES.md` before editing files.

## Current Shape

Mediagent is currently an agentic-ready tool foundation, not a full workflow agent.

The implemented slice supports:

- a real Python package at `src/mediagent/`
- a CLI bridge through `mediagent tools ...`
- a tool contract and registry
- bottom tools for env checks, DB init, path preparation, run records, sync cursors, media item state, media file state, HTTP downloads, and metadata writing
- credential/auth primitives with redacted session status
- X OAuth PKCE, token exchange/refresh/status, and bookmark collection tools
- Pixiv local OAuth/PKCE setup, explicit refresh-token auth, token refresh/status, and bookmark collection tools
- Telegram user-session media source tools for explicit local login, auth status, dialog listing, message/link-inbox collection, Telegram-specific media download, and deterministic message sync
- Reddit OAuth config/auth tools and saved-media collection tools
- unit tests, CLI smoke tests, fake HTTP clients, and recorded fixture responses

Workflow execution, built-in scheduling, Instagram support, and LLM agent behavior are intentionally not implemented yet. X and Reddit are implemented with fixture/fake HTTP coverage but are not live-verified. Pixiv auth, collection, deterministic bookmark sync, and the universal storage layout are implemented with fixture/fake HTTP coverage; Pixiv also has user-assisted live storage verification, including a bounded 100-item / 624-file `scanner-friendly-v2` layout run. Telegram foundation includes explicit login, curated link-inbox support, stream-safe real downloads, layout placement, rerun dedupe, and live verification for small media plus a one-hour video.

## Useful Commands

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.sync --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect reddit.saved.collect --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Important Direction

Do not build Agent Core or Workflow V1 next unless the user explicitly changes direction. The next likely priorities are Reddit live verification with user-provided credentials, `reddit.saved.sync`, additional Pixiv source discussion, or X live verification if credentials/API access are available.
