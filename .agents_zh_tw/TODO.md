# Mediagent 焦點 TODO

本檔只追蹤接下來要做的實作與驗證工作。已完成狀態、live-test 歷史與已解決問題請放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 目前焦點：Instagram 收藏媒體 Foundation

目標：參考已驗證的 Pixiv bookmark 架構，新增 deterministic Instagram 收藏媒體收集與同步工具，同時遵守 Instagram saved session 與 private API 的限制。

正常來源流程為：

`saved feed -> 正規化貼文與資源 -> upsert items -> 去重與狀態過濾 -> 規劃儲存路徑 -> 下載 -> 記錄檔案與 item 狀態`

收藏媒體邏輯應放在 Instagram platform 與 tool layers。它必須重用共用 downloader、storage planner、資料庫狀態、repair 行為與 session boundary，不建立第二套下載管線。

### 1. Platform Client 與正規化

- [ ] 新增有界限的 Instagram saved-feed client 操作，每次使用 configured saved session 讀取一頁。
- [ ] 回傳該頁 items 與 opaque next-page cursor，不暴露 cookies、authorization headers、signed media URLs 或原始 session settings。
- [ ] 將照片、Reels／影片與 carousel 貼文正規化為現有 media item/file model。
- [ ] 一個收藏貼文視為一個 media item，並將 carousel 中所有可下載資源納入 file candidates。
- [ ] 保留穩定 source identity、shortcode/media ID、作者、來源時間、canonical post URL、安全 caption metadata、resource index 與 media type。
- [ ] Runtime download URLs 與含憑證的 request context 只能存在記憶體中。
- [ ] 將登入過期、checkpoint/challenge、rate limit、private/unavailable media 與暫時性請求失敗映射到既有 structured Instagram error codes。

### 2. `instagram.saved.collect`

- [ ] 新增穩定的 deterministic Instagram 收藏貼文 collector。
- [ ] 支援有界限的 `limit` 與 `max_pages`，供 operator 測試與受控執行使用。
- [ ] 明確要求完整收集且未指定任意 item limit 時，支援分頁直到 feed 結束。
- [ ] 回傳 collection summary，包含 fetched pages、raw posts、normalized items、resource counts、next cursor 與 stop reason。
- [ ] 不下載檔案，也不修改 media item/file 狀態。
- [ ] Dry-run 只驗證設定並描述預計請求，不呼叫 Instagram 或寫入狀態。
- [ ] 使用既有 saved-session boundary，回傳可採取行動的 auth errors，不自動執行無界限 login loop。

### 3. `instagram.saved.sync`

- [ ] 新增穩定 sync tool，將 collection 與現有 DB、storage、download 及 status helpers 組合起來。
- [ ] 在合理範圍內使用與 Pixiv 相容的語意，支援 `full_sync`、`stop_on_known`、`limit`、`max_pages`、`store_cursor`、`retry_failed`、`repair_missing_files` 與 `write_sidecar_metadata`。
- [ ] Recurring sync 從最新收藏開始掃描，遇到已知 terminal item 後停止；不可只依賴舊 pagination cursor 作為唯一依據。
- [ ] 明確 full sync 應持續到 feed 結束，並由 tool-layer item/file dedupe 跳過健康且已完成的媒體。
- [ ] 只在成功且未截斷的 boundary 保存 durable cursor/source state；partial 或 failed run 不得推進。
- [ ] 重用 scanner-friendly storage：`<library_root>/instagram/<media_type>/<yyyy>/<mm>/...`。
- [ ] 保持完整貼文行為：carousel 的所有資源都下載完成後，才能將 parent item 標記為 downloaded。
- [ ] 記錄 partial 與 failed file/item 狀態，讓後續 `retry_failed` 與 `repair_missing_files` 可以復原。
- [ ] 回傳精簡 summary，包含 collected、known、queued、downloaded、partial、failed、repaired、skipped、files 與 bytes。

### 4. Agent 與 CLI 整合

- [ ] 將兩個工具註冊到 default tool registry，並公開 machine-readable inspect schemas。
- [ ] 新增 bounded collect、recurring sync 與 explicit full sync 的穩定 JSON examples。
- [ ] 新增英文 `instagram_saved_sync` SKILL，讓 Agent Core 能區分收藏同步與 explicit-link download。
- [ ] 確保「所有 Instagram 收藏媒體」自然語言任務不會被捏造 `limit` 或 `max_pages`。
- [ ] Explicit post/Reel URL 任務繼續使用既有 Instagram link-download SKILL。

### 5. 安全與 Rate Limits

- [ ] V1 採保守 sequential page requests，不並行爬取 Instagram feed。
- [ ] 遇到 rate limit、checkpoint/challenge 或 invalid session 時停止本輪，不做密集重試。
- [ ] 不持久化帳號密碼、session cookies、signed CDN query parameters 或原始 private-API payloads。
- [ ] 預設測試完全離線，使用 fake clients 與最小化 fixtures，不包含私人收藏內容或可識別帳號資料。

## 自動化驗證

- [ ] Unit tests 覆蓋空 saved feed、一張照片、一個 Reel／影片、一個多資源 carousel 與分頁。
- [ ] Collector tests 覆蓋 bounded limits、feed exhaustion、dry-run no-network 與 structured auth/rate-limit failures。
- [ ] Sync tests 覆蓋首次下載、第二次去重、stop-on-known recurring sync、full sync、carousel partial failure、retry、missing-file repair、安全儲存路徑與失敗／截斷時 cursor 不推進。
- [ ] Agent tests 覆蓋 bounded requests、recurring update requests 與無界限「所有收藏媒體」任務。
- [ ] `uv run --locked python -m unittest discover -s tests` 通過。
- [ ] `uv lock --check` 通過。
- [ ] `git diff --check` 通過。

## 本機 Live-Test Gate

- [ ] 只使用 `/home/ion/projects/mediagent` 的設定、DB、暫存 library 與 Instagram saved session。開發驗證期間絕不存取 `/data/services` 或 `/data/nas`。
- [ ] 檢查 saved session 一次，只收集一個 bounded page，且不在 log 中輸出私人 URL 或帳號細節。
- [ ] 將少量有界限的收藏貼文同步到專用本機 live-test library。
- [ ] 若 bounded sample 包含 carousel 與 Reel／影片，確認 carousel 會下載所有資源且 Reel／影片會產生有效檔案。
- [ ] 使用相同範圍再執行一次 sync，確認健康檔案會去重且不重複下載。
- [ ] 對專用 live-test scope 執行 `library.file.verify`。
- [ ] 記錄去識別化 summary 後，移除本機 live-test media、DB 與暫存輸出。
- [ ] 只有在自動化驗證與 bounded live test 都通過後，才能將 feature branch 合併到 `main`。

## 本焦點完成後

- 完成 systemd deployment MVP environment-check profile。
- 新增 run lock 或 lease guard，避免 timer runs 重疊。
- 新增適合 systemd journal 的 Agent Core summary-only output。
- 讓 Pixiv `stop_on_known` 具備 source-aware 判斷。
- 加入文件中定義的 timer-safe auth、rate-limit 與 cursor failure policy。

## 延後到 V2 或更後面

- Long-running daemon process。
- Built-in 或 agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory 與 multi-turn conversation state。
- Workspace-scoped command execution 與廣泛 library-management workflows。
- X explicit post-link support；tweet reads 仍需要付費 credits。
