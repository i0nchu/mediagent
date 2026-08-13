# Mediagent 目前狀態

## 2026-08-13 漫畫來源更新

- SQLite schema 已升為 v8，具備原子收藏 snapshot 與 active/inactive membership。
- nhentai 支援 exact gallery、完整收藏分頁、可重用及刷新之瀏覽器 cookie session（0600）。
- JMComic 支援 album/photo/可信封面、加密 mobile API、可重用登入 session、完整 album／收藏 manifest 與垂直切片還原。
- `comic.link.sync` 永遠 exact；`nhentai.favorites.sync` 以 gallery exact 同步；`jmcomic.favorites.sync` 只追蹤 active favorite albums。
- 共用 link intake 現在會在 generic HTML resolution 前，先把辨識到的 nhentai／JMComic links 分派給 exact comic adapter。direct `link.media.sync`、queued links、Telegram inbox，以及未來沿用相同 queue/tool boundary 的 inbox 都會生效；Telegram provenance 會保留，但不會建立 follow state。
- 完整章節會原子封裝為含 `ComicInfo.xml` 的 Kavita CBZ；只有一章的 JM album 仍維持穩定 series layout，避免未來新增章節時搬動舊 CBZ。
- 取消收藏不刪媒體，不完整 snapshot 不提交。
- 本次 locked offline suite 為 327 tests 全數通過。

## 已完成

- Package layout 位於 `src/mediagent/`。
- `main.py` 是薄啟動入口。
- `pyproject.toml` 已設定 console script：`mediagent = mediagent.cli:main`。
- Tool contract 位於 `src/mediagent/core/tooling.py`。
- Tool registry 位於 `src/mediagent/tools/defaults.py`。
- CLI bridge 位於 `src/mediagent/cli.py`。
- Agent Core V1 位於 `src/mediagent/agent/`，包含 SKILL loading、strict JSON action parsing、Ollama integration、tool allowlist enforcement、dry-run/execute boundaries，以及 compact/redacted tool-result feedback。
- Built-in English agent SKILL files 位於 `src/mediagent/agent/skills/builtin/`。
- Agent CLI commands 已建立：`mediagent agent run`、`mediagent agent skills list`、`mediagent agent skills inspect`。
- SQLite 初始化位於 `src/mediagent/core/db.py`，目前 schema version 是 `8`，並支援舊 media item/file table、stable `link_queue` lifecycle/retry/provenance fields 與漫畫來源收藏 memberships 的 idempotent migration。
- 檔案安全 helper 位於 `src/mediagent/core/filesystem.py`。
- credential/auth primitives 位於 `src/mediagent/core/auth.py`。
- rate-limit metadata parsing 位於 `src/mediagent/core/rate_limit.py`。
- secret redaction helper 位於 `src/mediagent/core/redaction.py`。
- HTTP abstraction 位於 `src/mediagent/core/http.py`，`download.http` 支援 custom request headers。
- Core URL intake 與 resolver helpers 位於 `src/mediagent/core/links.py`。
- Reddit public-link parsing helpers 位於 `src/mediagent/platforms/reddit/links.py`。
- X platform support 位於 `src/mediagent/platforms/x/`。
- Pixiv platform support 位於 `src/mediagent/platforms/pixiv/`，包含 local OAuth/PKCE setup、explicit refresh-token auth、token refresh、bookmark API calls、多頁作品 parsing 與 ugoira metadata preservation。
- Telegram platform support 位於 `src/mediagent/platforms/telegram/`，包含 Telethon-backed user-session configuration、explicit login boundaries、session status boundaries、dialog listing、message collection/link-inbox boundaries、media normalization 與 Telegram-specific media download。
- `telegram.dialogs.list` 回傳的 Telegram numeric dialog selectors 可以用字串或 explicit object ID 形式傳回 collect/sync tools。
- Reddit platform support 位於 `src/mediagent/platforms/reddit/`，包含 OAuth config/auth helpers、saved-listing API calls，以及第一版 image/gallery/video/direct-media shapes parsing。
- Reddit explicit-link support 已透過 `reddit_media_link` resolver 建立，支援 direct `i.redd.it` image URLs、direct `v.redd.it` MP4 video-only URLs、Reddit post/share links、bounded anonymous HTML、搭配靜態非敏感 `over18=1` 的 `old.reddit.com` fallback、static galleries、preview-fallback galleries，以及 manifest/login-wall cases 的 structured skips。
- Instagram platform support 位於 `src/mediagent/platforms/instagram/`，包含 saved-session auth boundaries、explicit local login、bounded session repair、post/Reel URL parsing 與 post-level resource normalization。
- Instagram explicit-link support 已透過 `instagram_media_link` resolver 建立，支援使用 configured saved local session 解析公開 `/p/<shortcode>/`、`/reel/<shortcode>/` 與 `/tv/<shortcode>/` URLs。
- 已有 dedicated resolver 的已知平台頁面網域會被 `reserved_platform_page` guard 接住，因此 unsupported Instagram pages、Pixiv 非 artwork pages，以及 Imgur gallery/album 類 pages 會回傳 structured skips，而不是 fallback 到 generic HTML/media resolution。既有 live DB/library 中的 `instagram_com` rows 是加入此 guard 前的歷史殘留。
- Deterministic sync helpers 位於 `src/mediagent/core/sync.py`。
- Universal storage planning 位於 `src/mediagent/core/storage.py`。
- 預設 shared-root storage layout 是 `scanner-friendly-v2`：`<platform>/<storage_category>/<yyyy>/<mm>/<filename>`。Storage category 通常等於 media type；Pixiv 漫畫原始頁面仍是 photo files，但使用 `comic-pages`，封裝後的 CBZ 使用 `comic`。
- 已透過 `MEDIAGENT_<PLATFORM>_LIBRARY_DIR` 支援平台專屬 library root，例如 `MEDIAGENT_PIXIV_LIBRARY_DIR`。
- 平台專屬 root 會被視為已經屬於該平台，因此預設會省略額外 platform directory。
- Pixiv bookmark sync 已支援 collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update。
- Pixiv artwork normalization 會保存 `work_type: illustration|comic|animation`；官方 `type:manga` 原始頁面存入 `pixiv/comic-pages/...`、deterministic CBZ 存入 `pixiv/comic/...`，`illust` 即使多頁仍存入 `pixiv/photo/...`。
- `pixiv.comics.package` 會把完整下載的漫畫頁面原子封裝為含 `ComicInfo.xml` 的 deterministic Kavita-oriented CBZ；單篇有唯一 series identity，真正系列共用資料夾，而 `migrate_legacy:true` 會重建 V1 archives，再把舊副本移到 `.trash/mediagent-comic-v1`。`pixiv.bookmarks.sync` 可用 `package_comics:true` opt in。
- Pixiv invisible stubs 與只包含 `s.pximg.net/.../limit_*.png` 的 placeholder response 會標記為 unavailable，不會下載。
- `pixiv.bookmarks.sync` 支援明確的 `repair_missing_files:true`；預設重跑仍會跳過 DB 中的 downloaded items，即使外部清理已把檔案移到 `.trash`。
- Pixiv bookmark sync 使用 `media_types` filtering 時會存入 scoped cursor，例如 `bookmarks:public:photo`。
- Telegram message sync 會在 durable processing 成功後儲存 per-source scoped cursors，例如 `messages:saved_messages:photo-video`。
- 低調的 Telegram inbox link resolver support 已作為 hidden stable tools 提供給 Agent SKILL 使用。它把 Telegram 視為 ingest provenance，並使用解析後的 `origin_source` 作為 media item 與 storage layout 的平台。
- Conservative cleanup/recovery support 已透過 `core.cleanup.media_state` 建立，可規劃 media-state cleanup，並在 DB reset 前先 quarantine files。
- `media_files` 使用穩定的非空 `file_key`，即使 `remote_url` 或 `local_path` 缺少也能保持 upsert idempotent。
- `media_files` 可記錄 library-relative path、storage layout version、file health、source timestamp 與 verification timestamp。
- stable JSON examples 位於 `examples/tools/`。
- fake HTTP 與 recorded-response fixtures 位於 `tests/fixtures/`。
- 測試位於 `tests/`。

