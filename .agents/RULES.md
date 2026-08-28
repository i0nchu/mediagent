# Mediagent Development Rules

Mediagent is a background media downloading daemon. It only collects and downloads media. It does not manage media libraries, browse media, share content, repost content, or provide a gallery UI.

These rules are intentionally lightweight but enforceable for the current early-stage Python project. They may be expanded later as platform support, migrations, CI, and deployment workflows mature.

## 1. Development Workflow

Every change must follow this loop:

1. Understand the requirement: confirm the user scenario, platform scope, and what is intentionally out of scope.
2. Check the impact area: read the relevant files and identify affected core flows, platform modules, database behavior, and configuration.
3. Implement in small steps: keep each change focused on one clear purpose.
4. Add tests: core flows and error paths need tests; platform integration tests must not be required for the default test run.
5. Verify manually: confirm the affected CLI command or core function behaves as expected locally.
6. Clean up the change: remove temporary output, avoid committing credentials or downloaded media, and record the verification method.

## 2. Readability Control

- Names must describe intent. Avoid excessive abbreviations and platform-specific shorthand that only one author understands.
- Keep functions small. A function should handle one level of work.
- Avoid hidden side effects. Functions that write files, write to the database, call the network, or read credentials must make that behavior clear through naming or placement.
- Keep core flows separate from platform details. Shared pipelines must not depend on Pixiv, X, Instagram, or other platform-specific API details.
- Comments should explain non-obvious reasons, constraints, or external service behavior. Do not restate what the code already says.
- Error messages must help locate the problem, but must not include tokens, cookies, sessions, or full credential values.
- Add an abstraction only when it reduces duplication, isolates platform differences, or prevents external service details from leaking into core flows.

## 3. Architecture Rules

Mediagent is organized around discovering media, deduplicating records, downloading files, writing files to disk, and recording state. Platforms provide source and download details; they do not control the daemon flow.

- `src/mediagent/core/` contains shared models, tool contracts, scheduling, pipelines, filesystem placement, retry policy, and platform registration.
- `src/mediagent/tools/` contains agent-callable tools such as collectors, filters, downloaders, metadata writers, and database utilities.
- `src/mediagent/platforms/<platform>/` contains platform APIs, authentication, parsing, and platform-specific conversion logic.
- `src/mediagent/core/db.py` or later database modules own state persistence, deduplication, download records, and error records.
- `src/mediagent/cli.py` owns the command-line entry point. It must not contain platform business logic.
- `main.py` is only a startup entry point. It must not contain the main application flow.

Platform modules must respect these boundaries:

- Do not control the daemon loop directly.
- Do not access another platform's data directly.
- Do not decide the global data directory by themselves.
- Do not bypass core database deduplication or state recording.
- Do not write platform credentials into metadata, logs, or error messages.

External catalog, viewer, and cleanup services are CLI consumers, not Mediagent
platforms. Their API clients, selection policy, credentials, pagination,
schedulers, and service units must live in the owning external project. This
repository may document the provider-neutral CLI contract they call, but must
not carry or deploy their implementation. A compatibility output format such
as `ComicInfo.xml` is a file contract and does not authorize runtime coupling to
the reader that consumes it.

The media model must reserve at least these media types:

- `photo`
- `video`
- `audio`

Even if the first implementation only supports images or videos, the data model must not be hard-coded to one media type.

## 4. Testing Rules

- Core flows, database deduplication, filename generation, and retry behavior must have unit tests.
- Tests should prioritize stable logic. Platform network behavior should be isolated with fixtures, fake clients, or recorded data.
- The default test run must not depend on real accounts, real cookies, real tokens, or external network access.
- Tests that need network access, credentials, or external services must be explicitly marked and skipped by default.
- Each platform should cover at least: empty result, new item, duplicate item, download failure, and partial file failure.
- When fixing a bug that can be reproduced by a test, add the test first or explain why automated coverage is not practical.
- Test data must not contain private bookmarks, real cookies, tokens, user IDs, or personally identifiable information.

## 5. Permission Boundaries

Mediagent must be conservative with file and network behavior. It must not damage existing user data by default.

- Only write to `MEDIAGENT_DATA_DIR`, the configured database path, the configured log path, and necessary cache locations.
- Do not delete existing user media files unless the user runs an explicit command with a clear confirmation flow.
- Do not upload, share, repost, or provide media browsing and management features.
- Do not modify browser data, system keychains, or platform account settings.
- Read credentials only from environment variables or explicit configuration files.
- Tokens, cookies, sessions, and refresh tokens must not be written to logs, metadata, test snapshots, or error reports.
- Download paths must be normalized to prevent platform data from causing path traversal or writes outside the target directory.
- If an external service returns rate limits, blocks, or login errors, record the state and stop that platform's current run instead of retrying without limit.

## 6. Version Control

- Each commit must focus on one purpose, such as "create core models", "add Pixiv collector", or "fix database deduplication".
- Do not commit `.venv/`, local databases, downloaded media, cookies, tokens, sessions, logs, or temporary output.
- Pull request descriptions or commit messages must describe behavior changes and verification methods.
- If a database schema changes, include a migration, an initialization compatibility strategy, or a clear note that the project is still pre-release and the schema can be rebuilt.
- Do not mix pure formatting with behavior changes unless the formatting is small and directly related.
- Do not overwrite or revert changes from someone else unless explicitly asked. Understand conflicts before resolving them.
- Any change involving credentials, downloaded data, or platform login behavior must be checked for sensitive information before version control.

Recommended ignore entries:

```gitignore
.venv/
*.sqlite
*.sqlite3
*.db
*.log
downloads/
media/
cookies*.txt
*.token
*.session
```

## 7. Pre-change Checklist

Before delivering or committing a change, confirm:

- The change matches the project purpose: collect and download only; do not manage or browse media.
- Core flow, platform logic, and download logic are not mixed into one layer.
- New file-writing behavior only targets allowed data, database, log, or cache locations.
- No tokens, cookies, sessions, personal data, or real downloaded media are included in code or tests.
- Re-running the flow will not download already completed items again.
- Failures are recorded and a single platform error does not crash the whole daemon.
- Reasonable tests were added, or the reason automated coverage is not practical is documented.
- The affected command or flow was manually verified.
