# Mediagent Runbook

## 環境

使用 Python 3.12+。

建議指令：

```bash
uv run --locked ...
```

本機開發 fallback：

```bash
PYTHONPATH=src python3 -m mediagent ...
```

## 跑測試

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 檢查 lockfile

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

## 初始化暫時資料庫

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run core.db.init --json
```

## 預覽 Cleanup / Recovery

套用 cleanup 前一定要先 preview。Planning mode 不修改檔案或 SQLite：

```bash
uv run --locked mediagent tools run core.cleanup.media_state \
  --input examples/tools/core.cleanup.media_state.json --json
```

Apply mode 需要 `confirm: true`，並會先 quarantine files，再 reset matching media state：

```bash
printf '{"mode":"apply","platform":"pixiv","status":"downloaded","confirm":true}' \
  | uv run --locked mediagent tools run core.cleanup.media_state --input - --json
```

## Dry-Run 檔案系統操作

```bash
printf '{"path":"${MEDIAGENT_DATA_DIR}/pixiv","kind":"directory","create":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-data uv run --locked mediagent tools run core.path.prepare --input - --dry-run --json
```

## 真實下載 Smoke Test

Smoke test 只寫入 `/tmp`。

```bash
printf '{"url":"https://example.com/","target_path":"/tmp/mediagent-download-test/example.html","expected_mime_prefix":"text/html","overwrite":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-download-test uv run --locked mediagent tools run download.http --input - --json
```

清理：

```bash
rm -rf /tmp/mediagent-download-test /tmp/mediagent.sqlite3
```

## X OAuth 設定形狀

不要提交 OAuth credentials。本機測試時，credential file 放在 `MEDIAGENT_DATA_DIR` 底下：

```bash
export MEDIAGENT_DATA_DIR=/tmp/mediagent-data
export X_CREDENTIALS_FILE="$MEDIAGENT_DATA_DIR/credentials/x-oauth.json"
```

或載入本機 `.env`：

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

產生 authorization URL：

```bash
uv run --locked mediagent tools run x.auth.start --json
```

browser callback 取得 code 後，依照 `examples/tools/x.auth.exchange.json` 建立 input，再執行：

```bash
uv run --locked mediagent tools run x.auth.exchange --input examples/tools/x.auth.exchange.json --json
```

檢查 session：

```bash
uv run --locked mediagent tools run x.auth.status --input examples/tools/x.auth.status.json --json
```

收集 bookmarks：

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run x.bookmarks.collect --input examples/tools/x.bookmarks.collect.json --json
```

## Pixiv 本機登入與 Live Test

Pixiv V1 支援明確的本機 OAuth/PKCE helper。不抓瀏覽器 profile、不保存密碼，也不要求使用者手動尋找 refresh token。

載入 `.env`：

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

產生 Pixiv login URL 與 PKCE verifier：

```bash
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --json > /tmp/pixiv-login-start.json
```

在瀏覽器打開回傳的 `data.authorization_url`。完成 Pixiv 登入後，複製完整 callback URL，或只複製其中的 `code` query parameter。callback URL 形狀如下：

```text
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?...&code=...
```

複製 `examples/tools/pixiv.auth.login.exchange.json` 作為 exchange input，替換：

- `callback_url`：完整 callback URL；或改用 `code`，只填短效 callback code。
- `code_verifier`：`/tmp/pixiv-login-start.json` 裡的 `data.code_verifier`。
- `credential_output_path`：`MEDIAGENT_DATA_DIR` 底下的路徑。

交換 callback URL/code 並寫入本機 credential file：

```bash
uv run --locked mediagent tools run pixiv.auth.login --input /tmp/pixiv-login-exchange.json --json
```

credential file 預期位置：

```text
$MEDIAGENT_DATA_DIR/credentials/pixiv-oauth.json
```

若已經有 refresh token，舊路徑仍可使用。在 `.env` 放入：

```bash
PIXIV_REFRESH_TOKEN=...
PIXIV_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/pixiv-oauth.json
```

建立或更新 credential file：

```bash
uv run --locked mediagent tools run pixiv.auth.refresh --input examples/tools/pixiv.auth.refresh.json --json
```

檢查 session：

```bash
uv run --locked mediagent tools run pixiv.auth.status --input examples/tools/pixiv.auth.status.json --json
```

收集收藏作品：

