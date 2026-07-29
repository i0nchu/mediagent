# Mediagent Runbook

## 環境

Python 3.12+ を使います。

推奨コマンド:

```bash
uv run --locked ...
```

ローカル開発時の fallback:

```bash
PYTHONPATH=src python3 -m mediagent ...
```

## テスト実行

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## lockfile 確認

```bash
uv lock --check
```

## CLI Smoke Checks

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect core.env.check --json
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

## 一時 database の初期化

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run core.db.init --json
```

## Cleanup / Recovery の preview

Cleanup を apply する前に必ず preview します。Planning mode は files や SQLite を変更しません。

```bash
uv run --locked mediagent tools run core.cleanup.media_state \
  --input examples/tools/core.cleanup.media_state.json --json
```

Apply mode は `confirm: true` を要求し、matching media state を reset する前に files を quarantine します。

```bash
printf '{"mode":"apply","platform":"pixiv","status":"downloaded","confirm":true}' \
  | uv run --locked mediagent tools run core.cleanup.media_state --input - --json
```

## filesystem operation の dry-run

```bash
printf '{"path":"${MEDIAGENT_DATA_DIR}/pixiv","kind":"directory","create":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-data uv run --locked mediagent tools run core.path.prepare --input - --dry-run --json
```

## Real download smoke test

Smoke tests は `/tmp` にだけ書きます。

```bash
printf '{"url":"https://example.com/","target_path":"/tmp/mediagent-download-test/example.html","expected_mime_prefix":"text/html","overwrite":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-download-test uv run --locked mediagent tools run download.http --input - --json
```

Cleanup:

```bash
rm -rf /tmp/mediagent-download-test /tmp/mediagent.sqlite3
```

## X OAuth setup shape

OAuth credentials を commit してはいけません。local testing では credential file を `MEDIAGENT_DATA_DIR` 配下に置きます。

```bash
export MEDIAGENT_DATA_DIR=/tmp/mediagent-data
export X_CREDENTIALS_FILE="$MEDIAGENT_DATA_DIR/credentials/x-oauth.json"
```

または local `.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

authorization URL を生成:

```bash
uv run --locked mediagent tools run x.auth.start --json
```

browser callback で code を取得したら、`examples/tools/x.auth.exchange.json` を元に input を作成して実行します。

```bash
uv run --locked mediagent tools run x.auth.exchange --input examples/tools/x.auth.exchange.json --json
```

session を確認:

```bash
uv run --locked mediagent tools run x.auth.status --input examples/tools/x.auth.status.json --json
```

bookmarks を収集:

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run x.bookmarks.collect --input examples/tools/x.bookmarks.collect.json --json
```

## Pixiv local login and live test

Pixiv V1 は明示的な local OAuth/PKCE helper に対応しています。browser profile を取得せず、password を保存せず、ユーザーが refresh token を手動で探す必要もありません。

`.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

Pixiv login URL と PKCE verifier を生成します:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --json > /tmp/pixiv-login-start.json
```

返された `data.authorization_url` を browser で開きます。Pixiv login 完了後、callback URL 全体を copy するか、`code` query parameter だけを copy します。callback URL の形は次の通りです:

```text
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?...&code=...
```

`examples/tools/pixiv.auth.login.exchange.json` を copy して exchange input を作り、次を置き換えます:

- `callback_url`: callback URL 全体。あるいは `code` を使い、短命 callback code だけを入れます。
- `code_verifier`: `/tmp/pixiv-login-start.json` の `data.code_verifier`。
- `credential_output_path`: `MEDIAGENT_DATA_DIR` 配下の path。

callback URL/code を交換し、local credential file を書きます:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input /tmp/pixiv-login-exchange.json --json
```

credential file の想定位置:

```text
$MEDIAGENT_DATA_DIR/credentials/pixiv-oauth.json
```

既に refresh token がある場合、従来の refresh-token path も使えます。`.env` に入れます。

```bash
PIXIV_REFRESH_TOKEN=...
PIXIV_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/pixiv-oauth.json
```

credential file を作成または更新:

```bash
uv run --locked mediagent tools run pixiv.auth.refresh --input examples/tools/pixiv.auth.refresh.json --json
```

session を確認:

