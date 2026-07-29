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
- Phase 19 第一版 stable link layer 已建立：schema-v7 `link_queue` lifecycle fields、active claim/lease 與 retry scheduling、source provenance merge、stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>` CLI entry point、Redgifs direct/watch resolver foundation、Reddit static/preview gallery support、Reddit external-provider delegation、simple static groups 的多檔 candidates、resolver header persistence sanitizer 與 regression tests
- Conservative cleanup/recovery foundation 已透過 `core.cleanup.media_state` 建立，支援 dry-run planning、manifest output、explicit apply confirmation、quarantine-before-DB-reset behavior 與 credential path protection
- Phase 19 live verification 已完成 public `mediagent link sync <url>` entry point、Redgifs direct/watch links、Reddit-to-Redgifs delegation、anonymous Reddit single-file photo/GIF links，以及透過 preview fallback 解析的一個 Reddit multi-image gallery。最新一次 compatibility wrapper 重跑解析 13 個 inbox links 中的 12 個，skip 1 個預期中的 X/auth link，下載 2 個新的 delegated Redgifs MP4 files，且 failed/partial downloads 為 0。Phase19 live-test library 目前有 5 個 Redgifs MP4 files 與 6 個 Reddit photo/GIF/JPEG files。
- Reddit foundation 已建立：OAuth config/auth tools、saved-listing collector、支援 image/gallery/video/direct media shapes 的 media parser、CLI examples、credential path safety、cursor path safety 與 fake-client tests。除非使用者明確恢復 auth-assisted account collection，否則把它視為 deferred legacy/advanced capability

不要在這裡展開已完成 phase 的細節；只有當完成內容會直接影響未來工作時，才加入短註記。

## 已完成焦點：Phase 19 Link-First Resolver Hardening

Phase 19 的 operational slice 已完成。本節內尚未勾選的項目屬於 post-19 promotion、future provider planning，或 deferred policy/test follow-ups；它們不是目前 stable link-first baseline 的阻塞項。

目標：將使用者明確提供的連結改為 Mediagent 的主要產品路徑。

舊的 auth-first 路徑不再是主方向：

```text
auth
-> account bookmarks / saved items / feeds
-> automatic discovery
-> download
```

新的主要路徑是：

```text
explicit URL source
-> URL normalization and uniqueness check
-> link queue lifecycle control
-> safe resolver chain
-> normalized media candidates
-> deterministic candidate selection
-> existing media/download/storage pipeline
```

Pixiv bookmark sync 因為已經完成且實用，所以保留為例外能力。新的平台工作應先從 explicit-link resolution 開始，再考慮帳號 collection。

### 19A. 公開 Link Tool Surface

- [x] 將目前 hidden link resolver 從 Telegram-only secret feature 提升為 first-class core link workflow。
- [x] 在穩定前讓 CLI surface 維持保守；實作位置在 core link tools，而不是 Telegram-only code。
- [x] 新增 `link.queue.upsert`，用於 URL intake 與 normalized-URL dedupe。
- [x] 加入 schema-v7 queue fields，支援 lifecycle、retry metadata、source provenance 與 future leases。
- [x] 將 permanent skips 與 retryable failures 分開；login wall、unsupported domain、unsafe URL、ambiguous page 不應無限制重試。
- [x] 同一 URL 從 CLI、Telegram inbox、workflow、未來 Agent/SKILL 等多個來源提交時，會合併 source provenance。
- [x] 新增 `link.media.sync` 作為 deterministic orchestration tool：讀取 queued URLs、解析、upsert media items、filter known items、規劃 storage paths、下載 files、寫 metadata、記錄 file state。
- [x] 允許 CLI JSON、queued `link_id` records、Telegram inbox links、未來 workflow steps 與未來 Agent/SKILL calls 作為 URL 輸入。
- [x] Dry-run mode 不得寫檔、修改 DB state 或建立 media-file records。
- [x] 目前 single-worker path 的 JSON output 已穩定到足以供 cron、workflows 與未來 agents 使用。
- [x] 啟用 queue claim/lease behavior，避免 concurrent cron 或 daemon runs 處理同一個 queued link。
- [x] 加入以 `next_attempt_at`、bounded attempts 與 retryable skip handling 為基礎的 retry scheduling。
- [ ] 等 public preview/debug API 確定後，再 promote 或替換 `link.resolve.preview` 與 `link.resolve.to_media_item`。

### 19B. Resolver Contract

- [x] 定義 `MediaCandidate`，包含 JSON-compatible fields：`url`、`media_type`、`mime_type`、`extension`、`size_bytes`、`source`、`quality_rank`、`file_index`、`content_identity`、`persistable_headers`、`download_context_ref`、`details`。
- [x] 將 `persistable_headers` 視為 allowlisted、non-secret set。需要 public media delivery 時可保存 `Referer`；`Authorization`、`Cookie`、bearer tokens、signed URL secrets、session headers、CSRF headers 必須只存在 runtime，不得寫入 SQLite、sidecar metadata、log 或 snapshot。
- [x] 在持久化 link resolution state 前移除 candidate headers 中帶 credential 的內容。
- [x] 定義 `LinkResolution`，包含 `status`、`skip_reason`、`original_url`、`normalized_url`、`canonical_url`、`aliases`、`final_url`、`origin_source`、`resolver_chain`、`auth_used`、`media_candidates`、`selected_candidate`、`warnings`、`details`。
- [x] Resolver 在可行時必須輸出 canonical source identity，例如 `platform + remote_id`、provider media id、canonical post URL 或 direct content URL。
- [x] Simple static file groups 已支援多個 internal candidates。
- [x] 定義 simple static file groups 的 multi-candidate group 語意：group id、required files、optional files、candidate ordering、partial-success status、`metadata.files` mapping。
- [x] 使用 structured skip reasons，例如 `requires_auth`、`login_wall`、`unsupported_domain`、`unsupported_media_type`、`unsupported_multi_media`、`javascript_required`、`blocked`、`unsafe_url`、`too_large`、`ambiguous_candidates`。
- [x] 保留足夠 metadata 供除錯與索引使用，但不得保存 raw HTML dumps、raw Telegram message text、cookies、tokens 或帶有 credentials 的 headers。
- [x] Storage layout 保持不變：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`。

