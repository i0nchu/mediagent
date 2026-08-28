# Mediagent 工具目錄

## 漫畫工具（2026-08-14）

- `comic.link.sync`：exact nhentai gallery 或 JM album/photo/cover 下載、修復與 CBZ。
- `nhentai.auth.status`、`nhentai.auth.refresh`：安全檢查、刷新並保存 browser session。
- `nhentai.favorites.collect`：完整遠端收藏/auth 診斷，不下載，也不變更 membership。
- `nhentai.favorites.sync`：完整收藏 inbox snapshot；每個 gallery 為 exact。
- `jmcomic.auth.status`、`jmcomic.auth.login`：安全狀態與 mobile API session 登入保存。
- `jmcomic.favorites.folders.collect`：authenticated 遠端收藏資料夾名稱/FID discovery。
- `jmcomic.favorites.folders.register`：本機帳號 scoped 名稱-to-FID/URL fallback registration。
- `jmcomic.favorites.folders.list`：不連網列出本機 fallback alias。
- `jmcomic.favorites.collect`：完整指定資料夾 union/auth 診斷，不下載，也不變更 membership。
- `jmcomic.favorites.sync`：完整指定資料夾 union snapshot；active album 持續發現新章。
- `jmcomic.library.reconcile`：`plan` 對全 library 做唯讀章號盤點；`apply` 必須傳入 `confirm:true`，只從健康本機原始頁重封裝受影響 CBZ，並隔離被取代的 archive。

用這份文件快速理解目前已註冊的工具。精確 schema 請用：

```bash
uv run --locked mediagent tools inspect <tool-name> --json
```

執行工具：

```bash
uv run --locked mediagent tools run <tool-name> --input examples/tools/<tool-name>.json --json
```

支援安全預覽的工具可以加上 `--dry-run`。
週期服務請用 `--summary-json` 取代 `--json`，避免 journal 寫入龐大明細。

## Agent Core V1 Commands

