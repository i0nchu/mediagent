# Mediagent Runbook

## Environment

Use Python 3.12+.

Preferred command runner:

```bash
uv run --locked ...
```

## Local Comic Live Tests

Use only repository-local development paths for the first live run:

```bash
export MEDIAGENT_DATA_DIR=/home/ion/projects/mediagent/tmp/live/comics
export MEDIAGENT_LIBRARY_DIR=/home/ion/projects/mediagent/tmp/live/comics/library
export MEDIAGENT_DB_PATH=/home/ion/projects/mediagent/tmp/live/comics/mediagent.sqlite3
export MEDIAGENT_NHENTAI_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/nhentai_session.json
export MEDIAGENT_JMCOMIC_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/jmcomic_session.json
mkdir -p "$MEDIAGENT_DATA_DIR/credentials" "$MEDIAGENT_LIBRARY_DIR"
uv run --locked mediagent tools run core.db.init --json
```

Do not `source .env` while it contains legacy names beginning with a digit. Rename them to `MEDIAGENT_JMCOMIC_USERNAME` and `MEDIAGENT_JMCOMIC_PASSWORD` first.

nhentai direct links work anonymously when the provider allows it:

```bash
uv run --locked mediagent tools run comic.link.sync \
  --input examples/tools/comic.link.sync.nhentai.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync \
  --input examples/tools/comic.link.sync.nhentai.json --json
```

For favorites, export a logged-in browser cookie jar once. Mediagent accepts either its private JSON session format or a Netscape-format `cookies.txt`; point `MEDIAGENT_NHENTAI_COOKIE_FILE` at the latter. Refresh preserves the configured format and enforces mode `0600`. Mediagent intentionally does not automate username/password plus CAPTCHA/proof-of-work login.

Tool results have two distinct layers. The top-level `status` says whether the requested tool operation succeeded. For an `auth.status` inspection, top-level success means the inspection completed; it does not mean login succeeded. Read `data.auth_status`, `data.authenticated`, `data.reusable`, and `data.remote_verified`. A locally loadable session that was not checked against the provider reports `authenticated: null` and `remote_verified: false`.

An imported browser session may receive HTTP 403 from nhentai's refresh endpoint even while authenticated favorites access still works. `nhentai.auth.refresh` performs a read-only favorites check after that response for diagnostics, but the refresh operation still returns top-level failure with `error.code: nhentai_refresh_rejected`. Its data states that rotation failed while current authentication remains usable. If the check also fails, it returns `nhentai_auth_required` and the browser cookie must be exported again.

```bash
uv run --locked mediagent tools run nhentai.auth.status --input examples/tools/nhentai.auth.status.json --json
uv run --locked mediagent tools run nhentai.auth.refresh --input examples/tools/nhentai.auth.refresh.json --json
uv run --locked mediagent tools run nhentai.favorites.collect --input examples/tools/nhentai.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run nhentai.favorites.sync --input examples/tools/nhentai.favorites.sync.json --dry-run --json
uv run --locked mediagent tools run nhentai.favorites.sync --input examples/tools/nhentai.favorites.sync.json --json
```

JMComic can create and reuse a session directly from configured credentials; browser cookies are not required. `JMCOMIC_USERNAME`, `JMCOMIC_PASSWORD`, and `JMCOMIC_SESSION_FILE` are accepted compatibility names; the `MEDIAGENT_JMCOMIC_*` names remain preferred and take precedence. `jmcomic.auth.login` deliberately replaces an invalid old session instead of failing before the login request. Test a small album before favorites:

```bash
uv run --locked mediagent tools run jmcomic.auth.status --input examples/tools/jmcomic.auth.status.json --json
uv run --locked mediagent tools run jmcomic.auth.login --input examples/tools/jmcomic.auth.login.json --json
uv run --locked mediagent tools run comic.link.sync \
  --input examples/tools/comic.link.sync.jmcomic-album.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync \
  --input examples/tools/comic.link.sync.jmcomic-album.json --json
uv run --locked mediagent tools run jmcomic.favorites.collect \
  --input examples/tools/jmcomic.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run jmcomic.favorites.sync \
  --input examples/tools/jmcomic.favorites.sync.json --dry-run --json
```

As an optional alternative, a Netscape-format browser export may be configured through `MEDIAGENT_JMCOMIC_COOKIE_FILE` or `JMCOMIC_COOKIE_FILE`; pointing `*_SESSION_FILE` directly at a `.txt` or `.cookies` path also works. Only cookies belonging to trusted JMComic domains are imported. A later session write preserves Netscape format and mode `0600`. When both cookie-file and session-file variables are set, the cookie-file variable takes precedence.