```bash
uv run --locked mediagent tools run pixiv.auth.status --input examples/tools/pixiv.auth.status.json --json
```

bookmarked works を収集:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.collect --input examples/tools/pixiv.bookmarks.collect.json --json
```

collector は normalized media items を返すだけで、file は download しません。通常の bookmark download には deterministic sync helper を使います。

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --json
```

どの file を download するかだけ preview し、file/DB writes を避けるには:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --dry-run --json
```

Default example は downloaded files を scanner-friendly library root に置きます。

```text
$MEDIAGENT_LIBRARY_DIR/<platform>/<media_type>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
```

Library root の解決順:

1. 明示的な tool input: `library_root` または legacy `target_dir`。
2. Platform-specific environment variable: `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`。例: `MEDIAGENT_PIXIV_LIBRARY_DIR`。
3. Global environment variable: `MEDIAGENT_LIBRARY_DIR`。
4. Fallback: `${MEDIAGENT_DATA_DIR}/library`。

Pixiv を独自の top-level directory に置きたい場合は、次を設定します。

```bash
MEDIAGENT_PIXIV_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/pixiv
```

この root はすでに Pixiv-specific なので、その下では media/date layout を使い、`pixiv/pixiv` は追加しません。

Pixiv image examples:

```text
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
```

`MEDIAGENT_PIXIV_LIBRARY_DIR` が未設定の場合、shared-root examples は次の通りです。

```text
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
```

SQLite database は `MEDIAGENT_DB_PATH` で決まります。完了した file は `media_files` に記録され、library-relative path、storage layout version、checksum、size、MIME type、file health を保持します。Parent item は `media_items` で `downloaded`、`partial`、`failed` のいずれかに更新されます。

Public library paths には default で JSON sidecar metadata を書きません。Source metadata は SQLite/internal records に保持します。明示的な debugging の時だけ `write_sidecar_metadata: true` を使います。

Pixiv に接続せず、known library files を verify するには:

```bash
uv run --locked mediagent tools run library.file.verify --json
```

単一 file の manual debugging では `download.http` を使い、Pixiv referer header を付けます。

```bash
uv run --locked mediagent tools run download.http --input examples/tools/download.http.pixiv.json --json
```

download 後の file location は `download.http` input で決まります。example では次に置きます。

```text
$MEDIAGENT_DATA_DIR/pixiv/...
```

Debugging では `metadata.write` で JSON metadata を手動で書けますが、public library の default metadata format ではありません。Workflow V1 までは、deterministic sync helper がない platform は manual CLI/tool composition または external script が必要です。

## Telegram Local Session And Live Test Shape

Telegram V1 foundation は実装済みで、現在 phase の live verification は完了しています。2026-07-24 UTC に real login/status、curated link-inbox collection、2 件の小さな media downloads、1 件の 1 時間 video download、scanner-friendly layout placement、`library.file.verify`、second-run dedupe を確認しました。Real Telegram downloads は `.partial` files に直接 stream し、validation と chunked checksum calculation 後だけ finalization します。

Telethon-compatible user MTProto session を使います。送信、forward、削除、chat management には使わないでください。

`.env` に local-only values を追加します。

```bash
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE_NUMBER=...
TELEGRAM_SESSION_FILE=${MEDIAGENT_DATA_DIR}/credentials/telegram.session
```

API ID/hash は user の Telegram developer app から取得します。Session file は credential です。`MEDIAGENT_DATA_DIR` の下に置き、commit せず、public media library にコピーしないでください。

`.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

初回 Telegram login は二段階の local flow です。

Login code を要求します。

```bash
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --json
```

出力された `phone_code_hash` と Telegram から届いた code を使います。

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output"
}
```

次を実行します。

```bash
uv run --locked mediagent tools run telegram.auth.login --input /path/to/local-telegram-login-complete.json --json
```

Telegram が 2FA password を要求する場合は、`password_ref` を使います。Inline 2FA password は拒否されます。

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

Configured session を検証します。

```bash
uv run --locked mediagent tools run telegram.auth.status --input examples/tools/telegram.auth.status.json --json
```

Media を download せず selectable dialogs を列挙します。

```bash
uv run --locked mediagent tools run telegram.dialogs.list --input examples/tools/telegram.dialogs.list.json --json
```

明示的な trusted source から media-bearing messages を収集します。

```bash
uv run --locked mediagent tools run telegram.messages.collect --input examples/tools/telegram.messages.collect.json --json
```

Files や DB rows を書かず deterministic Telegram sync を preview します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --dry-run --json
```

