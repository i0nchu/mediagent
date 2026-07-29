# Mediagent 工具目錄

用這份文件快速理解目前已註冊的工具。精確 schema 請用：

```bash
uv run --locked mediagent tools inspect <tool-name> --json
```

執行工具：

```bash
uv run --locked mediagent tools run <tool-name> --input examples/tools/<tool-name>.json --json
```

支援安全預覽的工具可以加上 `--dry-run`。

## Auth Tools

- `auth.session.status`：檢查 provider session 是否可用，不輸出 secrets。X 會委派到 X auth status。
- `auth.session.refresh`：透過 platform adapter refresh session。X 可寫入 `credential_output_path` 或 `X_CREDENTIALS_FILE`。
- `auth.session.revoke`：只回傳明確的本機 credential revocation guidance，不會自動 revoke remote session，且需要確認。

## Core Tools

- `core.env.check`：檢查必要環境變數與設定路徑。
- `core.db.init`：初始化 SQLite，建立 runs、media items、media files、sync cursors、auth sessions 與 future workflows 相關表。
- `core.cleanup.media_state`：規劃或套用保守的 live-test media-state cleanup。Planning mode 不修改檔案或 SQLite；apply mode 需要 `confirm: true`，會先把既有 media files 移到 quarantine，再移除 matching media file rows，並把 matching media items reset 為 `discovered`。Credential paths 受到保護，不會成為可執行 cleanup files。
- `core.path.prepare`：解析並驗證安全檔案路徑，拒絕寫入允許 roots 之外。
- `core.run.record`：將 tool/workflow run summary 寫入 SQLite，寫入前 redacts secrets。
- `core.sync_cursor.get`：讀取平台同步 cursor。
- `core.sync_cursor.set`：寫入平台同步 cursor。

## Media Tools

- `media.item.upsert`：用 `platform + remote_id` upsert discovered media item。
- `media.item.filter_new`：下載前過濾已知、已下載、失敗、跳過與新項目。
- `media.item.set_status`：明確更新已知 media item 的 parent status。Deterministic sync helpers 會在檔案下載完成後用這條路徑標記 `downloaded`、`partial` 或 `failed`。
- `media.file.upsert`：記錄 local media file 的 remote URL、local path、library-relative path、storage layout、MIME type、byte size、checksum、health 與 status。

支援 media types：

- `photo`
- `video`
- `audio`

## Storage And Library Tools

- `storage.path.plan`：為一個 normalized media file 規劃 deterministic scanner-friendly library path。預設 layout 是 `<platform>/<media_type>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>`。Library root 依序使用 explicit `library_root`、`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`、`MEDIAGENT_LIBRARY_DIR`，最後 fallback 到 `${MEDIAGENT_DATA_DIR}/library`。平台專屬 root 已經屬於單一平台，因此預設不會再加入重複的 platform directory。
- `library.file.verify`：依照 SQLite 已知檔案紀錄檢查 local existence、size 與 checksum，並把 file health 標記成 `valid`、`missing`、`corrupt` 或 `unknown`。它不刪除檔案，也不連線來源平台。

## Download And Metadata Tools

- `download.http`：下載單一遠端檔案到安全路徑，支援 dry-run、bounded attempts、`.partial` finalization、checksum、MIME/content-length validation、custom request headers 與 rate-limit metadata。
- `metadata.write`：將 normalized JSON metadata 寫到下載檔案旁邊，寫入前 redacts secrets。

## Pixiv Tools

- `pixiv.auth.login`：啟動或完成明確的本機 Pixiv OAuth/PKCE setup。沒有 `code` 或 `callback_url` 時回傳 login URL 與 code verifier；有 `code` 或 `callback_url` 加上 `code_verifier` 時交換 tokens，並可把 credential JSON 寫入 configured write roots。
- `pixiv.auth.status`：檢查 Pixiv credentials 是否可用，不輸出 secrets。可驗證帶 user ID 的可用 access token；若只有 refresh token，會測試 refresh 是否成功，但不寫 credential file。
- `pixiv.auth.refresh`：用明確提供的 refresh token 更新 Pixiv App API credentials，可寫入 configured write roots 內的 credential JSON。
- `pixiv.bookmarks.collect`：收集 configured account 的 Pixiv bookmarked illustrations/manga，normalize 單頁、多頁與 ugoira metadata，並可把 cursor 存入 SQLite。
- `pixiv.bookmarks.sync`：收集 Pixiv bookmarks、upsert/filter media items、規劃 scanner-friendly storage paths、用 `.partial` finalization 下載每個 `metadata.files[]` 檔案、記錄 local media files，並把 parent item status 更新為 `downloaded`、`partial` 或 `failed`。JSON sidecar metadata 需用 `write_sidecar_metadata` 明確啟用。使用 `media_types` filtering 時，sync cursor 會依 filter scope 儲存，例如 `bookmarks:public:photo`，不會修改 unscoped bookmark cursor。

Pixiv collector 不會自己下載檔案。完整 bookmark 下載請用 `pixiv.bookmarks.sync`。若要手動下載單一檔案，請使用回傳的 `metadata.files[].url` 搭配 `download.http`；Pixiv 圖片下載通常需要：

```json
{"Referer":"https://www.pixiv.net/"}
```

## Telegram Tools

Telegram 被視為 media source，不是通知、轉傳或 chat-management platform。實作使用 Telethon-compatible user MTProto session boundary；Telegram session file 是 credential。