Inspect only summaries and files under the local root. A second identical run should download zero healthy pages and report existing CBZ files. Direct JM album runs never create follow memberships; only `jmcomic.favorites.sync` does. Do not delete SQLite `-wal` or `-shm` files during a run.

Follow is implemented by periodically rerunning `jmcomic.favorites.sync`; it is not a resident daemon. The complete favorite snapshot updates active membership, and each active album is resolved again so new chapters are discovered. System-level examples live under `deploy/systemd/system/`; the `/data/services/mediagent` deployment units run as account `server`, set its `HOME`/`PATH`, use one shared non-blocking run lock, and emit compact `--summary-json` journal output. `nhentai.favorites.sync` may use the same timer pattern to discover newly favorited exact galleries, but it does not infer or follow a series.

If JMComic pages downloaded before the filename-hash descramble fix show horizontally reordered bands, `repair_missing_files` is not sufficient because those files still exist and are recorded as healthy. Explicitly redownload and rebuild that exact album with `mediagent link sync '<album-url>' --overwrite --json`. The operation uses `.partial` and atomic replacement; verify the resulting images and CBZ locally before any server deployment.

Telegram inbox and future custom inboxes do not need provider-specific comic commands. Supported nhentai/JMComic links pass through the shared `link.media.sync` intake and are automatically dispatched to the exact comic adapter before generic HTML resolution. Sending a direct comic link through an inbox therefore downloads/packages that linked work only; it never enables series follow. Inspect `summary.comic_links_considered` plus the CBZ counters to confirm dispatch.

Fallback during local development:

```bash
PYTHONPATH=src python3 -m mediagent ...
```

## Run Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Check Lockfile

```bash
uv lock --check
```

If it fails after packaging changes, run:

```bash
uv lock
```

## CLI Smoke Checks

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect core.env.check --json
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

## Agent Core V1 Smoke Checks

Agent Core V1 uses Ollama by default:

```bash
export MEDIAGENT_LLM_PROVIDER=ollama
export MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434
export MEDIAGENT_OLLAMA_MODEL=qwen3:8b
```

Inspect built-in SKILLs:

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
```

Preview an explicit-link task:

```bash
uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --dry-run --json
```

Unsupported tasks should fail before any tool call:

```bash
uv run --locked mediagent agent run "我目前有存在的 telegram inbox 嗎？" --dry-run --json
```

Expected shape: `status:"failure"`, `error.code:"unsupported_task"`, `skill:null`, and no tool steps.

LLM transport failures should be structured, not Python tracebacks:

```bash
MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:9 MEDIAGENT_OLLAMA_TIMEOUT_SECONDS=0.2 \
  uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --json
```

Expected shape: `status:"failure"` with `error.code:"llm_request_failed"`.

## Initialize a Temporary Database

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run core.db.init --json
```

## Preview Cleanup / Recovery

Always preview cleanup before applying it. Planning mode does not mutate files or SQLite:

```bash
uv run --locked mediagent tools run core.cleanup.media_state \
  --input examples/tools/core.cleanup.media_state.json --json
```

Apply mode requires `confirm: true` and quarantines files before resetting matching media state:

```bash
printf '{"mode":"apply","platform":"pixiv","status":"downloaded","confirm":true}' \
  | uv run --locked mediagent tools run core.cleanup.media_state --input - --json
```

## Dry-Run a Filesystem Operation

```bash
printf '{"path":"${MEDIAGENT_DATA_DIR}/pixiv","kind":"directory","create":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-data uv run --locked mediagent tools run core.path.prepare --input - --dry-run --json
```

## Real Download Smoke Test

Only write to `/tmp` for smoke tests.

```bash
printf '{"url":"https://example.com/","target_path":"/tmp/mediagent-download-test/example.html","expected_mime_prefix":"text/html","overwrite":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-download-test uv run --locked mediagent tools run download.http --input - --json
```

Clean up:

```bash
rm -rf /tmp/mediagent-download-test /tmp/mediagent.sqlite3
```

## X OAuth Setup Shape

Do not commit OAuth credentials. For local testing, keep credential files under `MEDIAGENT_DATA_DIR`:

```bash
export MEDIAGENT_DATA_DIR=/tmp/mediagent-data
export X_CREDENTIALS_FILE="$MEDIAGENT_DATA_DIR/credentials/x-oauth.json"
```

