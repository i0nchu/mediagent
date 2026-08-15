# Mediagent Runbook

## 本機漫畫 live test

第一次只使用 repo 內開發路徑：

```bash
export MEDIAGENT_DATA_DIR=/home/ion/projects/mediagent/tmp/live/comics
export MEDIAGENT_LIBRARY_DIR=/home/ion/projects/mediagent/tmp/live/comics/library
export MEDIAGENT_DB_PATH=/home/ion/projects/mediagent/tmp/live/comics/mediagent.sqlite3
export MEDIAGENT_NHENTAI_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/nhentai_session.json
export MEDIAGENT_JMCOMIC_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/jmcomic_session.json
mkdir -p "$MEDIAGENT_DATA_DIR/credentials" "$MEDIAGENT_LIBRARY_DIR"
uv run --locked mediagent tools run core.db.init --json
```

若 `.env` 仍有數字開頭的舊變數，不要 `source .env`；先改成 `MEDIAGENT_JMCOMIC_USERNAME`／`MEDIAGENT_JMCOMIC_PASSWORD`。

```bash
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.nhentai.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.nhentai.json --json
uv run --locked mediagent tools run nhentai.auth.status --input examples/tools/nhentai.auth.status.json --json
uv run --locked mediagent tools run nhentai.auth.refresh --input examples/tools/nhentai.auth.refresh.json --json
uv run --locked mediagent tools run nhentai.favorites.collect --input examples/tools/nhentai.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run nhentai.favorites.sync --input examples/tools/nhentai.favorites.sync.json --dry-run --json
```

nhentai 收藏需先從已登入瀏覽器匯出一次 cookie jar；可使用 Mediagent JSON session，或將 Netscape 格式 `cookies.txt` 指定給 `MEDIAGENT_NHENTAI_COOKIE_FILE`。refresh 會維持原格式並將權限設為 `0600`。不自動化帳密、CAPTCHA 或 proof-of-work。

工具結果有兩層語意。最外層 `status` 表示所要求的工具操作是否成功；對 `auth.status` 查詢而言，最外層 success 只表示檢查正常完成，不表示已登入。請查看 `data.auth_status`、`data.authenticated`、`data.reusable`、`data.remote_verified`。本機可載入但尚未向 provider 驗證的 session 會回報 `authenticated: null`、`remote_verified: false`。

瀏覽器匯入的 session 可能在 nhentai refresh endpoint 收到 HTTP 403，但 authenticated favorites 仍可正常使用。`nhentai.auth.refresh` 會再做一次只讀 favorites 驗證供診斷，但 refresh 操作仍以最外層 failure 回報，`error.code` 為 `nhentai_refresh_rejected`；data 會明確指出沒有 rotation、目前 auth 仍可用。若驗證也失敗，則回報 `nhentai_auth_required`，此時需重新從瀏覽器匯出 cookie。

JMComic 可以直接用設定好的帳號密碼建立並重用 session，不需要瀏覽器 cookie。也接受 `JMCOMIC_USERNAME`、`JMCOMIC_PASSWORD`、`JMCOMIC_SESSION_FILE`；推薦的 `MEDIAGENT_JMCOMIC_*` 若同時存在會優先採用。`jmcomic.auth.login` 會忽略並取代無效的舊 session，不會在送出登入 request 前就失敗。

`jmcomic.favorites.collect` 與 `.sync` 遇到 `jmcomic_auth_required` 時，每輪最多用設定好的帳密恢復一次。Recovered session 會立即保存，輪替 cookie 會在 collection 與每個 album resolve 後 checkpoint。可從 summary 欄位 `auth_recovery_attempted`、`auth_recovered`、`session_checkpointed` 判斷，輸出不含 session 內容。system JMComic service 為初次完整同步保留 18 小時。

```bash
uv run --locked mediagent tools run jmcomic.auth.status --input examples/tools/jmcomic.auth.status.json --json
uv run --locked mediagent tools run jmcomic.auth.login --input examples/tools/jmcomic.auth.login.json --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.jmcomic-album.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.jmcomic-album.json --json
uv run --locked mediagent tools run jmcomic.favorites.collect --input examples/tools/jmcomic.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run jmcomic.favorites.sync --input examples/tools/jmcomic.favorites.sync.json --dry-run --json
```

若要改用瀏覽器 session，可透過 `MEDIAGENT_JMCOMIC_COOKIE_FILE` 或 `JMCOMIC_COOKIE_FILE` 指定 Netscape `cookies.txt`；也可讓 `*_SESSION_FILE` 直接指向 `.txt`／`.cookies`。只會匯入 trusted JMComic domains 的 cookies；後續寫回會維持 Netscape 格式與 `0600`。同時設定 cookie-file 與 session-file 時，cookie-file 優先。