- `telegram.auth.login`：啟動或完成明確的本機 Telegram user-session login。`start` 會要求 Telegram login code 並回傳第二步需要的 `phone_code_hash`；`complete` 接收 login code 與 `phone_code_hash`，可透過 `password_ref` 支援 optional 2FA，且只會把 configured session file 寫在允許的 credential/data roots 底下。
- `telegram.auth.status`：驗證 configured Telegram user-session credentials，不輸出 secrets。會檢查 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE` / `MEDIAGENT_DATA_DIR`，只回傳安全的 session/account status。
- `telegram.dialogs.list`：列出 configured user session 可見且可選的 dialogs，回傳 safe chat identifiers、display titles、chat type、username 與 access hints；不回傳 message text 或 media bytes。
- `telegram.messages.collect`：從 explicit chats 或 message links 收集含媒體 messages，normalize photos、videos、audio、voice/audio documents 與 media documents 成 shared media items。可讀取與儲存 per-source cursors，但不下載 media bytes。設定 `extract_message_links: true` 時，會掃描 collected message text/captions 裡的 Telegram message links、解析 linked original messages，並 normalize 其 media；linked source cursors 不會前進。
- `telegram.media.download`：透過 Telegram client boundary 下載單一 Telegram media object 到安全 local path，支援 dry-run、直接 stream 到 `.partial`、checksum、MIME validation、finalization、timeout enforcement 與 path safety。
- `telegram.messages.sync`：收集 selected Telegram media messages、upsert/filter media items、規劃 scanner-friendly storage paths、透過 `telegram.media.download` 下載媒體、記錄 local media files、更新 parent item status，並只在 durable processing 成功後推進 per-source scoped cursors。

預設 source selection 必須明確。請使用 trusted chat selector，例如 Saved Messages、private collection channel、allowlisted group/channel 或 explicit message links；不要預設掃描所有 dialogs。

Curated Telegram 用法：把 `chat` 指向使用者自己的 private collection channel，並設定 `extract_message_links: true`。Configured user session 必須能讀取每個 linked original message；Mediagent 不繞過 protected 或 inaccessible chats。

小型 curated media download、一小時 linked video download、scanner-friendly layout placement、`library.file.verify` 與重跑去重已於 2026-07-24 UTC 完成 live verification。

## Reddit Tools

Reddit 被視為透過 authenticated user's saved listing curated 的 media source。第一版只讀 OAuth identity/history 資料並收集 direct media candidates；不做 posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping 或 third-party extractors。

- `reddit.auth.start`：產生 Reddit OAuth authorization URL，預設 scopes 為 `identity` 與 `history`。
- `reddit.auth.exchange`：用 Reddit OAuth callback code 交換 tokens，並可把 credential JSON 寫入 configured write roots。輸出不包含 raw tokens、client secrets 或 authorization codes。
- `reddit.auth.refresh`：從 `REDDIT_CREDENTIALS_FILE` 或 `refresh_token_ref` refresh Reddit OAuth access credentials；若 Reddit 沒回傳新的 refresh token，會保留既有 refresh token。
- `reddit.auth.status`：驗證 configured Reddit access token，並回傳安全 account/status response。
- `reddit.saved.collect`：收集 `username` 或 `me` 的含媒體 saved items，支援 `after` pagination 與 optional cursor storage，並 normalize 成 shared media items。它不下載檔案，也不寫入 media-file records。

第一版 parser 支援 Reddit-hosted single images、Reddit gallery images、Reddit-hosted video fallback URLs，以及具有穩定副檔名的 direct external image/video URLs。Unsupported embeds 與沒有 direct media 的 comments 會被跳過。

## X Tools

- `x.auth.start`：產生 X OAuth 2.0 PKCE authorization URL、state、code verifier 與 challenge。
- `x.auth.exchange`：用 authorization code 換 token，只回 redacted session metadata；raw tokens 可寫入 `credential_output_path` 或 `X_CREDENTIALS_FILE`。
- `x.auth.refresh`：refresh X OAuth tokens，若 X 沒回傳新的 refresh token，會保留既有 refresh token。
- `x.auth.status`：透過 `/2/users/me` 驗證 token、expiration、scopes 與 authenticated user ID。
- `x.bookmarks.collect`：抓取 authenticated user 的含媒體 bookmarks，normalize 成 media items，回傳 rate-limit metadata，並可把 pagination cursor 存入 SQLite。

## Credential Notes

- X credentials 可來自 `X_ACCESS_TOKEN` / `X_REFRESH_TOKEN`，或 `X_CREDENTIALS_FILE`。
- Pixiv credentials 可來自 `PIXIV_CREDENTIALS_FILE`、`PIXIV_REFRESH_TOKEN` 或 `PIXIV_ACCESS_TOKEN`。第一次本機 setup 優先使用 `pixiv.auth.login`。
- Telegram credentials 來自 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 與 `TELEGRAM_SESSION_FILE`；session file 是 credential，應放在 `${MEDIAGENT_DATA_DIR}/credentials/` 底下。第一次本機 setup 優先使用 `telegram.auth.login`。
- Reddit credentials 可來自 `REDDIT_CREDENTIALS_FILE` 或 token 環境變數。第一次 setup 優先使用 `reddit.auth.start` + `reddit.auth.exchange`，且一定要使用 unique descriptive `REDDIT_USER_AGENT`。
- `X_CREDENTIALS_FILE`、`PIXIV_CREDENTIALS_FILE`、`TELEGRAM_SESSION_FILE` 與 `REDDIT_CREDENTIALS_FILE` 應指向使用者明確管理的檔案。
- token exchange 與 refresh 的輸出不包含 raw tokens。
- SQLite run records 不得保存 raw access tokens、refresh tokens、cookies、sessions 或 bot tokens。