### 19C. Canonical Dedupe

- [x] 將 `link_queue.normalized_url` 視為第一層 intake dedupe，而不是最終 media identity。
- [x] 新增第一版 link alias 策略，讓 `redd.it/<id>`、`reddit.com/r/.../comments/<id>/...`、`old.reddit.com/...`、provider watch URL、direct media URL 可以指向同一個 queued link 或 resolved source。
- [x] 使用 resolver output 在 link aliases 與 `platform + remote_id` media item 層 dedupe；known file records 與 checksums 可避免既有 target 重複下載。
- [x] 保存所有已知 source URLs 作為 provenance，但不得建立重複下載工作。
- [x] Rerun 可以更新既有 link 的 resolution metadata，但不得重置已完成的 media-file state。

### 19D. Generic Resolver

- [x] 在抓取完整 HTML 前，先解析 direct public media URLs。
- [x] 支援有界的 image/video/audio MIME checks，包含 `.mov` / `video/quicktime`。
- [x] 使用 HEAD、range GET 或 bounded GET fallback 重新驗證 redirects、final URL、MIME type 與 size。
- [x] 從 bounded public HTML 解析 `og:image`、`og:video`、`twitter:image`、`twitter:player:stream`、`<video>`、`<source>`、direct media anchors，以及簡單 JSON-LD/media URL fields。
- [x] 對 candidates 評分，讓明顯 original/full-size media 優先於 thumbnails、icons、avatars 與 decorative images。
- [x] 只有在能 deterministic 選出單一明確 media candidate 時才下載。
- [x] 當頁面暴露多個合理 media files 時，回傳 `ambiguous_candidates` 或 `unsupported_multi_media`，不直接下載。
- [x] 不執行 JavaScript、不解 CAPTCHA、不繞 DRM、不抓 credentials、不保存 page dumps。

### 19E. Reddit Resolver

- [x] 保持匿名解析優先：direct `i.redd.it`、direct `v.redd.it` MP4、Reddit post/share links、`redd.it/<id>` 與 `old.reddit.com` fallback。
- [x] 用 structured skip reasons 偵測 login walls、blocked pages 與 no-media pages。
- [ ] 等有真實範例或 fixtures 後，擴充 deleted/removed/quarantined pages 的 structured skip coverage。
- [x] 目前階段不實作 Reddit auth fallback；無法解析的 login-wall posts 應以 `login_wall` 或 `external_source_hidden` 跳過。
- [x] 在公開可見時解析 Reddit metadata fields，例如 `url_overridden_by_dest`、`secure_media`、`media_embed`、`preview`、`reddit_video` 與 static gallery metadata。
- [x] 如果公開可見的 Reddit metadata 指向外部 URL，將該 URL 交回 resolver chain，而不是在 Reddit resolver 內寫一次性的 domain logic。
- [x] 將 Redgifs 視為優先 provider adapter，因為 live test 已證明 Reddit rich-video posts 常導向 Redgifs。
- [x] 未知外部 providers 交給 Generic Resolver。
- [x] 當 public HTML 暴露 direct `i.redd.it` candidates 時，支援 static Reddit image galleries。
- [x] DASH/HLS muxing 與 multi-file `v.redd.it` support 等 multi-candidate contract 有測試後再做。
- [x] 不加入 Reddit posting、commenting、voting、save/unsave、moderation 或 chat-management features。