## 已實作工具

- `auth.session.status`
- `auth.session.refresh`
- `auth.session.revoke`
- `core.env.check`
- `core.db.init`
- `core.cleanup.media_state`
- `core.path.prepare`
- `core.run.record`
- `core.sync_cursor.get`
- `core.sync_cursor.set`
- `download.http`
- `library.file.verify`
- `link.queue.upsert`
- `link.media.sync`
- `link.resolve.preview`（experimental）
- `link.resolve.to_media_item`（experimental）
- `media.file.upsert`
- `media.item.upsert`
- `media.item.filter_new`
- `media.item.set_status`
- `metadata.write`
- `storage.path.plan`
- `pixiv.auth.login`
- `pixiv.auth.status`
- `pixiv.auth.refresh`
- `pixiv.link.resolve`
- `pixiv.bookmarks.collect`
- `pixiv.bookmarks.sync`
- `pixiv.library.reconcile`
- `pixiv.comics.package`
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `instagram.saved.collect`
- `instagram.saved.sync`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links`（hidden stable）
- `telegram.inbox.sync_links`（hidden stable）
- `telegram.dialogs.list`
- `telegram.messages.collect`
- `telegram.media.download`
- `telegram.messages.sync`
- `reddit.auth.start`
- `reddit.auth.exchange`
- `reddit.auth.refresh`
- `reddit.auth.status`
- `reddit.saved.collect`
- `x.auth.start`
- `x.auth.exchange`
- `x.auth.refresh`
- `x.auth.status`
- `x.bookmarks.collect`

## 最新 Agent Core V1 狀態

- Agent Core V1 是 LLM-driven，不是 deterministic intent planner。被選定的模型會透過 strict JSON action protocol 決定 SKILL actions。
- 支援的 actions 是 `call_tool`、`final` 與 `ask_user`。
- 第一個 LLM backend 是 Ollama。預設本機設定為 `MEDIAGENT_LLM_PROVIDER=ollama`、`MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434`、`MEDIAGENT_OLLAMA_MODEL=qwen3:8b`。
- Built-in SKILL files 刻意使用英文撰寫，不預設使用者語言。由 LLM 理解並回應使用者的自然語言。
- Built-in skills 包含 `explicit_link_download`、`instagram_link_download`、`library_health_check`、`pixiv_bookmark_sync` 與 `telegram_inbox_download`。
- SKILL frontmatter 現在會透過 `supported_intents`、`unsupported_intents`、`requires_initial_tool_call` 與 `supports_unbounded` 暴露明確 intent boundaries。
- Agent Core 只會在 selected SKILL 明確記錄 full-sync mode 時支援 full-source 任務。Telegram inbox 與 Pixiv bookmark SKILL 現在透過明確的 `full_sync:true` tool inputs 支援「全部/完整/until-exhausted」請求；prompt 也會要求模型不要為這類任務捏造 count/page limits。
- Pixiv bookmark sync SKILL 文字現在明確說明 `limit` 代表 bookmark item count，不是 downloaded file count；多頁作品可能產生超過 item limit 的檔案數。
- Telegram inbox SKILL 文字現在描述 selected inbox workflow 邊界；沒有明確 selector 時會讓工具使用 `MEDIAGENT_TELEGRAM_INBOX_*`，並明確說明 V1 不檢查 inbox 是否存在或如何設定。
- `telegram.inbox.collect_links` 與 `telegram.inbox.sync_links` 現在可使用 `MEDIAGENT_TELEGRAM_INBOX_KEY` 搭配 `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME` 或 `MEDIAGENT_TELEGRAM_INBOX_CHAT` 作為 Agent Core、cron 與 systemd timer runs 的預設 inbox selector。
- `mediagent agent run "<task>"` 預設是 execute mode。`--dry-run` 是明確的預覽/開發模式；runner 會把 tool actions 正規化到全域 runtime mode，因此模型不能偷偷把 execute run 降級成 dry-run preview。
- LLM transport failures 會回傳 structured `llm_request_failed` agent errors，而不是 Python tracebacks。
- Skill selection 支援在任何 tool call 前回傳 `unsupported_task` / tool-gap outcome，前提是沒有 SKILL 明確符合任務。
- Agent Core 會移除使用者任務中沒有明確出現的 `library_root`、`target_dir` 與 `target_path`，並拒絕位於 configured write roots 外的 explicit destination paths。
- Long-running progress/logging 與 structured streaming 仍延後到 V2 或更後面。
- 目前本機 `qwen3:8b` model 已用 fake tools 驗證：英文 explicit-link 任務會選 `explicit_link_download`，繁體中文 inbox 任務會選 `telegram_inbox_download`，並能產生合法 `call_tool` action、遵守全域 run mode，且在 tool feedback 成功後回傳 `final`。
- `telegram_inbox_download` 現在會在 action tasks 要求初始 tool call。一次針對 `同步一次inbox的內容` 的 live Ollama dry-run 已選擇 inbox SKILL，並在不加 `--allow-experimental` 的情況下呼叫 hidden stable `telegram.inbox.sync_links`。
- 一次針對 `我目前有存在的 telegram inbox 嗎？` 的 live Ollama dry-run 已回傳 structured `unsupported_task`，且 `skill: null`、沒有 tool steps。

## 最新乾淨狀態 Agent Full-Source Verification

- 2026-08-05 UTC，active `/home/ion/projects/mediagent/mediagent-data/library` 與 `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` 已刪除並重建，沒有另外備份。`mediagent-data/credentials/` 內的 credentials 與 session files 已保留。
- `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"` 選擇 `telegram_inbox_download`，並呼叫 `telegram.inbox.sync_links`，輸入包含 `full_sync:true`、`store_cursor:false`，且沒有捏造 `limit` / `max_messages`。
- 第一次 Telegram run：收集/處理 31 個 links，27 個 resolved，4 個 skipped links，27 個 items queued/downloaded，下載 79 個 files，寫入 474005235 bytes，0 failed，0 partial。
- Telegram 重跑：處理 31 個 links，27 個 resolved，4 個 skipped links，27 個 skipped items，0 queued，0 files downloaded，0 bytes written。
- `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"` 選擇 `pixiv_bookmark_sync`，並呼叫 `pixiv.bookmarks.sync`，輸入包含 `full_sync:true`、`stop_on_known:false`、`store_cursor:false`，且沒有捏造 `limit` / `max_pages`。
- 第一次 Pixiv run：掃描 11 頁，collected/discovered 309 items，`collection_stop_reason:end_of_feed`，307 items queued/downloaded，2 skipped items，下載 1758 files，寫入 2946174301 bytes，0 failed，0 partial。
- Pixiv 重跑：掃描 11 頁，309 collected/discovered，309 skipped，0 queued，0 files downloaded，0 bytes written。
- `library.file.verify` 回報 1837 checked files、1837 valid、0 missing、0 corrupt、0 unknown。驗證後 active library 約 3.2G，active DB 約 2.8M。
- 驗證後 DB 摘要：downloaded media items 包含 Pixiv 309、Redgifs 10、Instagram 8、Reddit 3，以及少量 generic/source-host items。downloaded media files 包含 Pixiv 1800、Instagram 18、Redgifs 10、Reddit 5，以及 source-host/generic files。
- Telegram inbox runs 期間，Instagram resolver 仍會輸出大型 `JSONDecodeError in public_request` HTML diagnostics 到 stdout/stderr。Run 本身成功，但這仍然是 open summary-only/logging hardening task 的證據。