```bash
uv run --locked mediagent tools run pixiv.bookmarks.collect --input examples/tools/pixiv.bookmarks.collect.json --json
```

collector 只回傳 normalized media items，不會自己下載檔案。一般 bookmark 下載請使用 deterministic sync helper：

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --json
```

如果只想預覽將下載哪些檔案，且不寫檔、不寫 DB：

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --dry-run --json
```

預設範例會把下載檔放在 scanner-friendly library root：

```text
$MEDIAGENT_LIBRARY_DIR/<platform>/<media_type>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
```

Library root 解析順序：

1. 明確 tool input：`library_root` 或舊的 `target_dir`。
2. 平台專屬環境變數：`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`，例如 `MEDIAGENT_PIXIV_LIBRARY_DIR`。
3. 全域環境變數：`MEDIAGENT_LIBRARY_DIR`。
4. Fallback：`${MEDIAGENT_DATA_DIR}/library`。

如果想讓 Pixiv 使用自己的頂級目錄，設定：

```bash
MEDIAGENT_PIXIV_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/pixiv
```

因為這個 root 已經是 Pixiv 專屬，root 底下會使用 media/date layout，不會再多一層 `pixiv/pixiv`。

Pixiv 圖片範例：

```text
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
```

若沒有設定 `MEDIAGENT_PIXIV_LIBRARY_DIR`，shared-root 範例是：

```text
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
```

SQLite database 由 `MEDIAGENT_DB_PATH` 決定；每個完成檔案會記錄在 `media_files`，包含 library-relative path、storage layout version、checksum、size、MIME type 與 file health。Parent item 會在 `media_items` 標記為 `downloaded`、`partial` 或 `failed`。

Public library 預設不寫 JSON sidecar metadata。Source metadata 會留在 SQLite/internal records。只有明確除錯時才使用 `write_sidecar_metadata: true`。

不連線 Pixiv 驗證已知 library 檔案：

```bash
uv run --locked mediagent tools run library.file.verify --json
```

若要手動除錯單一檔案下載，使用 `download.http`，並帶 Pixiv referer header：

```bash
uv run --locked mediagent tools run download.http --input examples/tools/download.http.pixiv.json --json
```

下載後的檔案位置由 `download.http` input 決定。範例會放在：

```text
$MEDIAGENT_DATA_DIR/pixiv/...
```

除錯時仍可用 `metadata.write` 手動寫 JSON metadata，但這不是 public library 的預設 metadata 格式。Workflow V1 完成前，沒有 deterministic sync helper 的平台仍需要手動 CLI/tool composition 或外部 script。

## Telegram 本機 Session 與 Live Test 形式

Telegram V1 foundation 已實作，並已完成目前階段的 live verification。2026-07-24 UTC 已驗證真實 login/status、curated link-inbox collection、兩個小型 media downloads、一支一小時影片下載、scanner-friendly layout placement、`library.file.verify` 與第二次執行去重。真實 Telegram 下載會直接 stream 到 `.partial` 檔，並只在 validation 與分塊 checksum 完成後 finalization。

它使用 Telethon-compatible user MTProto session。不要用它來發訊息、轉傳、刪除或管理聊天。

在 `.env` 加入只屬於本機的值：

```bash
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE_NUMBER=...
TELEGRAM_SESSION_FILE=${MEDIAGENT_DATA_DIR}/credentials/telegram.session
```

API ID/hash 來自使用者的 Telegram developer app。Session file 是 credential，請放在 `MEDIAGENT_DATA_DIR` 底下，不要提交，也不要複製到 public media library。

載入 `.env`：

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

第一次 Telegram login 是兩步驟的本機流程。

要求 login code：

```bash
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --json
```

使用輸出的 `phone_code_hash`，搭配 Telegram 傳給你的 code：

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output"
}
```

接著執行：

```bash
uv run --locked mediagent tools run telegram.auth.login --input /path/to/local-telegram-login-complete.json --json
```

若 Telegram 要求 2FA password，請使用 `password_ref`。Inline 2FA password 會被拒絕：

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

驗證 configured session：

```bash
uv run --locked mediagent tools run telegram.auth.status --input examples/tools/telegram.auth.status.json --json
```

列出可選 dialogs，但不下載媒體：

```bash
uv run --locked mediagent tools run telegram.dialogs.list --input examples/tools/telegram.dialogs.list.json --json
```

從明確 trusted source 收集含媒體 messages：

```bash
uv run --locked mediagent tools run telegram.messages.collect --input examples/tools/telegram.messages.collect.json --json
```

預覽 deterministic Telegram sync，且不寫檔、不寫 DB rows：

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --dry-run --json
```

