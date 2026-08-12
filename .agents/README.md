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
- Pixiv work-type classification, legacy-library reconciliation, and deterministic CBZ packaging for official manga
- Telegram user-session media source tools for explicit local login, auth status, dialog listing, message/link-inbox collection, Telegram-specific media download, and deterministic message sync
- Reddit OAuth config/auth tools and saved-media collection tools, retained as deferred legacy/advanced capability
- first-class link-first tools through `link.queue.upsert` and `link.media.sync`, plus resolver foundations for direct media, bounded single-media HTML, Imgur, Pixiv artwork links, anonymous Reddit explicit links/static galleries, and Redgifs direct/watch links
- Instagram explicit-link foundation tools for saved-session status/login/ensure-session and whole-post post/Reel resolution through `link.media.sync`
- Agent Core V1 local preview with English SKILL files, Ollama-backed strict JSON action generation, tool allowlist enforcement, unsupported-task handling, execute/dry-run boundaries, and destination path sanitization
- unit tests, CLI smoke tests, fake HTTP clients, and recorded fixture responses

Workflow execution and built-in scheduling are intentionally not implemented yet. Agent Core V1 exists as a local preview, not as a broad autonomous planner or scheduler. X auth/bookmark collection and Reddit auth/saved collection exist with fixture/fake HTTP coverage, but they are no longer the main expansion path. Pixiv auth, collection, deterministic bookmark sync, and the universal storage layout are implemented with fixture/fake HTTP coverage; Pixiv also has user-assisted live storage verification, including a bounded 100-item / 624-file `scanner-friendly-v2` layout run. Telegram foundation includes explicit login, curated link-inbox support, stream-safe real downloads, layout placement, rerun dedupe, and live verification for small media plus a one-hour video. Instagram explicit-link foundation is implemented and live-verified for user-provided public post, carousel, and Reel links with a saved local session.

The current product direction is link-first: users, cron jobs, workflows, Telegram inboxes, and future agents provide explicit URLs; Mediagent resolves safe downloadable media candidates and then reuses the existing storage/download pipeline. Auth-assisted account collection should be treated as optional legacy/advanced behavior unless the user explicitly reopens it. Pixiv bookmarks remain the useful exception because that flow is already working.

## Useful Commands

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.sync --json
uv run --locked mediagent tools inspect pixiv.library.reconcile --json
uv run --locked mediagent tools inspect pixiv.comics.package --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
uv run --locked mediagent agent run "download https://example.com/media.jpg" --dry-run --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Important Direction

Do not build Workflow V1, built-in scheduling, Reddit saved sync, or X live auth verification next unless the user explicitly changes direction. Agent Core V1 may be hardened, but it must remain SKILL-scoped and tool-registry based. The link-first baseline is now the primary product path: stable `link.queue.upsert`, stable `link.media.sync`, public `mediagent link sync <url>`, queue claim/retry scheduling, canonical/media identity dedupe, Reddit external-provider delegation, Redgifs downloads, Instagram whole-post downloads, and simple multi-candidate partial-success handling. Keep resolver behavior bounded by default; future platform work should extend explicit-link provider adapters before account/bookmark collectors.
