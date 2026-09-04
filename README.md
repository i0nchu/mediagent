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
uv run mediagent status
uv run mediagent status SOURCE
uv run mediagent remove ASSET_ID
uv run mediagent restore ASSET_ID
uv run mediagent trash purge --dry-run
uv run mediagent trash purge
```

Commands read `.env` from the current directory; existing environment variables
take precedence. Use `--dry-run` to preview supported operations and `--json`
for complete machine-readable output.

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
Run `uv run mediagent --help` for the complete command reference.