## 最新 systemd Timer MVP 狀態

- Telegram inbox sync 是第一個 timer-deploy 目標，但正式部署應透過 `mediagent agent run "<task>"` 觸發，而不是直接呼叫 deterministic tools。
- `.env.example` 現在已記錄 `MEDIAGENT_TELEGRAM_INBOX_KEY`，以及 `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME` 或 `MEDIAGENT_TELEGRAM_INBOX_CHAT` 作為 default inbox selector。
- 本機 `.env` 已為目前 live test 加入非 secret 的 Telegram inbox selector values：`MEDIAGENT_TELEGRAM_INBOX_KEY=mediagent_inbox` 與 numeric inbox chat id。
- `telegram.inbox.collect_links` 與 `telegram.inbox.sync_links` 在 default inbox env vars 已設定時，可不傳 explicit `chat`/`chats` input 執行。
- 2026-08-04 UTC 的 Telegram inbox execute live verification 使用 selector key `mediagent_inbox`，讀取既有 cursor `links:mediagent_inbox=34`，收集 3 個新 links，解析 3 個 links，下載 3 個 video files，寫入 40603018 bytes，並保存 cursor `links:mediagent_inbox=38`。
- 後續 dry-run 與針對 `幫我同步更新下載 telegram inbox 中的內容` 的 Agent Core execute run，在 cursor `38` 之後找到 0 個新 links 與 0 個 queued downloads，確認目前 inbox 的 rerun cursor continuation 正常。
- Pixiv bookmark sync 現在支援 timer-safe `stop_on_known` scanning。啟用後會從最新 bookmarks 開始掃描、最多掃到 bounded `max_pages`，並在某頁包含 known terminal media item 後停止。
- 在 `stop_on_known` mode 中，Pixiv sync 因 known item 停止時不會保存 API pagination cursor，避免被誤解成 Telegram-style continuation cursor。
- Agent Core 的 Pixiv recurring sync 現在會使用 `pixiv.bookmarks.sync` 搭配 `stop_on_known:true` 與 bounded `max_pages`，而不是自行發明預設 item `limit`。
- 2026-08-04 UTC 對 `幫我同步更新下載 pixiv bookmark 中的內容` 的 Pixiv Agent Core live dry-run 掃描 1 頁、收集 30 個已知 bookmark items、回報 `collection_stop_reason: known_item_seen`、queued 0 downloads，且寫入 0 files。
- 直接用替代 `MEDIAGENT_LIBRARY_DIR` 執行 Pixiv dry-run 也 queued 0 downloads，確認改變 library root 不會重置 DB-based media item dedupe。
- `deploy/systemd/user/` 現在包含本機 example user units、timers、JSON inputs，以及 Telegram inbox sync 與 Pixiv bookmark sync 的最小 runbook。
- 2026-08-05 UTC 的 clean-state user-systemd verification 已移除舊 library/live-test outputs，將舊 SQLite DB 備份到 `mediagent-data/backups/mediagent.sqlite3.20260805014915.bak`，初始化 schema v7，並保留 credential files。
- 先前精確 full-source 任務 `下載所有 telegram inbox 內所有可下載的媒體來源` 與 `下載 pixiv bookmark 所有可下載媒體來源` 的 Agent Core 失敗已在程式中修正。下一個驗證步驟是用乾淨 DB/library 重新執行這兩個自然語言任務。
- `systemctl --user start mediagent-telegram-inbox.service` 在乾淨 DB 上成功：第一次 run 收集 31 links、解析 27、跳過 4、下載 79 files、寫入 474005235 bytes，並保存 cursor `links:mediagent_inbox=39`；第二次 run 找到 0 個新 links 並下載 0 files。
- `systemctl --user start mediagent-pixiv-bookmarks.service` 在 Telegram run 後成功：第一次 run 掃描 1 頁、收集 30 bookmarks，因 Telegram 已先下載 1 個 explicit Pixiv item 而 skipped 1，下載 29 個 bookmark items、共 293 files、寫入 447025170 bytes；因為 stop-on-known，不保存 API pagination cursor；第二次 run queued 0 且 skipped 30。
- 驗證後 library 狀態：372 筆 downloaded file records、372 valid files、0 missing、0 corrupt、0 unknown。重建後 library 約 880M。

