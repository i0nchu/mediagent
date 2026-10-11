# Mediagent agent guidance

Read and follow `AGENT.md`, `README.md`, and `TODO.md` before changing code.
`AGENT.md` is the authoritative development contract; do not modify it without
the project owner's explicit approval. Use focused `codex/*` branches and pull
requests targeting `main`.

## Code Review Rules

- Preserve SQLite authority and source provenance when content deduplicates.
  Flag changes that lose source references, revive purged content, advance
  cursors after incomplete work, or complete jobs with stale leases. The safe
  path is an idempotent transition with failure and migration coverage.
- Keep credentials and runtime download context out of persisted metadata,
  logs, public tool results, and LLM inputs. Validate external URLs, redirects,
  and filesystem boundaries before fetching or writing; retain bounded retries
  and recoverable, explicit lifecycle operations.
- Keep public CLI/tool contracts compatible and provider-specific logic in its
  adapter. Flag changes that silently replace a configured source or introduce
  a deployment/service dependency into shared collection flows. Experiments
  require approval before production integration.

Report actionable regressions with a concrete trigger, impact, and affected
line. Mechanical lint findings belong in CI. Review findings do not authorize
merging or deploying a pull request.
