# Mediagent 焦點 TODO

這份文件只追蹤接下來要做的實作任務。詳細狀態、驗證歷史與已解決問題請放在 `STATE.md`、`ISSUES.md`、`RUNBOOK.md`。

更新這份 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 已完成基線

以下基礎已經完整到足以作為目前開發基線：

- `src/mediagent/` Python package layout
- Tool contract、registry、CLI bridge
- env、DB、paths、run records、media items、media files、HTTP download、metadata writing、sync cursors、storage path planning、library verification 等底層工具
- 具備 redaction 與 credential-file 邊界的 credential/auth foundation
- 通用 scanner-friendly storage layout：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`
- X auth 與 bookmark collection fixture/fake-client 測試，仍待 live verification
- Pixiv auth、bookmark collection、deterministic `pixiv.bookmarks.sync`
- Pixiv bounded live layout verification 已用 100 個 bookmark items / 624 個 photo files 驗證 `scanner-friendly-v2`
- Telegram media-source foundation：explicit local `telegram.auth.login`、Telethon-backed user-session config、auth status、dialog listing、message/link-inbox collection、Telegram-specific media download、deterministic message sync、CLI examples、fake-client tests
- Telegram real login、curated link-inbox collection、小型 photo/video download 與重跑去重已完成 live verification
- Telegram stream-safe real downloads 與一小時影片 live verification 已完成目前階段目標
- Phase 16 undocumented Telegram inbox link resolver foundation 已放在 experimental boundaries 後方，包含 URL queueing、URL safety、direct media / generic single-media HTML / Imgur single-page / Pixiv artwork-link resolver behavior、origin-source storage metadata、link-safe download 與 regression tests
- Phase 17/18 Reddit explicit-link resolver foundation 已支援輕 credential 的單一媒體連結：direct `i.redd.it` images、direct `v.redd.it` MP4 video-only files、Reddit post/share links、bounded anonymous HTML、搭配靜態 `over18=1` 的 `old.reddit.com` fallback、unsupported gallery/manifest cases 的 structured skips、Reddit metadata preservation、Telegram inbox live verification、dedupe verification 與 file verification
- Conservative cleanup/recovery foundation 已透過 `core.cleanup.media_state` 建立，支援 dry-run planning、manifest output、explicit apply confirmation、quarantine-before-DB-reset behavior 與 credential path protection
- Reddit foundation 已建立：OAuth config/auth tools、saved-listing collector、支援 image/gallery/video/direct media shapes 的 media parser、CLI examples、credential path safety、cursor path safety 與 fake-client tests

不要在這裡展開已完成 phase 的細節；只有當完成內容會直接影響未來工作時，才加入短註記。

## 目前焦點：Phase 18 Link Resolver Hardening And Multi-File Readiness

目標：在第一次 Reddit live test 成功後，硬化 link-first resolver path，但不把它擴張成無限制 crawler。

目前 resolver path 已證明的形狀：

```text
explicit user-provided link
-> URL normalization and uniqueness check
-> resolver registry
-> normalized media item
-> existing sync/download/storage pipeline
```

### 18A. 補齊 Reddit 單一媒體覆蓋

- [ ] 補上 `redd.it/<post_id>` short URLs 安全轉址到 post pages 的 fake-client coverage。
- [ ] 補上 direct `old.reddit.com` input links 的 fake-client coverage。
- [ ] 補上 `shreddit-screenview-data` JSON extraction 的 fake-client coverage。
- [ ] 補測試證明存在清楚 original `i.redd.it` image 時，Reddit preview/thumbnail URLs 會被忽略。
- [ ] 補上 no-media pages、blocked pages、deleted/removed pages、login-required pages、quarantined pages 與 ambiguous multi-image pages 的 structured skip tests。
- [x] 補上 Telegram inbox sync fake-client coverage，證明 Reddit links 會下載到 `library/reddit/...`，且 Telegram 只保留為 `ingested_from`。
- [x] 在 generic direct-media fallback 前加入 direct `v.redd.it` MP4 support。
- [x] 加入 Reddit post/legacy-page 對 explicit `v.redd.it/...DASH_*.mp4` candidates 的解析。
- [x] 補測試證明 Reddit MP4 links 會映射為 `video`、`v0` 與 `library/reddit/video/...`。

### 18B. 準備 Multi-File Resolver Contract

- [ ] 讓目前 public result shape 維持和單一 resolved media item 相容。
- [ ] 先草擬未來可從一個 input link 回傳多個 files 的 internal resolver result shape。
- [ ] 將未來 multi-file result 映射到既有 media item `metadata.files` 格式。
- [ ] 在 multi-file shape 有 unit tests 前，不啟用 Reddit galleries 或 multi-stream video muxing。
- [ ] Storage layout 保持不變：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`。