## 最新 Repair Mode 狀態

- Pixiv 現在有離線 `pixiv.library.reconcile` plan/apply 流程，可更新舊 work-type metadata、以原子搬移將既有漫畫原始頁面從 `photo` 或舊 `comic` 移到 `comic-pages`、同步搬移 sidecars、quarantine 已知 placeholder downloads、更新 DB paths；apply 必須傳入 `confirm:true`。
- 本機 development DB 的 plan 驗證找到 309 個 Pixiv items：26 comic、280 illustration、3 animation、17 unavailable placeholder records，blocked actions 為 0。本機 library 中有 245 個 legacy comic source files 已不在 DB 記錄路徑，因此這些應使用 opt-in repair，而不是原地搬移。
- `.trash` 內的檔案會視為 library 缺檔，永遠不自動搬回；`repair_missing_files:true` 會下載新副本到規劃路徑，並保留 `.trash` 原狀。
- Locked offline suite 通過 271 tests，包含 Pixiv work classification、unavailable placeholder rejection、reconciliation plan/apply/confirmation、comic-page/sidecar 原子搬移、placeholder quarantine、missing-file repair、Kavita one-shot/series CBZ metadata/layout、V1 quarantine migration、long-Unicode path safety、missing-source refusal、DB 記錄、重跑重用與 bookmark-sync packaging integration。

- `link.media.sync` 支援明確的 file-health-aware repair：`repair_missing_files: true`。
- `telegram.inbox.sync_links` 與 `telegram.messages.sync` 也暴露相同選項，作為既有 sync logic 上的 compatibility paths。
- 預設重跑仍保持保守：除非明確啟用 repair mode，否則 downloaded items 會被跳過。
- Repair mode 只會在必要 file records missing/corrupt/unhealthy，或 DB row 標記 `downloaded` 但 `local_path` 實體檔案不存在時，重新 queue downloaded items。
- Dry-run repair 使用相同 candidate selection，並回傳 `planned_downloads`，不寫檔也不修改 DB。
- Focused regression tests 已覆蓋 missing-file queue、healthy downloaded skip、default rerun 不變，以及 dry-run no-write planning。
- 2026-08-03 UTC 對 live DB 做 dry-run repair planning：從 14 筆 missing downloaded file records 推導出 12 個 unique source URLs；其中解析並規劃 8 個 repair downloads，分布於 4 個 providers，另有 4 個 links 在 resolution 階段 skip；寫入 0 bytes、下載 0 files，live DB 維持 675 筆 downloaded file records 與相同 14 筆 missing on disk。
- 2026-08-03 UTC 的 bounded non-dry repair 使用同一個 12-source scope，成功下載 8 個 repaired files，分布於 Danbooru、nhentai、Redgifs 與 rule34，共寫入 76755767 bytes，0 failed/partial items。
- Repair 後 `library.file.verify` 回報 675 筆 downloaded file records 中有 669 valid、6 missing、0 corrupt、0 unknown。剩餘 6 筆 missing rows 全部是 Reddit records，來自 4 個 unique source URLs；diagnostic dry-run 顯示這些 source 目前回 `requires_auth:login_required`。

## Telegram Inbox Message-Link Bridge 狀態

- `telegram.inbox.sync_links` 現在會在同一則 inbox message 中分流 external URLs 與 Telegram message links。External URLs 保留 shared resolver/download path；public 與 private `t.me` / `telegram.me` message links 則 delegate 到 Telegram 原生 collect/sync/download logic。
- Telegram 原生 items 會保留 inbox chat ID、source message ID/date、collector run ID 與 merged source provenance，且不持久化 inbox message text。
- Protected、missing、private、deleted 或其他 inaccessible linked messages 會回傳 per-link structured skips，不會中止整個 inbox run。
- `link.media.sync` 的 `retry_auth_skipped:true` 會重試舊 auth-dependent queue rows；`telegram.inbox.sync_links` 的同名選項只處理 Telegram-ingested rows。兩條路徑都會使用 lease claim，且需要明確 operator intent。
- Fake-client regressions 已覆蓋 public、private、inaccessible、protected、external 與 Telegram 混合，以及舊 auth-skip retry paths。本次實作沒有執行 live download。