Source selector と limits を確認してから bounded sync を実行します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --json
```

Curated Telegram media では、private collection channel を作り、download したい media message links をそこに貼ります。その後 `examples/tools/telegram.messages.sync.link-inbox.json` の channel selector を設定して実行します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --dry-run --json
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --json
```

Configured user session が各 linked original message に access できる必要があります。この flow では Mediagent は Telegram chats への送信、forward、削除、管理を行いません。

最新の small-media live run は次を書き込みました。

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg
```

Long-video live run は次を書き込みました。

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4
```

同じ direct-link sync を再実行すると、completed items は skipped になります。

Shared-root Telegram files は次の形になります。

```text
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram を独自の top-level directory に置きたい場合は次を設定します。

```bash
MEDIAGENT_TELEGRAM_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/telegram
```

その場合 files は次に置かれます。

```text
$MEDIAGENT_DATA_DIR/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram cursors は source と media-type scope ごとに保存されます。例: `messages:saved_messages:photo-video`。Durable sync processing が成功した後だけ進みます。

## Reddit OAuth and Saved Collection

Reddit V1 foundation は fake-client coverage がありますが、user が Reddit app credentials を提供するまで real live verification は skip します。First version は saved posts を curated source として扱います。Posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping、third-party extractors は実装しません。

`.env` に local-only values を追加します:

```bash
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=http://127.0.0.1:8765/reddit/callback
REDDIT_USER_AGENT='linux:mediagent:0.1 (by /u/your_username)'
REDDIT_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/reddit-oauth.json
```

`.env` を読み込みます:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

authorization URL を生成:

```bash
uv run --locked mediagent tools run reddit.auth.start --input examples/tools/reddit.auth.start.json --json
```

返された `data.authorization_url` を browser で開きます。Redirect 後、`code` query parameter を `examples/tools/reddit.auth.exchange.json` を元にした local exchange input に入れて実行します。

```bash
uv run --locked mediagent tools run reddit.auth.exchange --input /path/to/local-reddit-auth-exchange.json --json
```

configured session を確認:

```bash
uv run --locked mediagent tools run reddit.auth.status --input examples/tools/reddit.auth.status.json --json
```

Saved media candidates だけを collect し、download しません:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --json
```

Credentials、DB writes、network なしで collector preview:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
```

`reddit.saved.collect` は normalized media items と optional cursor state だけを返します。Download orchestration は `reddit.saved.sync` を明示的に追加するまで deferred です。

## よくある問題

- `ModuleNotFoundError: mediagent`: `uv run --locked ...` を使うか、`PYTHONPATH=src` を設定します。
- exit code `2`: input、config、auth、permission、filesystem、database validation の問題です。
- exit code `1`: runtime、network、rate-limit failure です。
- unsafe path error: `MEDIAGENT_DATA_DIR` を設定し、その配下に書き込みます。
- X auth failure: token expiration、required scopes、`X_CREDENTIALS_FILE` が allowed write root の中か確認します。
- Pixiv auth failure: `PIXIV_CREDENTIALS_FILE`、token expiration、callback URL/code freshness、credential file が `MEDIAGENT_DATA_DIR` 配下か確認します。従来の refresh-token path を使う場合は `PIXIV_REFRESH_TOKEN` も確認します。
- Pixiv download 403: `download.http` headers に `{"Referer":"https://www.pixiv.net/"}` を追加します。
- Telegram auth failure: `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE`、session file が `MEDIAGENT_DATA_DIR` の下か確認します。
- Reddit auth failure: `REDDIT_CLIENT_ID`、`REDDIT_REDIRECT_URI`、`REDDIT_USER_AGENT`、`REDDIT_CREDENTIALS_FILE`、callback code freshness、credential file が `MEDIAGENT_DATA_DIR` 配下か確認します。

## Safety Reminder

X と Reddit の live verification にはまだ user-provided credentials が必要です。Pixiv と Telegram は現在の deterministic sync slice について user-assisted live verification を完了しています。今後の live runs でも user-provided credentials は必要です。