Or load the local `.env` file:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

Generate an authorization URL:

```bash
uv run --locked mediagent tools run x.auth.start --json
```

After the browser callback provides a code, create an input JSON based on `examples/tools/x.auth.exchange.json`, then run:

```bash
uv run --locked mediagent tools run x.auth.exchange --input examples/tools/x.auth.exchange.json --json
```

Check the configured session:

```bash
uv run --locked mediagent tools run x.auth.status --input examples/tools/x.auth.status.json --json
```

Collect bookmarks:

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run x.bookmarks.collect --input examples/tools/x.bookmarks.collect.json --json
```

## Pixiv Local Login And Live Test Shape

Pixiv V1 supports an explicit local OAuth/PKCE helper. It does not scrape browser profiles, store passwords, or require the user to manually locate a refresh token.

Load `.env`:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

Generate a Pixiv login URL and PKCE verifier:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --json > /tmp/pixiv-login-start.json
```

Open the returned `data.authorization_url` in a browser. After Pixiv login finishes, copy the full callback URL, or copy only the `code` query parameter from it. The callback URL shape is:

```text
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?...&code=...
```

Create an exchange input by copying `examples/tools/pixiv.auth.login.exchange.json`, replacing:

- `callback_url` with the full callback URL, or use `code` with only the short-lived callback code.
- `code_verifier` with `data.code_verifier` from `/tmp/pixiv-login-start.json`.
- `credential_output_path` with a path under `MEDIAGENT_DATA_DIR`.

Exchange the callback URL/code and write the local credential file:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input /tmp/pixiv-login-exchange.json --json
```

The credential file should be:

```text
$MEDIAGENT_DATA_DIR/credentials/pixiv-oauth.json
```

If you already have a refresh token, the older refresh-token path still works. Put the refresh token in `.env`:

```bash
PIXIV_REFRESH_TOKEN=...
PIXIV_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/pixiv-oauth.json
```

Create or update the local credential file:

```bash
uv run --locked mediagent tools run pixiv.auth.refresh --input examples/tools/pixiv.auth.refresh.json --json
```

Check the configured session:

```bash
uv run --locked mediagent tools run pixiv.auth.status --input examples/tools/pixiv.auth.status.json --json
```

Resolve one explicit Pixiv artwork URL without downloading:

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851"}' \
  | uv run --locked mediagent tools run pixiv.link.resolve --input - --json
```

Download one explicit Pixiv artwork URL through the shared link-first pipeline:

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851","write_sidecar_metadata":false}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

This path treats one artwork URL as the whole artwork, resolves all original pages by default, dedupes against `pixiv.bookmarks.sync`, and applies the required Pixiv `Referer` during download without persisting runtime headers. If credentials are missing or expired, `pixiv.link.resolve` returns a structured auth error with a recommended Pixiv auth tool instead of starting browser login by itself.

Collect bookmarked works:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.collect --input examples/tools/pixiv.bookmarks.collect.json --json
```

The collector returns normalized media items; it does not download files by itself. For normal bookmark downloading, run the deterministic sync helper:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --json
```

Preview the planned downloads without writing files or DB rows:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --dry-run --json
```

By default, the example writes downloaded files under the scanner-friendly library root:

```text
$MEDIAGENT_LIBRARY_DIR/<platform>/<storage_category>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
```

Library root resolution order:

1. Explicit tool input: `library_root` or legacy `target_dir`.
2. Platform-specific environment variable: `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`, such as `MEDIAGENT_PIXIV_LIBRARY_DIR`.
3. Global environment variable: `MEDIAGENT_LIBRARY_DIR`.
4. Fallback: `${MEDIAGENT_DATA_DIR}/library`.

To keep Pixiv in its own top-level directory, set:

```bash
MEDIAGENT_PIXIV_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/pixiv
```

Because this root is already Pixiv-specific, it uses the media/date layout below that root instead of adding `pixiv/pixiv`.

Operator note: `MEDIAGENT_LIBRARY_DIR` changes only affect future target planning. With the same SQLite DB, already terminal Pixiv items remain deduped and will not be repopulated into the new root. Treat the DB and library files as one state bundle when moving deployments.

Pixiv keeps file media type and work type separate. Manga source pages are still photo files, but official Pixiv `type:manga` works use `work_type:comic` and the `comic-pages` storage category. Multi-page `type:illust` works remain illustrations under `photo`. Packaged CBZ files use `comic`.

Plan the legacy DB/library update before applying it:

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.plan.json --json
```