確認 source selector 與 limits 後，再執行 bounded sync：

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --json
```

Curated Telegram media 的用法：建立 private collection channel，把想下載的媒體 message links 貼進去。接著在 `examples/tools/telegram.messages.sync.link-inbox.json` 設定 channel selector 後執行：

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --dry-run --json
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --json
```

Configured user session 必須能讀取每個 linked original message。這個流程不會讓 Mediagent 發訊息、轉傳、刪除或管理 Telegram chats。

最新的小型 media live run 寫入：

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg
```

長影片 live run 寫入：

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4
```

重跑同一個 direct-link sync 會跳過已完成項目。

Shared-root Telegram 檔案會使用：

```text
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

若想讓 Telegram 使用自己的頂級目錄，設定：

```bash
MEDIAGENT_TELEGRAM_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/telegram
```

檔案會落在：

```text
$MEDIAGENT_DATA_DIR/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram cursors 會依 source 與 media-type scope 儲存，例如 `messages:saved_messages:photo-video`。只有 durable sync processing 成功後才會前進。

## Reddit OAuth 與 Saved Collection

Reddit V1 foundation 已有 fake-client coverage，但在使用者提供 Reddit app credentials 前不做真實 live verification。第一版把 saved posts 當作 curated source；不做 posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping 或 third-party extractors。

在 `.env` 加入本機專用值：

```bash
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=http://127.0.0.1:8765/reddit/callback
REDDIT_USER_AGENT='linux:mediagent:0.1 (by /u/your_username)'
REDDIT_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/reddit-oauth.json
```

載入 `.env`：

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

產生 authorization URL：

```bash
uv run --locked mediagent tools run reddit.auth.start --input examples/tools/reddit.auth.start.json --json
```

在瀏覽器打開回傳的 `data.authorization_url`。Redirect 後，把 `code` query parameter 填入以 `examples/tools/reddit.auth.exchange.json` 為基礎建立的本機 exchange input，然後執行：

```bash
uv run --locked mediagent tools run reddit.auth.exchange --input /path/to/local-reddit-auth-exchange.json --json
```

檢查 configured session：

```bash
uv run --locked mediagent tools run reddit.auth.status --input examples/tools/reddit.auth.status.json --json
```

只收集 saved media candidates，不下載：

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --json
```

不需要 credentials、DB writes 或 network 的 collector preview：

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
```

`reddit.saved.collect` 只回傳 normalized media items 與 optional cursor state。下載 orchestration 要等未來明確加入 `reddit.saved.sync`。

## 常見問題

- `ModuleNotFoundError: mediagent`：使用 `uv run --locked ...` 或設定 `PYTHONPATH=src`。
- exit code `2`：input、config、auth、permission、filesystem 或 database validation 問題。
- exit code `1`：runtime、network 或 rate-limit failure。
- unsafe path error：設定 `MEDIAGENT_DATA_DIR`，並寫入該目錄底下。
- X auth failure：檢查 token expiration、required scopes，以及 `X_CREDENTIALS_FILE` 是否在允許寫入 root 內。
- Pixiv auth failure：檢查 `PIXIV_CREDENTIALS_FILE`、token expiration、callback URL/code 是否過期，以及 credential file 是否在 `MEDIAGENT_DATA_DIR` 底下。若使用舊的 refresh-token 路徑，也檢查 `PIXIV_REFRESH_TOKEN`。
- Pixiv download 403：在 `download.http` headers 加上 `{"Referer":"https://www.pixiv.net/"}`。
- Telegram auth failure：檢查 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE`，以及 session file 是否在 `MEDIAGENT_DATA_DIR` 底下。
- Reddit auth failure：檢查 `REDDIT_CLIENT_ID`、`REDDIT_REDIRECT_URI`、`REDDIT_USER_AGENT`、`REDDIT_CREDENTIALS_FILE`、callback code 是否過期，以及 credential file 是否在 `MEDIAGENT_DATA_DIR` 底下。

## 安全提醒

X 與 Reddit 仍需要使用者提供 credentials 才能做 live verification。Pixiv 與 Telegram 已完成目前 deterministic sync slice 的使用者協助 live verification。未來 live runs 都仍需要使用者提供 credentials。