## 已驗證

最後已知通過的驗證命令：

```bash
uv run --locked python -m unittest discover -s tests
uv lock --check
uv run --locked mediagent tools list --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect reddit.saved.collect --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools inspect pixiv.link.resolve --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
PIXIV_ACCESS_TOKEN= PIXIV_REFRESH_TOKEN= PIXIV_CREDENTIALS_FILE= uv run --locked mediagent tools run pixiv.link.resolve --input examples/tools/pixiv.link.resolve.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

最新本機完整測試狀態是 260 個測試通過。

Phase 16 Telegram inbox link resolver verification：

- `link.resolve.preview` 與 `link.resolve.to_media_item` 已實作為 experimental tools。`telegram.inbox.collect_links` 與 `telegram.inbox.sync_links` 是供 Agent SKILL 使用的 hidden stable tools。
- 一般 `mediagent tools list` 會隱藏 experimental tools 與低調的 hidden tools；`--include-experimental` 會顯示 experimental tools，而 hidden tools 仍可透過名稱呼叫。
- 一般 `mediagent tools run link.resolve.to_media_item` 會以 `experimental_tool_not_allowed` 拒絕執行。
- Top-level `mediagent --help` 不會暴露 hidden `experimental` command path。
- Tests 覆蓋 URL normalization、`normalized_url` uniqueness、userinfo rejection、malformed URL skip behavior、unsafe schemes、localhost/private IP rejection、unresolved host rejection、redirect limits、unsupported MIME rejection、`.mov` / `video/quicktime`、generic single-media HTML discovery、HEAD-forbidden HTML fallback、X age/login wall skip behavior、Imgur single-page resolution、ambiguous multi-media skip、Pixiv artwork-link `requires_auth`、duplicate Telegram URL queueing、origin-source storage layout、沒有 raw message text 的 Telegram provenance metadata、safe GET redirect revalidation、oversized GET body rejection，以及 MOV redirect-to-non-media rejection。
- 已執行隔離 live network smoke verification，將 `https://www.gstatic.com/webp/gallery/1.jpg` 解析並下載到 temporary scanner-friendly path：`gstatic_com/photo/2026/07/20260728__gstatic_com__url_3e125a8d7d4f4d6e6dea2830__p0.jpg`，44891 bytes，`image/jpeg`，checksum present，DB file record written，metadata sidecar written，temporary directory 已清除。
- Real Telegram auth status usable。Real Telegram inbox sync 已對本機 `inbox` channel 使用 integer chat selector `3779502941` 執行。Phase 16 live verification 已證明 nhentai/Danbooru 的 generic public HTML handling，以及 X login-wall skip。Reddit short links 現在由下方 Phase 17 處理。

Phase 17/18 Reddit explicit-link resolver verification：

- Fake-client tests 覆蓋 direct `i.redd.it`、direct `v.redd.it` MP4 video-only resolution、modern Reddit `shreddit-post` extraction、modern JS verification fallback 到 `old.reddit.com`、從 Reddit pages 擷取 explicit `v.redd.it/...DASH_*.mp4`、最高 DASH MP4 candidate selection、gallery skip behavior，以及 direct `v.redd.it/<id>` manifest skip behavior。
- Reddit MP4 resolutions 會映射到 `media_type: "video"`、`part: "v0"`、`library/reddit/video/...`，metadata 會在 audio muxing 尚未實作前標記 `audio_status: "not_merged"` / `mux_required: true`。
- Telegram inbox sync fake-client coverage 證明 Reddit MP4 links 會下載到 `library/reddit/video/...`，且 Telegram 只保留為 `ingested_from`。
- 2026-07-29 UTC 的 real Telegram auth status usable。
- Real Telegram inbox sync 對 chat selector `3779502941` 收集 5 個 external links，成功解析 4 個。X link 以 `requires_auth` / `login_or_age_gate` 跳過。
- Reddit share link 透過 `reddit_media_link` 與 `old.reddit.com` fallback 解析，下載 1 張 JPEG 到 `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/reddit/photo/2026/07/20260728__reddit__t3_1v8yi6w__p0.jpg`。
- 同一輪 live run 也下載 1 個 rule34 PNG、1 個 nhentai JPEG、1 個 Danbooru PNG 到 `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/<platform>/photo/2026/07/...`。
- 第二次執行去重成功：queued downloads 0、bytes written 0。
- `library.file.verify` 檢查 4 個 live-test files：4 valid、0 missing、0 corrupt、0 unknown。

Phase 19 link-first live verification：