### 19F. Redgifs Foundation

目標：將 Redgifs 做成穩定的 no-auth provider adapter。這樣現在就能下載直接 Redgifs links，未來若能打通 `reddit link -> Redgifs link`，也能重用同一條下游解析與下載路徑。

- [x] 為 public `redgifs.com/watch/<id>` 與已知 Redgifs host variants 加入專用 Redgifs resolver。
- [x] 將 Redgifs URLs 正規化成 canonical watch URL 與穩定 remote id。
- [x] 從 bounded public Redgifs watch-page HTML 抽出 direct MP4 candidates。
- [x] 優先選擇清楚的 video candidates，例如 `media.redgifs.com/<Id>.mp4` 或 `media.redgifs.com/<Id>-silent.mp4`，而不是 preview images 或 decorative assets。
- [x] 記錄 `audio_status` 為 `unknown`、`silent` 或 `not_detected`，但不承諾已 mux audio。
- [x] 使用 Generic Resolver 與 `download.http` 相同的 redirect、MIME、size、URL safety checks 驗證 direct Redgifs media。
- [x] 將解析結果映射成 `origin_source: "redgifs"`、`media_type: "video"`、file key `v0`、storage path `library/redgifs/video/<yyyy>/<mm>/...`。
- [x] 當 Redgifs 來自其他 resolver 時，保留 upstream provenance，例如 Telegram inbox 或未來 Reddit delegation。
- [x] 對 unavailable videos、region blocks、login/age gates、JavaScript-only pages、ambiguous multi-media pages、unsupported MIME 與 oversized media 回傳 structured skips。
- [x] 目前階段不使用 Redgifs API credentials 或 third-party API access。
- [x] 不抓 creator profiles、searches、feeds、related videos、comments 或 account data。
- [x] 使用 Telegram inbox 內的 direct 與 Reddit-delegated Redgifs links 做 live test；五個 Redgifs watch links 已解析並下載 MP4 到 phase19 live-test library。

### 19G. Post-19 相關 Provider Adapters

- [ ] 保留 Imgur single-media support，但遷移到相同 provider-adapter pattern。
- [ ] 將 Pixiv artwork-link resolution 與 Pixiv bookmark sync 分開規劃。
- [ ] 將 X post-link resolution 與 X bookmark APIs 分開規劃，並假設 login wall / anti-bot failures 仍可能是正常 skip states。
- [ ] Generic、Redgifs 與 Reddit resolver contracts 穩定前，Instagram 保持 deferred。

### 19H. Deferred Auth Fallback Policy

- [ ] 目前階段不實作 Reddit app-only auth。
- [ ] Reddit user OAuth 與 script password grant 保留為後續 optional local-only fallbacks，不作為專案主要方向。
- [ ] 若之後 Reddit API approval 可用，再重新評估 explicit Reddit links 的 optional metadata-only fallback。
- [ ] 未來任何 Reddit auth fallback 都只能讀取使用者明確提供的 links metadata，不得讀 saved items、feeds、subreddits、comments、votes 或 account history。
- [ ] 任何 future Reddit Data API usage 必須使用 registered OAuth token、unique descriptive `REDDIT_USER_AGENT`，並根據 `X-Ratelimit-Used`、`X-Ratelimit-Remaining`、`X-Ratelimit-Reset` 做 rate-limit backoff。
- [ ] 除非官方 policy 變更，否則遵守 Reddit 目前 free Data API guidance：每個 OAuth client id 100 QPM，並以 10 分鐘 window 平均。
- [ ] 不嘗試繞過 Reddit API limits、login walls、deleted content、removed content 或 access controls。
- [ ] 如果未來透過 API fallback 保存 Reddit metadata，promotion 前必須加入 deleted Reddit user content 的 retention/deletion strategy。

