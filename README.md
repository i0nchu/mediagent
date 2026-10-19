# Mediagent

Mediagent is a Python 3.12+ command-line media collector. It resolves, downloads,
packages, deduplicates, and records media without providing a browsing UI or a
prescribed deployment model.

## Install

```bash
git clone https://github.com/i0nchu/mediagent.git
cd mediagent
uv sync --locked
cp .env.example .env
```

Inputs that require segmented-video extraction can be run with
`uv run --locked --extra video mediagent add URL`; some stream combinations
also require the system `ffmpeg` executable.

Pornhub exact-video downloads default to an 8 GiB limit. Set
`MEDIAGENT_PORNHUB_MAX_MEDIA_BYTES=17179869184` in `.env` for 16 GiB
(accepted range: 1 MiB–64 GiB, expressed as integer bytes). This applies to
`add`, `link sync` and Telegram external-link delivery. An explicit
`link sync URL --max-media-bytes BYTES` takes precedence; process environment
variables override `.env`. Size checks also bound staging usage. The setting
does not select a lower quality automatically.

Known size-limit failures report `pornhub_media_too_large` with the actual/limit
bytes and stop automatic link retries. After changing the limit, inspect
`mediagent link backlog --json`, run `mediagent link retry ID`, then run the
next Telegram sync. Standalone links can use `mediagent add URL` again.
Previously exhausted jobs also require explicit retry; changing `.env` alone
does not reopen them. Ensure the service reads the intended `.env` before
its next invocation; existing environment settings retain precedence.

Set the local paths in `.env`:

```dotenv
MEDIAGENT_DATA_DIR=/absolute/path/to/data
MEDIAGENT_LIBRARY_DIR=/absolute/path/to/library
MEDIAGENT_DB_PATH=/absolute/path/to/data/mediagent.sqlite3
```

```bash
uv run mediagent init
uv run mediagent status
```

Agent Core defaults to Ollama. To use an OpenAI-compatible local service such
as llama.cpp, set `MEDIAGENT_LLM_PROVIDER=openai_compatible` and configure
`MEDIAGENT_OPENAI_BASE_URL`, `MEDIAGENT_OPENAI_MODEL`, and the optional API key,
timeout, and maximum output tokens shown in `.env.example`.

## Media operations

```bash
uv run mediagent add 'https://example.com/media-or-post'
uv run mediagent add /path/to/media-file
uv run mediagent add /path/to/media-directory
uv run mediagent sync SOURCE
uv run mediagent sync jmcomic --no-repair --max-chapters 2 --time-budget 120 --audit-chapters 0 --summary-json
uv run mediagent sync jmcomic --repair --max-chapters 10 --time-budget 300 --summary-json
uv run mediagent sync jmcomic --revalidate-all --max-chapters 10 --time-budget 300 --summary-json
uv run mediagent sync telegram --link-batch-size 5 --link-time-budget 60 --summary-json
uv run mediagent link backlog --json
uv run mediagent link retry 88
uv run mediagent link retry 88 --retry-auth
uv run mediagent status
uv run mediagent status SOURCE
uv run mediagent remove ASSET_ID
uv run mediagent restore ASSET_ID
uv run mediagent tag ASSET_ID favorite landscape
uv run mediagent tag ASSET_ID
uv run mediagent tag
uv run mediagent untag ASSET_ID landscape
uv run mediagent search favorite
uv run mediagent trash purge --dry-run
uv run mediagent trash purge
```

All CLI subcommands, including `library`, `tools`, and `experimental`, read
`.env` from the current directory; existing environment variables take
precedence. Set `MEDIAGENT_ENV_FILE` to use another file (relative paths are
resolved from the current directory), or set it to an empty string to disable
file loading. Use `--dry-run` to preview supported operations and `--json` for
complete machine-readable output.

JMComic Favorites observes each album's complete chapter listing, using
`latest_photo_id` as a hint rather than proof of completion. Healthy tracked
chapters reuse SQLite metadata and local CBZs without requesting chapter pages.
New chapters take priority; failed chapters, local repair and periodic historical
audits remain explicit work. Changes to chapter numbering/titles update SQLite
and ComicInfo while keeping existing archive paths and user-renamed filenames;
new archives use the current canonical layout. Each invocation processes at most 20
chapters with a 300-second cooperative budget. The budget is checked between
albums, chapters and pages; one network request or archive operation can overrun
it. Paused work and downloaded pages resume on later invocations.

`--no-repair` skips known failures and damaged/missing existing chapters while
still downloading new chapters and resuming budget-paused work. Use `--repair`
to retry due failures and repair tracked files. Historical auditing selects at
most two chapters per album after its 24-hour interval, so a 100-chapter album
can take 50 audit rounds to cover; `--audit-chapters 0` disables it. Repeat
`--revalidate-all` (also `--full`) to resume a bounded full
metadata/checksum pass; healthy bytes are reused, and a new pass begins only
after the previous one completes. Full revalidation and `--overwrite` require
repair mode. `--overwrite` deliberately replaces selected files on every
invocation and may repeat earlier chapters when bounded; use revalidation for
a resumable historical check. Schema 16 adds album/chapter checkpoints without
assuming old Favorites observations mean downloaded media, moving files or
reviving removed/purged content. Stop all writers and back up SQLite before upgrading; validate a copy first.
Rollback requires matching code/database backups; no schema downgrade is provided.

Local files are copied into the managed library. Directories are scanned
recursively without following symbolic links; original files are left unchanged.
For one imported or downloaded item, the normal output includes its Asset ID;
use `--json` to inspect every Asset ID returned by a batch operation.

Remove moves every active representation of an Asset below `.trash/mediagent/`
and records the operation in SQLite. Restore returns recoverable representations.
Purge permanently
deletes removed content older than `MEDIAGENT_TRASH_RETENTION_DAYS` while
retaining its identity tombstone so the same content is not kept again. A new
source without a remote checksum may still transfer bytes before identification.
Asset tags are a simple list shared by manual and automated tagging.
Search matches tags, descriptive metadata, source identity, and filenames; it
lists active Assets by default and accepts `--all` when removed Assets are needed.
Set `MEDIAGENT_AUTO_TAG=true` to generate metadata-only tags after `add` with
the configured LLM. `tag ASSET_ID` runs automatic tagging again; bare `tag`
processes one ready batch of pending jobs. LLM failures never change a
successful download into a failure.
Run `uv run mediagent --help` for the complete command reference.

## Contributing

Follow `AGENT.md`. Develop on a focused branch and open a pull request against
`main`. Run `uv sync --locked --group dev`, `uv run --locked ruff check .`,
`uv run --locked python -m unittest discover -s tests`, `uv lock --check`, and
`git diff --check` before review.

The PR quality workflow checks lint, the lockfile, and changed-line whitespace,
then runs offline tests on Python 3.12 with base dependencies and on Python
3.12/3.13 with the optional video backend. It also builds the distributions.
No production credentials or live accounts are needed.

Codex follows the review rules in `AGENTS.md`. Enable automatic review for this
repository in [Codex Code Review settings](https://app.chatgpt.com/settings/code-review)
after connecting its GitHub repository; this account setting cannot be enabled
by committing a workflow. To request an individual review, comment
`@codex review` on the PR. AI reviews complement the required CI checks and
human approval; they do not merge or deploy changes.