- Stable core link tools `link.queue.upsert` 與 `link.media.sync` 已實作，且不用 experimental flags 即可 discovery。
- Public CLI entry point `mediagent link sync <url>` 會 delegate 到 `link.media.sync`，因此 non-Telegram link automation 會使用與 Telegram inbox compatibility wrapper 相同的 resolver/download/storage pipeline。
- Public CLI live smoke 已用已知 Redgifs URL 重跑 `mediagent link sync <url>`；它走同一條 pipeline 成功解析，跳過已下載項目，重複寫入 bytes 為 0。
- Queued `link.media.sync` runs 會用 `lease_owner` / `lease_expires_at` claim ready links、忽略其他 worker 尚未過期的 leases，並將 retryable failures 排成有 bounded `next_attempt_at` backoff 的 `deferred` records。
- Reddit explicit links 可以把單一 publicly visible external post URL delegate 回 resolver chain。Redgifs delegated results 會保留 Redgifs storage/layout，同時保存 Reddit upstream metadata。
- Telegram inbox compatibility wrapper `telegram.inbox.sync_links` 已於 2026-07-29 UTC 對 chat selector `3779502941` 做 live run，設定 `store_cursor:false`，output root 是 `/home/ion/projects/mediagent/mediagent-data/live-test-phase19/library`。
- 第一次執行收集 13 個 external links、解析 9 個、queue/download 6 個新 media items、以 structured reasons skip 4 個 links，且 failed/partial downloads 為 0。
- 先前 skipped 的 Reddit gallery link 已透過 `link.media.sync` 重跑；anonymous `old.reddit.com` public HTML 暴露 `preview.redd.it` candidates，preview fallback 已為 `t3_1v8boac` 下載 3 個 JPEG files。
- 最新一次 compatibility-wrapper 重跑收集 13 個 links、解析 12 個、skip 1 個預期中的 X/auth link，下載 2 個新的 Reddit-delegated Redgifs MP4 files，skip 10 個已知 items，且 failed/partial downloads 為 0。
- Phase 19 live-test library 內的下載內容包含 5 個 Redgifs MP4 videos 與 6 個 Reddit photo/GIF/JPEG files，位於 `library/redgifs/video/2026/07/...` 與 `library/reddit/photo/2026/07/...`，總計 211178527 bytes。
- 使用 platform selectors 做 `library.file.verify`，確認 Redgifs 5/5 valid、Reddit 6/6 valid；沒有 `.partial` 或 `.tmp` 殘留。

Phase 20 Instagram foundation verification：

- Stable `instagram.auth.status`、`instagram.auth.login`、`instagram.auth.ensure_session` 與 `instagram.link.resolve` 已實作、註冊到 default tool registry，並有 fake-client regression tests。
- 本機 Instagram saved session 位於 `/home/ion/projects/mediagent/mediagent-data/credentials/instagram_session.json`，權限是 `0600`，必須視為 credential。
- `instagram.link.resolve` 具備平台邊界：非 Instagram direct media 會以 `instagram_media_unsupported` 拒絕；out-of-root saved-session paths 會在 fake-client callbacks、real-client loads 或 network work 前回傳 `unsafe_credential_path`。
- 一個 Instagram post URL 代表整個貼文。Carousel/multi-resource posts 預設下載所有 resources；`img_index` 只保留為 source metadata，除非未來加入明確選項改變行為。
- Instagram CDN media URLs 是 signed/expiring runtime data，只在下載當下使用，不會持久化到 SQLite、sidecar metadata、logs、snapshots 或 tool output。
- 2026-07-30 UTC，直接用正式工具 live verification 解析 3/3 個使用者提供的 Instagram links，auth/rate-limit/checkpoint failures 為 0，並透過 `link.media.sync` 下載 9 個 files 到 `/home/ion/projects/mediagent/mediagent-data/library/instagram/`：7 個 JPEG photos 與 2 個 MP4 videos。
- 直接測試中的兩個 `/p/<shortcode>/` links 是 carousels：一個下載 3 個 JPEG resources，另一個下載 4 個 JPEG resources 與 1 個 MP4 resource。`/reel/<shortcode>/` link 下載 1 個 MP4 resource。
- 2026-07-30 UTC，Telegram inbox live verification 收集使用者貼上的 Instagram links，解析 3/3 個選定 Reel links，下載 3 個 MP4 files 到 `/home/ion/projects/mediagent/mediagent-data/library/instagram/video/2026/07/`；重跑後 3 個已下載項目全部 skip，duplicate bytes 為 0。
- Filesystem verification 顯示 JPEG/MP4 container types 有效，Instagram library root 底下沒有 `.partial` 或 `.tmp` files，且 mixed-carousel layout 正確：photo resources 位於 `instagram/photo/...`，video resources 位於 `instagram/video/...`。
- SQLite/sidecar checks 顯示直接與 inbox live tests 共留下 6 個 Instagram media items 與 12 個 media-file rows，且都使用穩定 Instagram post/resource URLs，而不是 signed CDN hosts。

Reddit foundation verification：

- `reddit.auth.start`、`reddit.auth.exchange`、`reddit.auth.refresh`、`reddit.auth.status` 已實作，且可透過 CLI discovery。
- `reddit.saved.collect` 已實作，且可透過 CLI discovery。
- Fake-client tests 覆蓋 auth URL generation、token exchange credential-file writing、refresh token preservation、status checks、redaction、generic user-agent rejection、unsafe credential paths、saved-listing normalization、cursor storage、dry-run no DB writes、unsafe collector DB paths、media-type filtering、saved comment skip 與 unsupported embed skip。
- `reddit.saved.collect` 只回傳 normalized media items，不寫入 `media_files`。
- Reddit auth/saved live verification 目前 deferred，除非明確恢復 auth-assisted account collection。

Cleanup/recovery foundation verification：

- `core.cleanup.media_state` 已覆蓋 dry-run planning，且不修改檔案或 DB。
- Apply mode 需要 `confirm: true`。
- Apply mode 會先 quarantine 既有 media files，再將 matching media items reset 為 `discovered`，並移除 matching media file rows。
- Credential paths 受到保護，不會以可執行 cleanup file paths 形式輸出。
- Unsafe quarantine paths 會被拒絕。

Telegram foundation verification：