Agent Core V1 是 local preview，會透過 SKILL allowlists 呼叫同一個 `ToolRegistry`。它不是獨立 platform layer，也不是 scheduler。

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect <skill-name> --json
uv run --locked mediagent agent run "<natural language task>" --dry-run --json
uv run --locked mediagent agent run "<natural language task>" --json
```

內建 SKILL：

- `explicit_link_download`
- `instagram_link_download`
- `instagram_saved_sync`
- `library_health_check`
- `pixiv_bookmark_sync`
- `telegram_inbox_download`

當沒有 SKILL 明確符合時，Agent run 會在 tool call 前拒絕 unsupported tasks。Ollama transport failures 會回傳 structured `llm_request_failed`，且會移除模型憑空產生的 destination path fields，除非使用者明確提供那些路徑。

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

- `storage.path.plan`：為一個 normalized media file 規劃 deterministic scanner-friendly library path。預設 layout 是 `<platform>/<storage_category>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>`；storage category 通常等於 media type，Pixiv manga 原始頁面則使用 `comic-pages`。Library root 依序使用 explicit `library_root`、`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`、`MEDIAGENT_LIBRARY_DIR`，最後 fallback 到 `${MEDIAGENT_DATA_DIR}/library`。平台專屬 root 已經屬於單一平台，因此預設不會再加入重複的 platform directory。
- `library.file.verify`：依照 SQLite 已知檔案紀錄檢查 local existence、size 與 checksum，並把 file health 標記成 `valid`、`missing`、`corrupt` 或 `unknown`。它不刪除檔案，也不連線來源平台。
- `library.content.deduplicate`：對 `.trash` 之外的 tracked downloaded files 計算 SHA-256；dry-run 只規劃，apply 讓一般媒體收斂為單一可見路徑，漫畫脈絡保持分離並盡可能使用 hard link。
- `library.trash.reconcile`：規劃或原子匯入被 pre-v10 cleanup 搬走的 downloaded rows，將其轉成明確 removed library state。它驗證原始相對路徑、記錄大小與 legacy trash candidate 的 SHA-256；不搬檔、保留重複舊 trash copies、忽略 managed/reconcile trash，任何 unmatched row 或 active identity 衝突會阻擋整次 apply。支援 dry-run，apply 前必須審查。
- `library.trash.status`：回報目前 service account 是否能安全寫入 `.trash/mediagent`，包含 UID/GID/mode 與未自動 purge 的 retention policy；工具 `success` 只代表查詢成功，呼叫者仍須檢查 `operational`。
- `library.trash.prepare`：只建立及驗證 `.trash/mediagent`，不改既有 `.trash` owner/permissions；拒絕 symlink 與 library 外路徑，支援唯讀 dry-run。
- `library.entry.remove`：以 `path` 或 `entry_id` 移到 `.trash/mediagent/<removal-id>/`、同步來源 rows 並抑制 repair；不支援 dry-run。
- `library.entry.restore`：以 `removal_id`、`entry_id` 或 managed path 驗證 checksum 後還原；不支援 dry-run。
- `library.entry.rename`：以 `path` 或 `entry_id` 改名，CBZ 會原子改寫 `ComicInfo.xml`；不支援 dry-run。

## Download And Metadata Tools

- `download.http`：下載單一遠端檔案到安全路徑，支援 dry-run、bounded attempts、`.partial` finalization、checksum、MIME/content-length validation、custom request headers 與 rate-limit metadata。
- `metadata.write`：將 normalized JSON metadata 寫到下載檔案旁邊，寫入前 redacts secrets。

## Pixiv Tools

- `pixiv.auth.login`：啟動或完成明確的本機 Pixiv OAuth/PKCE setup。沒有 `code` 或 `callback_url` 時回傳 login URL 與 code verifier；有 `code` 或 `callback_url` 加上 `code_verifier` 時交換 tokens，並可把 credential JSON 寫入 configured write roots。
- `pixiv.auth.status`：檢查 Pixiv credentials 是否可用，不輸出 secrets。可驗證帶 user ID 的可用 access token；若只有 refresh token，會測試 refresh 是否成功，但不寫 credential file。
- `pixiv.auth.refresh`：用明確提供的 refresh token 更新 Pixiv App API credentials，可寫入 configured write roots 內的 credential JSON。
- `pixiv.link.resolve`：解析使用者提供的 Pixiv artwork URL 或 `illust_id`，回傳 normalized downloadable media candidates，但不下載檔案。它會使用已設定的 Pixiv session，不會自行啟動 browser login；若需要使用者重新登入或 refresh，會回傳 `pixiv_auth_missing_credentials` 這類 structured auth errors。
- `pixiv.bookmarks.collect`：收集 configured account 的 Pixiv bookmarked illustrations/manga，normalize 單頁、多頁與 ugoira metadata，並可把 cursor 存入 SQLite。
- `pixiv.bookmarks.sync`：收集 Pixiv bookmarks、upsert/filter media items、規劃 scanner-friendly storage paths、用 `.partial` finalization 下載每個 `metadata.files[]` 檔案、記錄 local media files，並更新 parent item status。Pixiv work type 與實體 media type 分離：官方 manga 原始頁面仍是 photo files，但使用 `work_type:comic` 與 `comic-pages` storage category；多頁 `illust` 仍存入 `photo`。`package_comics:true` 會把每個新下載完成的漫畫封裝為 `comic` 下的一個 CBZ。Invisible/placeholder-only works 不會下載。`repair_missing_files:true` 可明確修復 DB downloaded 但必要檔案 missing/corrupt/unhealthy 或 `local_path` 不存在的項目；預設重跑仍保守跳過。Timer-style recurring sync 使用 `stop_on_known:true` 與 bounded `max_pages`；explicit full rebuild 使用 `full_sync:true`、省略 `limit/max_pages`、設定 `stop_on_known:false`。
- `pixiv.library.reconcile`：離線 plan/apply 舊 Pixiv library migration。更新 `work_type` / storage metadata、原子搬移既有 manga source files 與 sidecars 從 `photo` 或舊 `comic` 到 `comic-pages`、更新 SQLite file paths，並先 quarantine 已知 `s.pximg.net/.../limit_*.png` placeholder downloads 再移除其 file rows。Apply 需要 `confirm:true`，且永遠不會自動還原 `.trash` 內容。
- `pixiv.comics.package`：為每個完整下載的 Pixiv 漫畫規劃或建立 deterministic Kavita-oriented CBZ。Archive 位於 `comic/<series-directory>/`，包含 zero-padded 頁面名稱與 root-level `ComicInfo.xml`，並以 `comic-kavita-v2` 記錄。單篇使用唯一 series identity、`Number=1`、`Count=1`、`Format=One-Shot`；真正 Pixiv 系列會共用資料夾並保存可取得的 series/chapter metadata。寫入使用 `.partial` 加 atomic replacement。`migrate_legacy:true` 會重建 V1 date-layout archive，再透過 `library.entry.remove` 退役舊 CBZ；舊 media/source row 會保留並連到明確 removed entry，重跑會忽略它。不連線 Pixiv，也不刪除原始頁面。

Pixiv collector 不會自己下載檔案。完整 bookmark 下載請用 `pixiv.bookmarks.sync`。Explicit artwork URLs 可以直接交給 `link.media.sync`，它會把一個作品 URL 視為一個 media item、預設解析所有頁、與 `pixiv.bookmarks.sync` 去重、在下載時套用必要的 Pixiv `Referer`，且不把 runtime headers 寫入 metadata。若要手動下載單一檔案，請使用回傳的 `metadata.files[].url` 搭配 `download.http`；Pixiv 圖片下載通常需要：

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

## Core Link Tools

這些工具是 Phase 19 link-first 產品路徑的穩定入口。

- `link.queue.upsert`：queue 一個或多個 explicit URLs，使用 normalized URL dedupe，並合併 source provenance。它不解析也不下載媒體。
- `link.media.sync`：解析 explicit URLs 或 queued link records，會為 cron/daemon runs claim ready queued records、排程 retryable deferred records，將明確的 media candidates 轉成 normalized media items、dedupe known items、規劃 storage paths、下載 files、寫入 optional sidecar metadata、記錄 media-file state，並更新 parent item status。`retry_auth_skipped:true` 會在 platform session 可用後明確 reclaim 舊的 `requires_auth` / `login_wall` rows。

Public CLI shortcut:

```bash
mediagent link sync <url> --json
```

這個 shortcut 會 delegate 到 `link.media.sync`；它是 user-provided links 的 stable non-Telegram entry point。

`link.media.sync` 是 deterministic，可由 Python、CLI、cron、workflows 與未來 Agent/SKILL integrations 呼叫。它必須把寫入限制在 configured project-local roots 內，且不得持久化 resolver candidates 內帶 credential 的 headers。

已有 dedicated resolver 的已知平台頁面網域會保留給平台 resolver，不會 fallback 到 generic parser。Unsupported Instagram pages、Pixiv 非 artwork pages，以及 Imgur gallery/album 類 pages 會回傳 structured platform skips，而不是交給 `generic_html_media` 解析。

## Instagram Tools

Instagram support 包含 explicit post/Reel links，以及 configured account 的 saved-media feed；不掃 stories、profiles、messages、comments、likes、follows 或更廣泛 account activity。

一個 Instagram post URL 代表整個貼文。Carousel posts 預設下載所有 media resources；`img_index` 只保留為 source metadata，除非未來加入明確選項改變行為。Signed Instagram CDN URLs 是 runtime-only download data，不得持久化到 SQLite、sidecar metadata、logs、snapshots 或 tool output。

- `instagram.auth.status`：驗證 configured saved Instagram session，不暴露 cookies、session IDs、username、password 或 raw private API payloads。Session path 會在 fake-client callbacks、real-client loads 或 network work 前檢查 project-local roots 邊界。
- `instagram.auth.login`：使用明確本機 username/password credentials 建立或替換 saved local Instagram session。Session file 必須位於 allowed credential/data roots，並以嚴格權限寫入。
- `instagram.auth.ensure_session`：檢查 saved session，且只在 credentials 存在、cooldown 允許時低頻重新登入。Checkpoint 與 2FA 會停止自動化；rate-limit 與 temporary-block states 應延後工作。
- `instagram.link.resolve`：解析一個公開 Instagram `/p/<shortcode>/`、`/reel/<shortcode>/` 或 `/tv/<shortcode>/` URL 成 normalized media candidates。它不會自行執行 password login；非 Instagram hosts 或缺少 shortcode 會回傳 `instagram_media_unsupported`，missing/invalid session 會回傳可供 agent 判斷的 auth errors。
- `instagram.saved.collect`：透過 configured saved session sequentially 讀取收藏貼文並回傳 normalized whole-post items，不下載檔案。支援 bounded `limit` / `max_pages` 與 explicit full pagination；opaque cursors、session errors、checkpoints 與 rate limits 會保持 structured，runtime CDN URLs 不會出現在 public output。
- `instagram.saved.sync`：將 saved-feed collection 與共用 SQLite dedupe、scanner-friendly storage、downloads、file/item status、retry 與 missing-file repair 組合。Recurring runs 應使用 `stop_on_known:true` 與保守 page cap；explicit full sync 使用 `full_sync:true`，不捏造 item/page limit。

## Experimental Link Tools

這些工具在 public preview/compatibility story 確定前，仍屬 experimental helper surfaces。列出時使用 `--include-experimental`，inspect/run 時使用 `--allow-experimental`。

- `link.resolve.preview`：安全 preview 一個 explicit URL，不下載。已支援 direct media、bounded single-media HTML，以及已實作的小型 provider-specific resolver behavior。
- `link.resolve.to_media_item`：將 resolved link candidate 轉成 normalized media item，供既有 storage/download pipeline 使用。

目前不要把這些 experimental names 視為 stable public API。Promotion 必須保留既有 live-test commands 的 aliases，並同步更新 examples、本 catalog、`RUNBOOK.md` 與 localized handoff files。

## Agent-Only Low-Profile Skills

- `telegram.inbox.sync_links`：支援 selected inbox 的 `full_sync:true` full-source scan。此模式不會套用預設 100 messages scan limit；URL/media/file dedupe 仍由 tool layer 處理。Inbox 中的 `t.me` / `telegram.me` message links 會 bridge 到 Telegram 原生 message sync，保留 inbox provenance，並對 protected/inaccessible links structured skip；外部 URL 仍走 link resolver pipeline。`retry_auth_skipped:true` 會重試舊的 Telegram-ingested auth-dependent rows。

- `telegram_inbox_download`：讓 Agent Core 處理 configured Telegram inbox，但不把直接工具入口記錄為 public workflow commands。底層工具是 hidden stable surfaces：預設不列出，但知道名稱的使用者或 agent 仍可呼叫。

## Reddit Tools

Reddit auth/saved tools 已存在，但目前是 deferred legacy/advanced capability。當前產品方向是 explicit-link resolution，優先使用 anonymous/bounded behavior，並把 Redgifs 作為下一個 no-auth provider foundation。除非使用者明確恢復 auth-assisted account collection，否則不要基於 saved collection 繼續開發。

Saved-collection slice 只讀 OAuth identity/history 資料並收集 direct media candidates；不做 posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping 或 third-party extractors。

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
- Reddit credentials 可來自 `REDDIT_CREDENTIALS_FILE` 或 token 環境變數。只有在明確驗證 deferred auth-assisted path 時才使用 `reddit.auth.start` + `reddit.auth.exchange`，且一定要使用 unique descriptive `REDDIT_USER_AGENT`。
- Instagram credentials 來自 `INSTAGRAM_ACCOUNT`、`INSTAGRAM_SECRET` 與 `INSTAGRAM_SESSION_FILE`；session file 是 credential，應放在 `${MEDIAGENT_DATA_DIR}/credentials/` 底下。Link 或 saved-media sync 前優先使用 `instagram.auth.ensure_session`，只有明確建立本機 session 時才用 `instagram.auth.login`。
- `X_CREDENTIALS_FILE`、`PIXIV_CREDENTIALS_FILE`、`TELEGRAM_SESSION_FILE`、`REDDIT_CREDENTIALS_FILE` 與 `INSTAGRAM_SESSION_FILE` 應指向使用者明確管理的檔案。
- token exchange 與 refresh 的輸出不包含 raw tokens。
- SQLite run records 不得保存 raw access tokens、refresh tokens、cookies、sessions 或 bot tokens。