Apply only after reviewing the summary and stopping overlapping Pixiv sync jobs:

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.apply.json --json
```

Apply mode atomically moves existing manga source files and adjacent JSON sidecars into `comic-pages`, updates SQLite paths/metadata, and quarantines known Pixiv placeholder downloads. It does not contact Pixiv. Files already moved into `.trash` are treated as missing and are never restored from trash.

After reconciliation, preview source-backed repair for recorded files that are missing from their library paths:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync \
  --input examples/tools/pixiv.bookmarks.sync.repair.json --dry-run --json
```

Run the same command without `--dry-run` only when missing library files should be downloaded again. `repair_missing_files` is opt-in: normal timer runs continue to respect DB `downloaded` state, while repair downloads a new copy to the planned path and leaves `.trash` untouched.

After reconciliation and any required repair, preview legacy manga packaging:

```bash
uv run --locked mediagent tools run pixiv.comics.package \
  --input examples/tools/pixiv.comics.package.json --dry-run --json
```

Remove `--dry-run` to create CBZ files. The committed example sets `migrate_legacy:true`: the tool reads only complete, healthy source pages, writes each Kavita V2 archive through a `.partial` file plus atomic replacement, records it in SQLite, and keeps source pages. After a V2 archive succeeds, an older V1 date-layout CBZ is moved to `library/.trash/mediagent-comic-v1` and its stale DB row is removed. Future bookmark syncs can set `package_comics:true` to package newly downloaded manga automatically.

Kavita V2 uses one directory per series. A Pixiv one-shot receives its own unique series identity; works with real Pixiv series metadata share one directory and use `Series`, `Number`, optional `Volume`, and optional `Count` from the normalized comic contract.

Pixiv image examples:

```text
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic/Work Title [pixiv-139193091]/Work Title [pixiv-139193091].cbz
```

Without `MEDIAGENT_PIXIV_LIBRARY_DIR`, the shared-root examples are:

```text
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic/Work Title [pixiv-139193091]/Work Title [pixiv-139193091].cbz
```

If Immich scans the Pixiv external library but comics should be handled by another reader, add both exclusion patterns in that external library's Scan Settings and rescan:

```text
**/comic/**
**/comic-pages/**
```

Point Kavita at `pixiv/comic`; do not point it at `pixiv` or `comic-pages`. Each immediate child of `comic` is one series directory and there are no archive files at the comic root. The `comic-pages` directory remains Mediagent's lossless source for repair or rebuilding CBZ files.

The SQLite database is read from `MEDIAGENT_DB_PATH`; each completed file is recorded in `media_files` with a library-relative path, storage layout version, checksum, size, MIME type, and file health. The parent item is marked `downloaded`, `partial`, or `failed` in `media_items`.

Public library paths do not receive JSON sidecar metadata by default. Source metadata stays in SQLite/internal records. Use `write_sidecar_metadata: true` only for explicit debugging.

Verify known library files without contacting Pixiv:

```bash
uv run --locked mediagent tools run library.file.verify --json
```

For manual one-file debugging, download selected Pixiv image URLs with `download.http` and a Pixiv referer header:

```bash
uv run --locked mediagent tools run download.http --input examples/tools/download.http.pixiv.json --json
```

Downloaded files go wherever the `download.http` input says. The examples place files under:

```text
$MEDIAGENT_DATA_DIR/pixiv/...
```

Metadata JSON can still be written manually with `metadata.write` when debugging, but it is not the default public-library metadata format. Until Workflow V1 exists, platforms without deterministic sync helpers still require manual CLI/tool composition or an external script.

## Telegram Local Session And Live Test Shape

Telegram V1 foundation is implemented and live-verified for the current phase. Real login/status, curated link-inbox collection, two small media downloads, one one-hour video download, scanner-friendly layout placement, `library.file.verify`, and second-run dedupe were verified on 2026-07-24 UTC. Real Telegram downloads stream directly to `.partial` files and are finalized only after validation and chunked checksum calculation.

It uses a user MTProto session through Telethon-compatible tooling. Do not use it for sending, forwarding, deleting, or chat management.

Add local-only values to `.env`:

```bash
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE_NUMBER=...
TELEGRAM_SESSION_FILE=${MEDIAGENT_DATA_DIR}/credentials/telegram.session
```

The API ID/hash come from the user's Telegram developer app. The session file is a credential. Keep it under `MEDIAGENT_DATA_DIR`, do not commit it, and do not copy it into the public media library.

Load `.env`:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