- `telegram.auth.login` 已覆蓋 login-code start、透過 `password_ref` complete、無 Telegram config dry-run、缺少 code/hash validation、inline password rejection 與 secret redaction。
- `telegram.auth.status` 已覆蓋 missing config、unsafe session paths、usable fake sessions 與 secret redaction。
- `telegram.dialogs.list` 已覆蓋 filtered dialog listing，且不回傳 message/media content。
- `telegram.messages.collect` 已覆蓋 explicit chat selection、media type filtering、protected-content exclusion、album/grouped media normalization、private/public message-link parsing、curated link-inbox extraction、linked media resolution 與 scoped cursor storage。
- `telegram.media.download` 已覆蓋 safe writes、`.partial` finalization、checksum output、MIME validation 與 path safety。
- `telegram.media.download` 已覆蓋 malformed direct 與 nested `download_ref` input 會回傳 `telegram_download_missing_ref`，而不是 generic runtime error。
- `telegram.messages.sync` 已覆蓋 collect -> upsert -> status filter -> storage path plan -> Telegram-specific download -> file record -> item status update -> scoped cursor storage。
- `telegram.messages.sync` 已覆蓋 `.partial` 建立後 download cancellation：會記錄 failed file/item/run state，並移除 partial file。
- Telegram dry-run sync 搭配 fake client 已證明不會寫入 DB 或 library files。
- Telegram real login、auth status、curated link-inbox collection、小型媒體下載、長影片下載、layout placement、`library.file.verify` 與重跑去重已於 2026-07-24 UTC 完成 live verification。
- Telegram 真實下載現在會直接 stream 到 `.partial`，`timeout_seconds` 代表無進度 idle timeout，分塊計算 checksum，並用 atomic move finalization。

Deterministic Pixiv sync 驗證：

- `pixiv.bookmarks.sync` 已有 fake-client tests 覆蓋多檔成功下載、第二次執行跳過、dry-run 不寫檔/DB、partial failure、path safety、Pixiv `Referer`、scanner-friendly storage layout、file records、item status updates 與安全 cursor advancement。
- `pixiv.bookmarks.sync` 已有 photo-only sync 在 media-type filtering 後仍能儲存 cursor 的 regression coverage。
- `storage.path.plan` 已有平台專屬 library root 的 regression coverage。
- `storage.path.plan` 與 `pixiv.bookmarks.sync` 已有 `scanner-friendly-v2` platform layer，以及平台專屬 root 不重複 platform directory 的 regression coverage。
- 舊式 SQLite DB 若缺 `media_items.downloaded_at`，會在 `core.db.init` / tool initialization 時被 migration，讓 `media.item.set_status` 可以正常標記 downloaded。

Phase 21 Pixiv explicit-link implementation verification 已於 2026-08-03 UTC 完成：

- `pixiv.link.resolve` 已實作為 stable public tool，可解析單一 Pixiv artwork URL 或 `illust_id`。
- Core `pixiv_artwork_link` resolver 使用 Pixiv artwork detail，產生 normalized media candidates，支援多頁作品，保留 ugoira zip candidates，並回傳 structured Pixiv auth/rate-limit/unavailable errors。
- `link.media.sync` 可以直接消費 Pixiv artwork URLs，與既有 Pixiv bookmark-sync items/files 去重，並在不持久化 runtime headers 的前提下套用必要 Pixiv `Referer`。
- Fake-client tests 覆蓋 URL/id parsing、localized aliases、artwork detail request shape、多頁解析、ugoira zip candidates、missing credentials、unsafe credential-file paths、`pixiv.link.resolve` platform boundary、Pixiv `Referer` 與 bookmark-sync dedupe。
- CLI inspect 可用於 `pixiv.link.resolve` 與 `link.media.sync`。無 credential dry-run 會回傳 structured `pixiv_auth_missing_credentials` 與 `recommended_tool: "pixiv.auth.login"`。

Phase 21 Telegram inbox live verification 已於 2026-08-03 UTC 完成：

- 將自然語言任務「下載 inbox 中所有新的媒體」解析為使用 configured inbox chat 執行 `telegram.inbox.sync_links`，啟用 cursor storage，並走共享 link resolver/download pipeline。
- 第一輪 live run 收集 27 個 external links、considered 27、resolved 24、skipped 3、queued 9 個新 media items，下載 9 items / 22 files，寫入 134098941 bytes，partial 0、failed 0。
- Pixiv explicit links 透過 `pixiv_artwork_link` 解析：`112418327` 下載 4 個 files 到 `library/pixiv/photo/2023/10/...`；`137814756` 解析成 38 個已知 valid files，並被 dedupe 跳過。
- 第二輪 live run 收集 0 links、下載 0 files，確認 inbox path cursor advancement 生效。
- `library.file.verify` 檢查 675 個 DB file records：661 valid、14 missing、0 corrupt、0 unknown。14 個 missing rows 是舊的已記錄 link-first live-test files，不是本輪下載的新檔案。
- 本輪新下載的 22 個 artifact paths 全部存在。Pixiv persisted media metadata 沒有 runtime headers 或 tokens，Pixiv link resolution rows 也不再持久化 `runtime_headers` 或 runtime `download_context` keys。

Pixiv live verification 已於 2026-07-21 UTC 完成一次：

- `pixiv.auth.status` 對使用者提供的帳號回傳 usable session。
- `pixiv.bookmarks.collect` 回傳 30 個 public bookmark items。
- `download.http` 成功下載一張 JPEG bookmark 圖片到 `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test/143734851_p0.jpg`。
- 下載驗證：330936 bytes、`image/jpeg`、checksum `sha256:72c9988b5d32786423966ff7aae99166041b532571a83f7e4bda1adcd442e2fe`。

Phase 11 live storage verification 已於 2026-07-22 UTC 完成：

- 移除舊 Pixiv live 下載輸出：`/home/ion/projects/mediagent/mediagent-data/media`。
- 重設 `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` 內 Pixiv media item/file/cursor 狀態，保留 credentials。
- 重新收集 11 頁 Pixiv public bookmarks：309 個 raw bookmark items、306 個 photo items、1797 個 image files。
- 使用 `scanner-friendly-v1` 重新下載全部 1797 個 image files 到 `/home/ion/projects/mediagent/mediagent-data/library`。
- Public library 驗證：1797 個 media files、0 個 JSON sidecars、0 個 `.partial` files。
- SQLite 驗證：schema version `5`，306 個 Pixiv photo items 標記為 `downloaded`，1797 個 Pixiv media files 標記為 `downloaded`，全部有 `library_relative_path`、`storage_layout = scanner-friendly-v1` 與 `file_health = valid`。
- `library.file.verify` 檢查 1797 個檔案：1797 valid、0 missing、0 corrupt、0 unknown。
- 第二次去重檢查：306 個 Pixiv photo items 會被跳過，0 個會再次下載。
- 接著用已提交的 `pixiv.bookmarks.sync` 非 dry-run 跑 `max_pages = 20` 與 `media_types = ["photo"]`；它掃描 11 頁、跳過全部 306 個已下載 photo items、queued downloads 為 0，並在 SQLite 記錄 1 筆 successful tool run。

