# Mediagent 目前狀態

## 已完成

- Package layout 位於 `src/mediagent/`。
- `main.py` 是薄啟動入口。
- `pyproject.toml` 已設定 console script：`mediagent = mediagent.cli:main`。
- Tool contract 位於 `src/mediagent/core/tooling.py`。
- Tool registry 位於 `src/mediagent/tools/defaults.py`。
- CLI bridge 位於 `src/mediagent/cli.py`。
- SQLite 初始化位於 `src/mediagent/core/db.py`，目前 schema version 是 `7`，並支援舊 media item/file table 與 stable `link_queue` lifecycle/retry/provenance fields 的 idempotent migration。
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
- Deterministic sync helpers 位於 `src/mediagent/core/sync.py`。
- Universal storage planning 位於 `src/mediagent/core/storage.py`。
- 預設 shared-root storage layout 是 `scanner-friendly-v2`：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`。
- 已透過 `MEDIAGENT_<PLATFORM>_LIBRARY_DIR` 支援平台專屬 library root，例如 `MEDIAGENT_PIXIV_LIBRARY_DIR`。
- 平台專屬 root 會被視為已經屬於該平台，因此預設會省略額外 platform directory。
- Pixiv bookmark sync 已支援 collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update。
- Pixiv bookmark sync 使用 `media_types` filtering 時會存入 scoped cursor，例如 `bookmarks:public:photo`。
- Telegram message sync 會在 durable processing 成功後儲存 per-source scoped cursors，例如 `messages:saved_messages:photo-video`。
- Undocumented Telegram inbox link resolver support 已放在 experimental boundaries 後方。它把 Telegram 視為 ingest provenance，並使用解析後的 `origin_source` 作為 media item 與 storage layout 的平台。
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
- `pixiv.bookmarks.collect`
- `pixiv.bookmarks.sync`
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links`（experimental）
- `telegram.inbox.sync_links`（experimental）
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
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

最新本機完整測試狀態是 187 個測試通過。

Phase 16 Telegram inbox link resolver verification：

- `link.resolve.preview`、`link.resolve.to_media_item`、`telegram.inbox.collect_links` 與 `telegram.inbox.sync_links` 已實作為 experimental tools。
- 一般 `mediagent tools list` 會隱藏 experimental tools；`--include-experimental` 才會顯示。
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
- Telegram 真實下載現在會直接 stream 到 `.partial`，在 Telethon download call 外層 enforce `timeout_seconds`，分塊計算 checksum，並用 atomic move finalization。

Deterministic Pixiv sync 驗證：

- `pixiv.bookmarks.sync` 已有 fake-client tests 覆蓋多檔成功下載、第二次執行跳過、dry-run 不寫檔/DB、partial failure、path safety、Pixiv `Referer`、scanner-friendly storage layout、file records、item status updates 與安全 cursor advancement。
- `pixiv.bookmarks.sync` 已有 photo-only sync 在 media-type filtering 後仍能儲存 cursor 的 regression coverage。
- `storage.path.plan` 已有平台專屬 library root 的 regression coverage。
- `storage.path.plan` 與 `pixiv.bookmarks.sync` 已有 `scanner-friendly-v2` platform layer，以及平台專屬 root 不重複 platform directory 的 regression coverage。
- 舊式 SQLite DB 若缺 `media_items.downloaded_at`，會在 `core.db.init` / tool initialization 時被 migration，讓 `media.item.set_status` 可以正常標記 downloaded。

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

## 尚未實作或尚未驗證

- Workflow V1 runner
- 內建 scheduler
- cron examples
- 真實 X OAuth 帳號現場驗證
- 真實 Reddit OAuth / saved-collection 現場驗證，目前 deferred，除非明確恢復 auth-assisted collection
- Reddit audio muxing、DASH/HLS manifest handling 與複雜 multi-file `v.redd.it` support
- `reddit.saved.sync`，目前 deferred，除非明確恢復 auth-assisted collection
- Pixiv localhost callback server
- Instagram feed、saved-post、stories、profile scraping、messaging、posting、comments、likes、follows 與廣泛 account collection
- Instagram session status TTL，以及 checkpoint/2FA/rate-limit/thumbnail-only Reel cases 的額外 edge-case fixtures
- LLM Agent Core
- visual workflow editor

## 下一個建議任務

Phase 20 Instagram explicit-link foundation 已完成。下一個實作焦點是 Phase 21 Pixiv explicit artwork-link resolution，並且要走共享 link-first pipeline。

Reddit OAuth/saved collection 與 X live auth verification 都視為 deferred legacy/advanced paths。除非使用者明確要求，否則不要在 link-first provider-adapter contract 通過至少一個更多 provider adapter 或多次 cron-style runs 保持穩定前開始 Workflow V1 或 Agent Core。