第二次相同執行應下載 0 個健康頁面並回報 existing CBZ。直接 JM album 不建立 follow，只有收藏同步會。執行期間不要刪 SQLite `-wal`／`-shm`。

follow 的實作是由 timer 定期重跑 `jmcomic.favorites.sync`，不是常駐 daemon。完整收藏 snapshot 更新 active membership，之後重新解析每個 active album 以發現新章。system-level 範例位於 `deploy/systemd/system/`；`/data/services/mediagent` unit 會以 `server` 帳號執行、明確設定其 `HOME`／`PATH`，並使用共用 non-blocking run lock 與精簡的 `--summary-json` journal。`nhentai.favorites.sync` 也可用相同 timer 發現新收藏的 exact gallery，但不會推測或追蹤系列。

若在 filename-hash descramble 修正前下載的 JMComic 頁面呈現水平帶狀錯位，`repair_missing_files` 不足以修正，因為檔案仍存在且 DB 視為健康。請用 `mediagent link sync '<album-url>' --overwrite --json` 明確重新下載該 exact album 並重建 CBZ。流程會用 `.partial` 與 atomic replacement；先在本機確認圖片／CBZ 正常，再部署至 server。

Telegram inbox 與未來自製 inbox 不需要各自呼叫平台漫畫工具。支援的 nhentai／JMComic links 會經過共用 `link.media.sync` intake，在 generic HTML resolution 前自動分派至 exact comic adapter。因此從 inbox 傳入 direct comic link，只會下載／封裝該連結的作品，不會啟用 series follow。可查看 `summary.comic_links_considered` 與 CBZ counters 確認分派結果。

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
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

## Agent Core V1 Smoke Checks

Agent Core V1 預設使用 Ollama：

```bash
export MEDIAGENT_LLM_PROVIDER=ollama
export MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434
export MEDIAGENT_OLLAMA_MODEL=qwen3:8b
```

檢查內建 SKILL：

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
```

預覽 explicit-link task：

```bash
uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --dry-run --json
```

不支援的任務應該在任何 tool call 前失敗：

```bash
uv run --locked mediagent agent run "我目前有存在的 telegram inbox 嗎？" --dry-run --json
```

預期結果：`status:"failure"`、`error.code:"unsupported_task"`、`skill:null`，且沒有 tool steps。

LLM transport failures 應該回傳 structured result，而不是 Python traceback：

```bash
MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:9 MEDIAGENT_OLLAMA_TIMEOUT_SECONDS=0.2 \
  uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --json
```

預期結果：`status:"failure"` 且 `error.code:"llm_request_failed"`。

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

解析單一 Pixiv artwork URL，但不下載檔案：

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851"}' \
  | uv run --locked mediagent tools run pixiv.link.resolve --input - --json
```

透過共享 link-first pipeline 下載單一 Pixiv artwork URL：

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851","write_sidecar_metadata":false}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

這條路徑會把一個 artwork URL 視為整個作品，預設解析所有 original pages，與 `pixiv.bookmarks.sync` 去重，並在下載時套用必要的 Pixiv `Referer`，但不會持久化 runtime headers。若 credentials 缺失或過期，`pixiv.link.resolve` 會回傳 structured auth error 與建議的 Pixiv auth tool，不會自行啟動 browser login。

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
$MEDIAGENT_LIBRARY_DIR/<platform>/<storage_category>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
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

Operator note：`MEDIAGENT_LIBRARY_DIR` 的變更只會影響未來的 target planning。使用同一份 SQLite DB 時，已經是 terminal 狀態的 Pixiv items 仍會被 dedupe，不會自動重新填入新的 root。搬移部署時，請把 DB 與 library files 視為同一組狀態一起處理。

Pixiv 會分開保存 file media type 與 work type。漫畫原始頁面仍是 photo files，但官方 Pixiv `type:manga` 會使用 `work_type:comic` 與 `comic-pages` storage category；多頁 `type:illust` 仍是 illustration 並放在 `photo`。封裝後的 CBZ 使用 `comic`。

先規劃舊 DB/library 更新，不做任何寫入：

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.plan.json --json
```

檢查 summary 並停止重疊的 Pixiv sync jobs 後，再明確套用：

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.apply.json --json
```