### 18C. 下一批 Provider Link Resolvers

- [ ] 規劃 explicit Pixiv artwork-link resolver，讓它重用既有 Pixiv auth 與 artwork parsing，而不是依賴 bookmark access。
- [ ] 將 explicit X post-link resolver 與 X bookmark APIs 分開規劃，並清楚處理 login walls 與 anti-bot limits。
- [ ] Generic HTML resolver 維持保守：只處理單一明確 public media file，不執行 JavaScript、不抓 credentials、不保存 page dumps。

### 18D. Reddit Deferred Scope

- [ ] Reddit OAuth live verification 在 credentials 尚不可用時維持 pending。
- [ ] 等 explicit-link behavior 與 collector output shape 穩定後，再考慮 `reddit.saved.sync`。
- [ ] 等 resolver contract 能乾淨支援一個 link 產生多個 files 後，再做 Reddit galleries。
- [ ] 等 ffmpeg/dependency strategy 與 multi-file resolver contract 穩定後，再做 Reddit audio muxing、DASH/HLS manifest handling，以及完整 multi-file `v.redd.it` support。
- [ ] 不加入 Reddit posting、commenting、voting、save/unsave、moderation 或 chat-management features。

### 18E. Reddit Video Mux 與 Managed FFmpeg 規劃

- [ ] 決定 Mediagent 是否要管理 project-local ffmpeg binary、接受明確 `MEDIAGENT_FFMPEG_PATH`，或兩者都支援。
- [ ] 加入 tool-safe ffmpeg capability check，回報版本與支援 codec，但不修改 PATH。
- [ ] 規劃一個 media item 搭配多個 source files，讓 Reddit video/audio tracks 可以分開下載後 mux 成單一 final file。
- [ ] 在 muxing 尚未可用前，保留 direct single MP4 video-only downloads support。
- [ ] 補測試證明 audio-only MP4 candidates 不會被存成使用者可瀏覽的 video files。

## Side Decisions

- [ ] X live OAuth verification 仍待處理，因為 API access 可能需要付費 credits。
- [ ] 等 Phase 18 hardening 完成後，規劃 X 與 Pixiv explicit-link resolvers，讓 inbox automation 可以從明確 post/artwork links 下載，不依賴 bookmark/feed access。
- [ ] 討論 Pixiv 是否需要 bookmarks 以外的 source tools，例如 following-user works 或 explicit artwork IDs。
- [ ] 在使用者明確決定要正式公開前，Telegram link resolver behavior 保持 undocumented。
- [ ] 在 Pixiv、Telegram、Reddit、X 邊界穩定前，Instagram 保持 deferred。

## 設計決策：B 方案 Hidden Telegram Link Resolver

目標：把 Telegram inbox 外部連結功能做成 bounded resolver pipeline，而不是 domain allowlist，也不是無限制 crawler。

這樣可以支援使用者整理過的 Telegram inbox，同時把資安與維護成本限制在可控範圍內。

目標形狀：

```text
Telegram inbox message
-> external URL extraction
-> URL normalization and uniqueness check
-> resolver chain
-> normalized media item
-> existing sync/download/storage pipeline
```

實作邊界：

- Telegram 只作為 ingest source 與 provenance；storage platform 由解析後的 `origin_source` 決定。
- `link_queue.normalized_url` 是唯一性 key，避免重複貼上的連結產生重複工作。
- Resolver 行為在明確決定正式公開前，必須維持在 experimental/undocumented tool boundaries 後方。
- Public HTML pages 不要求 domain allowlist。
- 第一版 resolver 只支援 public HTTPS direct media URLs、bounded public HTML parsing 找出的單一明確媒體目標，以及必要時才加入的少量 explicit provider adapters。
- Direct media 支援有界的 image/video MIME types，包含 `.mov` / `video/quicktime`。
- Public HTML pages 只有在 resolver 能 deterministic 找出唯一可下載媒體檔時才支援。
- Login-required pages、multi-media pages、JavaScript-rendered pages、超出 safe redirects 的 URL shortener expansion，以及 unknown providers 都要用 structured reasons 跳過。
- 實際 GET download 必須重新執行 URL safety、redirect validation、MIME validation 與 byte limits，不可只信任 preview result。
- Metadata 應保存 resolved source、原始 Telegram provenance、normalized URL、checksum、MIME type 與 file size，但不得保存 raw message text、credentials、cookies 或 page dumps。

測試目標：

