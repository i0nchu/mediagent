# Mediagent 焦點 TODO

本檔只追蹤接下來要實作的工作。已完成狀態、live-test 歷史與已解決問題，放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 目前焦點：Phase 21 Pixiv Explicit Link Resolver

目標：讓使用者提供的 Pixiv 作品連結，可以和 Instagram、Reddit、Redgifs、generic public links 一樣走 link-first pipeline。

主要流程：

```text
Pixiv artwork URL 或 artwork id
-> Pixiv URL/id normalization
-> 既有 Pixiv auth/session handling
-> Pixiv artwork detail fetch
-> normalized media candidates
-> link.media.sync
-> scanner-friendly storage
```

本階段只處理 explicit links，不處理 bookmark/feed discovery。既有 Pixiv bookmark sync 仍保留，但新的功能應盡量重用共享 resolver/download/storage pipeline。

## 產品範圍

- [ ] 支援 `https://www.pixiv.net/artworks/<illust_id>`。
- [ ] 支援 localized artwork paths，例如 `https://www.pixiv.net/en/artworks/<illust_id>`。
- [ ] Pixiv 專屬工具可接受直接輸入 `illust_id`。
- [ ] 一個 artwork URL 代表整個作品。
- [ ] 多頁 illustration/manga 預設解析所有 original pages。
- [ ] 若 URL 帶有頁面選擇提示，第一版只保留為 metadata，除非未來加入明確選項改變行為。
- [ ] 重用既有 Pixiv item identity：`platform = "pixiv"` 與 `remote_id = <illust_id>`。
- [ ] Explicit-link downloads 必須能和 `pixiv.bookmarks.sync` 已下載的 items/files 去重。

## 非目標

- [ ] 本階段不加入 Pixiv feed、following-user、ranking、search、tag、recommendation 或 user-profile collection。
- [ ] 不加入媒體瀏覽、gallery UI、reposting、commenting、bookmarking、liking 或任何 account mutation。
- [ ] 本階段不把 ugoira frames 轉成影片。
- [ ] 若 App API detail path 可用，不做大範圍 Pixiv HTML scraping。
- [ ] 本階段不開始 Workflow V1、內建排程、RuleSpec 或 Agent Core。

## 21A. Resolver Contract

- [ ] 在 core link resolver registry 加入 Pixiv resolver，名稱為 `pixiv_artwork_link`。
- [ ] 將可接受 URL 正規化為 `https://www.pixiv.net/artworks/<illust_id>`。
- [ ] 對等價的 localized Pixiv artwork URLs 產生 aliases。
- [ ] 回傳包含 `origin_source: "pixiv"`、canonical URL、remote id、resolver name、source timestamp、author metadata 與 media candidates 的 `LinkResolution`。
- [ ] 將每個 resolved artwork file 轉成 `MediaCandidate`，包含 stable `file_index`、`part`、`media_type`、`mime_type`、`extension`、`content_identity` 與 source timestamp。
- [ ] 多頁作品仍是一個 Pixiv media item，但具有多個 file candidates。
- [ ] 可取得時保留 title、caption、tags、author id/name、create date、dimensions、Pixiv type、page count、`x_restrict`、visibility、bookmark/view counts。
- [ ] 對 unsupported URLs、缺少 artwork id、private/deleted/unavailable artwork、rate limits、auth failure、unsupported media type 與 resolver failure 回傳 structured skips。

## 21B. Pixiv API 與 Auth 邊界

- [ ] 新增 `pixiv_client.get_illust_detail`，使用 Pixiv App API artwork detail 行為。
- [ ] 重用 `pixiv.auth.status` / `pixiv.auth.refresh` 既有的 credential loading 與 refresh 行為。
- [ ] `pixiv_artwork_link` 可以使用已設定的 Pixiv session，但不得自行啟動 browser login。
- [ ] credentials 缺失或無效時，回傳 agent-decidable errors，並在適當時建議 `pixiv.auth.login` 或 `pixiv.auth.refresh`。
- [ ] Credential files 必須留在 allowed write roots 內。
- [ ] access tokens、refresh tokens、authorization codes、client secrets 與 raw upstream auth payloads 不得出現在 outputs、logs、metadata 或 tests。
- [ ] Pixiv rate-limit 或 temporary block responses 必須轉成 structured rate-limit errors，且不得在 tight loop 內重試。