Pixiv live artifacts 已於 2026-07-24 UTC 清理：

- 移除舊的單檔 Pixiv smoke output：`/home/ion/projects/mediagent/mediagent-data/pixiv/live-test`。
- 移除舊的完整 Pixiv live library output：`/home/ion/projects/mediagent/mediagent-data/library`。
- 移除空的 `/home/ion/projects/mediagent/mediagent-data/pixiv` 目錄。
- 將 `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` 內 Pixiv `media_items`、`media_files`、`sync_cursors` 重設為 0。
- 保留 `/home/ion/projects/mediagent/mediagent-data/credentials/pixiv-oauth.json`。

Phase 13 Telegram + Pixiv layout live verification 已於 2026-07-24 UTC 執行：

- Telegram `telegram.auth.login` 已用使用者提供的 app code 完成，`telegram.auth.status` 回報 usable session。
- Telegram `telegram.dialogs.list` 找到使用者控制的 collection channel。
- Telegram `telegram.messages.collect` 將 3 個 curated message links 解析成 3 個 media items：一個長 private video、一個小型 video/GIF-style file、一張 photo。
- 長 private Telegram video 起初暴露真實下載 buffering 問題並被標為 `failed`；stream-safe download support 完成後已成功重試。
- Telegram direct link sync 成功下載 2 個小型 media files 到 shared scanner-friendly library：
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov`
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg`
- 重跑同一個 Telegram direct link sync 時 queued 0 downloads，並 skip 2 個已下載 items。
- Telegram stream-safe 長影片 sync 下載 `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4`，寫入 660481192 bytes，失敗 0 files。
- 重跑同一個長影片 sync 時 queued 0 downloads、skip 1 個已下載 item、寫入 0 bytes。
- Bounded Pixiv sync 使用 `max_pages = 4`、`limit = 100`、`media_types = ["photo"]`，收集 120 個 raw bookmark items，發現 100 個 photo targets，下載 100 個 items / 624 個 files，寫入 1131771564 bytes，失敗 0 files。
- Pixiv files 使用 `scanner-friendly-v2`，落在 `/home/ion/projects/mediagent/mediagent-data/library/pixiv/photo/2026/...`。
- `library.file.verify` 檢查 Telegram 與 Pixiv 共 627 個 files：627 valid、0 missing、0 corrupt、0 unknown。
- Filesystem verification 顯示 624 個 Pixiv files、3 個 Telegram files、0 個 `.partial` files。
- 使用相同 bounded input 的 Pixiv 第二次 dry-run queued 0 downloads，並 skip 100 個已下載 items。

## Instagram 收藏媒體 Foundation

Instagram 收藏媒體 foundation 與 bounded 本機 live verification 已於 2026-08-11 UTC 完成：

- 新增 sequential 單頁 saved-feed client，保留 opaque pagination，並提供 structured session、checkpoint 與 rate-limit failures。
- 新增 `instagram.saved.collect` 與 `instagram.saved.sync`；支援 photo、Reel/video、carousel 的 whole-post normalization、runtime-only signed URLs、共用 scanner-friendly storage/download/status/repair、safe cursor advancement 與 sidecar。
- 註冊 tools，新增 bounded/recurring/full JSON examples，以及英文 `instagram_saved_sync` Agent SKILL；saved-feed intent 與 explicit Instagram links 保持分離，且「all saved media」不會被加上虛構限制。
- Review hardening 會拒絕 configured write roots 之外的 explicit DB paths，也會避免 page limit 截斷後透過 opaque cursor 跳過未回傳貼文。
- Locked offline suite 通過 260 tests，涵蓋 pagination、dedupe、carousel resources、CLI example inputs、partial failure、cursor safety、dry-run isolation、auth/rate-limit errors、download、retry、repair 與 Agent intent boundaries。
- Local-only bounded live run 讀取一個 saved-feed page，並同步前 2 個貼文；兩者都是 Reels/videos，共成功下載 2 個檔案、16,746,907 bytes。
- 第二次相同執行 queued/downloaded 都是 0，跳過 2 個健康 items；`library.file.verify` 回報 2 valid、0 missing、0 corrupt。
- SQLite 檢查發現 0 個持久化 runtime CDN/session/auth markers。專用本機 live-test DB、library 與暫存輸出已於測試後移除。Bounded sample 未包含 carousel，因此真實 carousel 下載仍由離線測試覆蓋。

## 尚未實作或尚未驗證

- Workflow V1 runner
- 內建 scheduler
- cron examples
- 真實 X OAuth 帳號現場驗證
- 真實 Reddit OAuth / saved-collection 現場驗證，目前 deferred，除非明確恢復 auth-assisted collection
- Reddit audio muxing、DASH/HLS manifest handling 與複雜 multi-file `v.redd.it` support
- `reddit.saved.sync`，目前 deferred，除非明確恢復 auth-assisted collection
- Pixiv localhost callback server
- Instagram stories、profile scraping、messaging、posting、comments、likes、follows，以及 saved-feed boundary 以外的廣泛 account collection
- Instagram session status TTL，以及 checkpoint/2FA/rate-limit/thumbnail-only Reel cases 的額外 edge-case fixtures
- visual workflow editor

## 下一個建議任務

依照 `TODO.md` 進行 systemd timer hardening：deployment environment validation、overlapping-run protection、精簡 journal output、source-aware Pixiv stop-on-known，以及一致的 timer-safe failure policy。Reddit OAuth/saved collection 與 X live auth verification 維持 deferred legacy/advanced paths。