- URL normalization 與 `normalized_url` uniqueness。
- Userinfo、malformed URLs、unsafe schemes、localhost/private IPs、unresolved hosts、redirect limits、unsupported MIME、oversized responses，以及 redirect-to-non-media rejection。
- Direct media 與 `.mov` handling。
- 從 `og:image`、`og:video`、`twitter:image`、`twitter:player:stream`、`<video>`、`<source>`、`<a>` 與 page data 內嵌 direct media URLs 做 generic HTML media discovery。
- Single-media provider resolution 與 multi-media provider skip behavior。
- Telegram inbox collection 不保存 raw message text。
- Dry-run 不寫檔、不修改 DB、不建立 media-file。
- Hidden experimental boundary：normal tool listing 會隱藏這些工具，normal inspect/run 會拒絕，top-level help 不暴露 hidden command path。

## 本階段已完成：Phase 16 Undocumented Telegram Inbox Link Resolver

- [x] 新增 hidden experimental tool boundaries 與 Telegram inbox link sync CLI routing。
- [x] 新增 URL normalization、unique `link_queue` storage 與 SQLite schema version 6。
- [x] 新增 direct media URLs、public single-image Imgur pages 與 Pixiv artwork-link identification 的 safe resolver behavior。
- [x] 新增 schemes、userinfo、malformed URLs、DNS/private IPs、redirects、MIME types、`.mov` 與 max media size 的嚴格 URL safety。
- [x] Phase 16 下載改用 link-safe GET download，不使用 generic downloader。
- [x] 保留 `origin_source` 作為 storage platform，Telegram 只作為 ingest provenance。
- [x] 補上 `ISSUES.md` 內所有 Phase 16 acceptance criteria 與 security issues 的 regression tests。
- [x] 已執行隔離 live network smoke verification；真實 Telegram inbox sync 也已執行，但當時 inbox 內沒有外部 URL 可下載。

## 本階段已完成：Cleanup / Recovery Foundation

- [x] 新增 `core.cleanup.media_state` 作為保守的 live-test cleanup/recovery tool。
- [x] `mode: "plan"` 可在不修改檔案或 SQLite 的情況下預覽 matching media items/files。
- [x] `mode: "apply"` 需要 `confirm: true`。
- [x] Apply mode 會先將既有 media files 移到 quarantine，再 reset matching DB state。
- [x] 工具要求 platform selector，並支援 optional `remote_id` 與 `status` selectors。
- [x] Credential paths 受到保護，不會被列為可執行 cleanup files。
- [x] Tests 覆蓋 dry-run no mutation、selector validation、credential protection、quarantine-before-reset、confirmation 與 path safety。

## Later：RuleSpec Policy Layer

等 Pixiv 與 Telegram deterministic platform sync behavior 維持穩定後再做。

目標：讓使用者描述 source selection 與 filtering rules，而不是把每個平台的 curation model 寫死。

Proposed flow：

```text
platform collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM 或 Agent Core 未來可以協助把自然語言意圖轉換成 RuleSpec，但 scheduled daemon runs 應該執行已儲存的 deterministic rules，而不是每次都要求 LLM 即興判斷。

## Later：Workflow、Scheduling、Agentic Composition

- [ ] 等 Pixiv 與 Telegram 的 deterministic sync behavior 都穩定後，再加入 YAML Workflow V1。
- [ ] 在 headless workflows 可靠之前，scheduler 先維持 cron/systemd。
- [ ] 加入 tool discovery 與 safe usage 的 SKILL 文件。
- [ ] 加入呼叫同一個 registry 的 Agent Core，不直接呼叫 platform internals。
- [ ] 等 deterministic scheduling 可靠後，再加入 agentic scheduler。

## 本階段已完成：Telegram 大型媒體下載強化

- [x] 真實 Telethon 下載會直接 stream 到規劃好的 `.partial` 檔，而不是回傳 `bytes`。
- [x] Tool-level finalization 會驗證完成的 `.partial`、分塊計算 checksum，接著 atomic move 到 final path。
- [x] 真實 Telegram download call 外層會 enforce `timeout_seconds`。
- [x] Fake-client tests 已覆蓋串流寫入 `.partial` 與 streaming 失敗時清除 partial。
- [x] 一小時 Telegram 影片已下載到 `${MEDIAGENT_DATA_DIR}/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4`。
- [x] 重跑同一個 Telegram sync 會跳過已完成長影片。
- [x] `library.file.verify` 檢查 627 files，627 valid。

## 目前明確不做

- [ ] Headless Workflow V1 有用之前，不做 visual workflow editor。
- [ ] Bottom/platform tool contracts 穩定之前，不做 LLM Agent Core。
- [ ] Cron-compatible execution 可靠之前，不做 built-in scheduler。
- [ ] 不做 media browsing、library management、sharing、forwarding、reposting、chat-management features。