References：

- Reddit Data API Wiki：<https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki>
- Reddit Data API Terms：<https://redditinc.com/policies/data-api-terms>

### 19I. Promotion And Compatibility

- [x] 決定 queue intake 與 sync orchestration 的 stable public tool names：`link.queue.upsert` 與 `link.media.sync`。
- [x] 保留 `telegram.inbox.sync_links` 作為 wrapper，避免既有 live-test commands 中斷。
- [x] 補上 `link.queue.upsert` 與 `link.media.sync` 的 examples。
- [x] 更新 `TOOL_CATALOG.md`、`RUNBOOK.md`、`ARCHITECTURE.md` 與 localized handoff files，反映 stable core link tools。
- [x] Normal tool listing 維持保守；stable link tools 是公開工具，experimental Telegram inbox 與 preview helpers 仍要求 explicit opt-in flags。
- [x] 記錄 promoted link tools 的 exit codes、JSON result shape、dry-run behavior、queue behavior 與 structured skip reasons。

### 19J. Verification And Post-19 Test Follow-Ups

- [x] Unit-test URL normalization、canonicalization 與 normalized URL uniqueness。
- [x] Unit-test 初版 link queue lifecycle metadata、retryable vs permanent skips、source provenance merge 與 batch limits。
- [x] claim/lease execution 實作後，unit-test active retry scheduling 與 concurrent claim behavior。
- [x] Unit-test alias/canonical/media-item dedupe，涵蓋 distinct Reddit links 與 provider/direct media identities。
- [x] Unit-test credential-bearing headers 不會透過 link resolution state 被保存到 SQLite。
- [ ] 等 runtime-only download contexts 存在後，再擴充 secret persistence tests 到 metadata sidecars、logs、snapshots、signed runtime download data 與 `download_context_ref`。
- [x] Unit-test SSRF protections：unsafe schemes、userinfo、localhost/private IPs、unresolved hosts、redirect limits、redirect-to-private-target。
- [x] Unit-test direct media resolution：images、GIF、MP4、WebM、MOV、audio MIME types。
- [x] Unit-test generic HTML candidate parsing、thumbnail rejection、ambiguous candidate skip 與 no-JS behavior。
- [x] Unit-test Redgifs URL normalization、watch-page extraction、direct MP4 candidate selection、preview rejection、unavailable video skip 與 live-test fixture parsing。
- [x] Reddit external URL delegation to Redgifs 實作後，補對應 unit test。
- [x] Unit-test Reddit anonymous fallback、login-wall detection、static gallery resolution 與 structured skips。
- [x] 針對 static file groups，以 fixtures 測試 multi-candidate planning 的 partial success、required-file failure 與 `metadata.files` mapping。
- [ ] Promotion 任何 Reddit API fallback 前，先測試 Reddit rate-limit metadata parsing 與 backoff behavior。
- [x] Unit-test `link.media.sync` dry-run no writes 與 rerun dedupe。
- [x] Live-test 只能使用使用者明確提供的 URLs，且 output paths 必須在 `${MEDIAGENT_DATA_DIR}` 底下。

## Side Decisions And Post-19 Guidance

這些項目用來引導未來工作，不應被視為 Phase 19 尚未完成的實作項目。

- [ ] 將 auth-assisted account collection 視為 optional legacy/advanced behavior；Pixiv bookmark sync 是目前唯一例外。
- [ ] Reddit、X、Instagram 與未來平台優先做 explicit-link resolvers，而不是 saved/bookmark/feed collectors。
- [ ] 等 no-auth Generic Resolver、Redgifs foundation 與 Reddit anonymous resolver 穩定後，再考慮 Reddit auth fallback。
- [ ] X live OAuth verification 仍待處理，因為 API access 可能需要付費 credits。
- [ ] 等 Phase 19 core link tools 完成後，規劃 X 與 Pixiv explicit-link resolvers，讓 inbox automation 可以從明確 post/artwork links 下載，不依賴 bookmark/feed access。
- [ ] 討論 Pixiv 是否需要 bookmarks 以外的 source tools，例如 following-user works 或 explicit artwork IDs。
- [ ] Core link tools 建立後，再把 Telegram inbox link behavior 作為其中一種 URL input source 推廣。
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
- `link_queue.normalized_url` 是第一層 intake uniqueness key。Resolver canonical aliases 與 final media identity 必須避免不同 URL 指向同一內容時產生重複下載。
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
explicit URL source or collector
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