## 21C. Public Tool Surface

- [ ] 新增 `pixiv.link.resolve`，用來檢查單一 Pixiv artwork URL 或 id，不下載檔案。
- [ ] 註冊 Pixiv resolver，讓 `link.media.sync` 可直接下載 Pixiv artwork URLs。
- [ ] `pixiv.link.resolve` 必須維持 platform-bound：非 Pixiv hosts 不得透過此工具解析成功。
- [ ] 新增 `examples/tools/pixiv.link.resolve.json`。
- [ ] 三語更新 `TOOL_CATALOG.md` 與 `RUNBOOK.md`，加入 Pixiv explicit-link commands。
- [ ] CLI 路徑維持簡單：credentials 設定好後，使用者應可執行 `mediagent link sync <pixiv artwork url>`。

## 21D. Download 與 Storage 行為

- [ ] 使用既有 `link.media.sync` orchestration 處理 upsert、dedupe、path planning、download、sidecar metadata、file records 與 item status。
- [ ] Pixiv image downloads 必須使用必要且安全的 Pixiv `Referer` 行為。
- [ ] 不持久化 credential-bearing headers 或 raw tokens。
- [ ] 檔案存放在目前的 scanner-friendly layout。
- [ ] 在 shared root 下，路徑應類似 `library/pixiv/photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>`。
- [ ] 在 `MEDIAGENT_PIXIV_LIBRARY_DIR` 下，路徑不應重複 platform layer，應類似 `photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>`。
- [ ] 使用既有 media/file status 規則：重跑會跳過已下載檔案，failed items 只有明確要求才 retry，多頁作品部分失敗時 item 標成 `partial`。

## 21E. Ugoira Policy

- [ ] 若 detail flow 可取得 ugoira metadata，重用既有 ugoira metadata parsing。
- [ ] 第一版 ugoira output 表示為 source zip candidate，與目前 Pixiv bookmark-sync 能力一致。
- [ ] 清楚標記 ugoira metadata，方便未來工具轉檔或索引。
- [ ] 若本階段無法安全實作 detail-based ugoira resolution，回傳 `unsupported_media_type`，不要發明部分轉檔行為。

## 21F. Tests

- [ ] Unit-test Pixiv artwork URL/id parsing 與 canonicalization。
- [ ] Unit-test localized URL alias handling。
- [ ] Unit-test `pixiv_client.get_illust_detail` 的 fake HTTP request shape。
- [ ] Unit-test single-page artwork resolution。
- [ ] Unit-test multi-page artwork resolution 與 candidate ordering。
- [ ] Unit-test ugoira zip candidate 或 structured skip 行為。
- [ ] Unit-test auth missing、auth refresh failure、rate limit、deleted/private artwork 與 unsupported URL errors。
- [ ] Unit-test `pixiv.link.resolve` platform boundary 與 secret redaction。
- [ ] Unit-test `link.media.sync` 搭配 Pixiv artwork URL，包含 Pixiv `Referer`、與既有 Pixiv bookmark records 去重、sidecar metadata、scanner-friendly layout。
- [ ] Unit-test dry-run behavior，證明不寫 DB 也不寫檔。

## 21G. Verification

- [ ] 跑完整預設測試：`uv run --locked python -m unittest discover -s tests`。
- [ ] 跑 `uv lock --check`。
- [ ] 用 CLI JSON inspect `pixiv.link.resolve`。
- [ ] 用 `pixiv.link.resolve` dry-run 一個 Pixiv artwork URL。
- [ ] 用 `link.media.sync` dry-run 一個 Pixiv artwork URL。
- [ ] Live bulk verification 延後，等之後和其他平台一起做長時間驗證。

## 後續候選

以下不屬於 Phase 21：

- [ ] X explicit post-link feasibility。
- [ ] Instagram session-status TTL 與長時間 cron verification。
- [ ] Telegram inbox 從 experimental wrapper 推進成正式文件化的 URL input source。
- [ ] 只有在新的 explicit-link examples 需要時，才做 Reddit/Redgifs follow-up。
- [ ] Link-first provider adapters 通過多次執行仍穩定後，再開始 Workflow V1。
- [ ] Deterministic tools 與 workflow boundaries 穩定後，再開始 Agent Core / SKILL integration。