First-time Telegram login is a two-step local flow.

Request a login code:

```bash
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --json
```

Use the returned `phone_code_hash` with the code Telegram sends you:

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output"
}
```

Then run:

```bash
uv run --locked mediagent tools run telegram.auth.login --input /path/to/local-telegram-login-complete.json --json
```

If Telegram asks for a 2FA password, use `password_ref`. Inline 2FA passwords are rejected:

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output",
  "password_ref": {
    "source": "env",
    "name": "TELEGRAM_2FA_PASSWORD"
  }
}
```

Verify the configured session:

```bash
uv run --locked mediagent tools run telegram.auth.status --input examples/tools/telegram.auth.status.json --json
```

List selectable dialogs without downloading media:

```bash
uv run --locked mediagent tools run telegram.dialogs.list --input examples/tools/telegram.dialogs.list.json --json
```

Collect media-bearing messages from an explicit trusted source:

```bash
uv run --locked mediagent tools run telegram.messages.collect --input examples/tools/telegram.messages.collect.json --json
```

Preview a deterministic Telegram sync without writing files or DB rows:

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --dry-run --json
```

Run a bounded sync only after checking the source selector and limits:

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --json
```

For curated Telegram media, create a private collection channel and paste message links for media you want to download. Then set the channel selector in `examples/tools/telegram.messages.sync.link-inbox.json` and run:

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --dry-run --json
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --json
```

The configured user session must be able to access each linked original message. Mediagent does not send, forward, delete, or manage Telegram chats for this flow.

The latest small-media live run wrote:

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg
```

The long-video live run wrote:

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4
```

Re-running the same direct-link sync skipped the completed items.

Shared-root Telegram files use:

```text
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

To keep Telegram in its own top-level directory, set:

```bash
MEDIAGENT_TELEGRAM_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/telegram
```

Then files go under:

```text
$MEDIAGENT_DATA_DIR/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram cursors are stored per source and media-type scope, for example `messages:saved_messages:photo-video`. They advance only after successful durable sync processing.

## Instagram Saved Session And Link Test

Instagram support is explicit-link first. Use it only for user-provided public post, carousel, Reel, or tv URLs. The resolver treats one Instagram post URL as the whole post, so carousel links download all resources by default. The resolver never performs password login by itself.

Local setup uses `.env` values:

```bash
set -a
source .env
set +a
```

Check the saved session:

```bash
uv run --locked mediagent tools run instagram.auth.status --json
```

If the session is missing or invalid and credentials are present, explicitly repair it:

```bash
uv run --locked mediagent tools run instagram.auth.ensure_session --json
```

Inspect one link without downloading:

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/"}' \
  | uv run --locked mediagent tools run instagram.link.resolve --input - --json
```

Download one whole post through the shared link pipeline:

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

Downloaded files land under:

```text
$MEDIAGENT_DATA_DIR/library/instagram/photo/<yyyy>/<mm>/
$MEDIAGENT_DATA_DIR/library/instagram/video/<yyyy>/<mm>/
```

Signed Instagram CDN URLs are runtime-only. Verify they are not present in SQLite, sidecar metadata, logs, snapshots, or committed fixtures.

## Link-First Resolver Smoke Checks

The current primary path is explicit URL resolution, not account saved/bookmark collection. Use these checks when working on Phase 19 link-first tools.

List experimental link tools:

```bash
uv run --locked mediagent tools list --json --include-experimental
```

Inspect stable link tools:

```bash
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
```

Queue an explicit URL without downloading:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","ingest_platform":"cli"}' \
  | uv run --locked mediagent tools run link.queue.upsert --input - --json
```

Resolve and download an explicit URL through the core link pipeline:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

Use the public link entry point for the same workflow without writing tool JSON:

```bash
uv run --locked mediagent link sync 'https://example.com/path/to/media.jpg' --write-sidecar-metadata --json
```

Run queued links from cron or a daemon worker:

```bash
uv run --locked mediagent tools run link.media.sync --json
```

Queued runs claim ready links with a short lease, skip links leased by other workers, and include retryable `deferred` links only after `next_attempt_at`. Permanent skips such as login walls, unsafe URLs, unsupported media, deleted/removed content, and access controls are not retried.

Inspect the current preview resolver:

```bash
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
```

Preview an explicit URL without downloading:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","record":false}' \
  | uv run --locked mediagent tools run link.resolve.preview --input - --json --allow-experimental
```

Expected behavior:

- direct public image/video/audio URLs resolve before full HTML fetches
- public single-media HTML may resolve when exactly one clear candidate exists
- Reddit static galleries may resolve as multiple photo candidates; complex galleries, login-required, JavaScript-required, blocked, unsafe, or ambiguous pages return structured skip reasons
- download steps must repeat URL safety, redirect, MIME, and byte-limit checks instead of trusting preview output
- output files, if a sync/download command is used, must stay under `${MEDIAGENT_DATA_DIR}`

Redgifs direct/watch links are implemented as a no-auth provider foundation. Direct `redgifs.com/watch/<id>` links should resolve to `origin_source: "redgifs"`, `media_type: "video"`, file key `v0`, and scanner-friendly storage under `library/redgifs/video/<yyyy>/<mm>/...` when public HTML exposes a direct MP4 candidate.

Reddit explicit links currently use anonymous/bounded behavior. If a Reddit page hides external media behind a login wall or dynamic client data, the resolver should skip with `login_wall` or `external_source_hidden`. Do not use Reddit saved collection as the next product path unless the user explicitly reopens auth-assisted collection.

## Deferred Reddit OAuth And Saved Collection

Reddit V1 auth/saved tooling exists with fake-client coverage, but it is deferred legacy/advanced capability. It must not post, comment, vote, save/unsave, moderate, chat, scan subreddits, scrape HTML pages, or run third-party extractors.

Use this section only when explicitly validating the legacy auth-assisted path.

Add local-only values to `.env`:

```bash
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=http://127.0.0.1:8765/reddit/callback
REDDIT_USER_AGENT='linux:mediagent:0.1 (by /u/your_username)'
REDDIT_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/reddit-oauth.json
```

Load `.env`:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

Generate an authorization URL:

```bash
uv run --locked mediagent tools run reddit.auth.start --input examples/tools/reddit.auth.start.json --json
```

Open the returned `data.authorization_url` in a browser. After the redirect, copy the `code` query parameter into a local exchange input based on `examples/tools/reddit.auth.exchange.json`, then run:

```bash
uv run --locked mediagent tools run reddit.auth.exchange --input /path/to/local-reddit-auth-exchange.json --json
```

Check the configured session:

```bash
uv run --locked mediagent tools run reddit.auth.status --input examples/tools/reddit.auth.status.json --json
```

Collect saved media candidates without downloading:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --json
```

Preview the collector without credentials, DB writes, or network:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
```

`reddit.saved.collect` returns normalized media items and optional cursor state only. Download orchestration is not the current direction; do not add `reddit.saved.sync` unless the user explicitly chooses to resume auth-assisted account collection.

## Common Troubleshooting

- `ModuleNotFoundError: mediagent`: use `uv run --locked ...` or set `PYTHONPATH=src`.
- exit code `2`: input, config, auth, permission, filesystem, or database validation problem.
- exit code `1`: runtime, network, or rate-limit failure.
- unsafe path error: set `MEDIAGENT_DATA_DIR` and write under that directory.
- X auth failure: check token expiration, required scopes, and whether `X_CREDENTIALS_FILE` is inside an allowed write root.
- Pixiv auth failure: check `PIXIV_CREDENTIALS_FILE`, token expiration, callback URL/code freshness, and whether the credential file is under `MEDIAGENT_DATA_DIR`. If using the older refresh-token path, also check `PIXIV_REFRESH_TOKEN`.
- Pixiv download returns 403: include `{"Referer":"https://www.pixiv.net/"}` in the `download.http` headers.
- Telegram auth failure: check `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_FILE`, and whether the session file is under `MEDIAGENT_DATA_DIR`.
- Reddit explicit link returns `login_wall` or `external_source_hidden`: this is an expected skip when public HTML does not expose the media URL. Prefer direct provider links such as Redgifs when available.
- Reddit auth failure in the deferred saved-collection tooling: check `REDDIT_CLIENT_ID`, `REDDIT_REDIRECT_URI`, `REDDIT_USER_AGENT`, `REDDIT_CREDENTIALS_FILE`, callback-code freshness, and whether the credential file is under `MEDIAGENT_DATA_DIR`.

## Safety Reminder

Do not run real platform login or media sync code until its tool is implemented with fixture tests and secret redaction checks. The current expansion path is explicit link resolution with no-auth behavior first. X and Reddit auth-assisted collection are deferred unless the user explicitly resumes them. Pixiv and Telegram have completed user-assisted live verification for their current deterministic sync slices. Future live runs still require user-provided credentials when a platform-specific login tool is used.