Apply 會原子搬移既有 manga source files 與相鄰 JSON sidecars 到 `comic-pages`、更新 SQLite paths/metadata，並 quarantine 已知 Pixiv placeholder downloads；此步驟不連線 Pixiv。已經移到 `.trash` 的檔案會視為缺失，永遠不會從 trash 自動還原。

Reconciliation 後，預覽 DB 已完成但 library path 缺檔的 source-backed repair：

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync \
  --input examples/tools/pixiv.bookmarks.sync.repair.json --dry-run --json
```

只有確定要重新下載 missing library files 時，才移除 `--dry-run` 執行相同命令。`repair_missing_files` 是 opt-in；一般 timer run 仍尊重 DB `downloaded` 狀態，repair 會在規劃路徑下載新副本，並保留 `.trash` 原狀。

完成 reconciliation 與必要 repair 後，先預覽舊漫畫封裝：

```bash
uv run --locked mediagent tools run pixiv.comics.package \
  --input examples/tools/pixiv.comics.package.json --dry-run --json
```

移除 `--dry-run` 才會建立 CBZ。Committed example 設定了 `migrate_legacy:true`：工具只讀取完整且健康的原始頁面，透過 `.partial` 與 atomic replacement 寫入 Kavita V2 archive、記錄 SQLite，並保留原始頁面。V2 成功後，舊 V1 date-layout CBZ 會移到 `library/.trash/mediagent-comic-v1`，stale DB row 才會刪除。未來 bookmark sync 可設定 `package_comics:true`，自動封裝新下載的漫畫。

Kavita V2 每個系列使用一個資料夾。Pixiv 單篇會取得自己的唯一 series identity；有真正 Pixiv series metadata 的作品會共用同一資料夾，並從 normalized comic contract 使用 `Series`、`Number`、optional `Volume` 與 optional `Count`。

Pixiv 圖片範例：

```text
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic/作品標題 [pixiv-139193091]/作品標題 [pixiv-139193091].cbz
```

若沒有設定 `MEDIAGENT_PIXIV_LIBRARY_DIR`，shared-root 範例是：

```text
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic/作品標題 [pixiv-139193091]/作品標題 [pixiv-139193091].cbz
```

若 Immich 會掃描 Pixiv external library，但漫畫要交給其他閱讀器，請在該 external library 的 Scan Settings 加入以下兩個 exclusion patterns，然後重新掃描：

```text
**/comic/**
**/comic-pages/**
```

Kavita 只需指向 `pixiv/comic`，不要指向 `pixiv` 或 `comic-pages`。`comic` 的每個 immediate child 都是一個 series directory，comic root 不會直接放 archive。`comic-pages` 保留作為 Mediagent 修復或重建 CBZ 的無損來源。

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

## Instagram Saved Session 與 Link Test

Instagram support 採 explicit-link first。只用於使用者提供的公開 post、carousel、Reel 或 tv URLs。Resolver 會把一個 Instagram post URL 視為整個 post，因此 carousel links 預設會下載所有 resources。Resolver 不會自行執行 password login。

本機 setup 使用 `.env`：

```bash
set -a
source .env
set +a
```

檢查 saved session：

```bash
uv run --locked mediagent tools run instagram.auth.status --json
```

如果 session missing 或 invalid，且 credentials 已設定，明確呼叫修復工具：

```bash
uv run --locked mediagent tools run instagram.auth.ensure_session --json
```

只解析一個 link，不下載：

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/"}' \
  | uv run --locked mediagent tools run instagram.link.resolve --input - --json
```

透過 shared link pipeline 下載整個 post：

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

下載檔案會落在：

```text
$MEDIAGENT_DATA_DIR/library/instagram/photo/<yyyy>/<mm>/
$MEDIAGENT_DATA_DIR/library/instagram/video/<yyyy>/<mm>/
```

Signed Instagram CDN URLs 是 runtime-only。需確認它們不會出現在 SQLite、sidecar metadata、logs、snapshots 或 committed fixtures。

## Link-First Resolver Smoke Checks

目前主要路徑是 explicit URL resolution，而不是 account saved/bookmark collection。開發 Phase 19 link-first tools 時，優先使用以下檢查。

列出 experimental link tools：

```bash
uv run --locked mediagent tools list --json --include-experimental
```

檢查 stable link tools：

```bash
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
```

Queue 一個 explicit URL，但不下載：

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","ingest_platform":"cli"}' \
  | uv run --locked mediagent tools run link.queue.upsert --input - --json
```

透過 core link pipeline 解析並下載 explicit URL：

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

使用 public link entry point 執行同一條 workflow，且不需要撰寫 tool JSON：

```bash
uv run --locked mediagent link sync 'https://example.com/path/to/media.jpg' --write-sidecar-metadata --json
```

從 cron 或 daemon worker 執行 queued links：

```bash
uv run --locked mediagent tools run link.media.sync --json
```

Queued runs 會以短租約 claim ready links、跳過其他 worker 尚未過期的 leases，且只在 `next_attempt_at` 到期後納入 retryable `deferred` links。Login walls、unsafe URLs、unsupported media、deleted/removed content 與 access controls 等 permanent skips 不會重試。

檢查目前 preview resolver：

```bash
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
```

不下載，只 preview 一個 explicit URL：

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","record":false}' \
  | uv run --locked mediagent tools run link.resolve.preview --input - --json --allow-experimental
```

預期行為：

- direct public image/video/audio URLs 會在 full HTML fetch 前被解析
- public single-media HTML 只有在存在單一明確 candidate 時才可解析
- Reddit static galleries 可解析成多個 photo candidates；complex galleries、login-required、JavaScript-required、blocked、unsafe 或 ambiguous pages 會回傳 structured skip reasons
- 下載步驟必須重新執行 URL safety、redirect、MIME 與 byte-limit checks，不可只信任 preview output
- 如果使用 sync/download command，output files 必須留在 `${MEDIAGENT_DATA_DIR}` 底下

Redgifs direct/watch links 已作為 no-auth provider foundation 實作。當 public HTML 暴露 direct MP4 candidate 時，direct `redgifs.com/watch/<id>` links 應解析為 `origin_source: "redgifs"`、`media_type: "video"`、file key `v0`，並落在 scanner-friendly storage：`library/redgifs/video/<yyyy>/<mm>/...`。

Reddit explicit links 目前使用 anonymous/bounded behavior。如果 Reddit page 把外部媒體藏在 login wall 或 dynamic client data 後面，resolver 應以 `login_wall` 或 `external_source_hidden` 跳過。除非使用者明確重啟 auth-assisted collection，否則不要把 Reddit saved collection 當作下一個產品路徑。

## Deferred Reddit OAuth 與 Saved Collection

Reddit V1 auth/saved tooling 已有 fake-client coverage，但它是 deferred legacy/advanced capability。它不得 posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping 或使用 third-party extractors。

只有在明確驗證 legacy auth-assisted path 時才使用本節。

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

`reddit.saved.collect` 只回傳 normalized media items 與 optional cursor state。下載 orchestration 不是目前方向；除非使用者明確選擇恢復 auth-assisted account collection，否則不要加入 `reddit.saved.sync`。

## 常見問題

- `ModuleNotFoundError: mediagent`：使用 `uv run --locked ...` 或設定 `PYTHONPATH=src`。
- exit code `2`：input、config、auth、permission、filesystem 或 database validation 問題。
- exit code `1`：runtime、network 或 rate-limit failure。
- unsafe path error：設定 `MEDIAGENT_DATA_DIR`，並寫入該目錄底下。
- X auth failure：檢查 token expiration、required scopes，以及 `X_CREDENTIALS_FILE` 是否在允許寫入 root 內。
- Pixiv auth failure：檢查 `PIXIV_CREDENTIALS_FILE`、token expiration、callback URL/code 是否過期，以及 credential file 是否在 `MEDIAGENT_DATA_DIR` 底下。若使用舊的 refresh-token 路徑，也檢查 `PIXIV_REFRESH_TOKEN`。
- Pixiv download 403：在 `download.http` headers 加上 `{"Referer":"https://www.pixiv.net/"}`。
- Telegram auth failure：檢查 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE`，以及 session file 是否在 `MEDIAGENT_DATA_DIR` 底下。
- Reddit explicit link 回傳 `login_wall` 或 `external_source_hidden`：當 public HTML 沒暴露 media URL 時這是預期 skip。可用時優先使用 Redgifs 等 direct provider links。
- Deferred saved-collection tooling 的 Reddit auth failure：檢查 `REDDIT_CLIENT_ID`、`REDDIT_REDIRECT_URI`、`REDDIT_USER_AGENT`、`REDDIT_CREDENTIALS_FILE`、callback code 是否過期，以及 credential file 是否在 `MEDIAGENT_DATA_DIR` 底下。

## 安全提醒

目前擴展路徑是 explicit link resolution，優先使用 no-auth behavior。除非使用者明確恢復，X 與 Reddit auth-assisted collection 都維持 deferred。Pixiv 與 Telegram 已完成目前 deterministic sync slice 的使用者協助 live verification。若未來使用平台特定 login tool，live runs 仍需要使用者提供 credentials。
