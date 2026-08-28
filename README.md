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

## Media operations

```bash
uv run mediagent add 'https://example.com/media-or-post'
uv run mediagent sync SOURCE
uv run mediagent status
uv run mediagent status SOURCE
```

Commands read `.env` from the current directory; existing environment variables
take precedence. Use `--dry-run` to preview supported operations and `--json`
for complete machine-readable output.

```bash
uv run mediagent library remove --path /absolute/path/to/file --reason 'not wanted'
uv run mediagent library restore --removal-id rmv_operation_id
uv run mediagent library rename --path /absolute/path/to/file --name 'new name'
uv run mediagent library deduplicate --dry-run
uv run mediagent library trash status
```

Remove moves content below `.trash/mediagent/` and records the operation in
SQLite. Trash is retained indefinitely. Run `uv run mediagent --help` for the
complete command reference.
