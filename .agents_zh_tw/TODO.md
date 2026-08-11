# Mediagent 焦點 TODO

本檔只追蹤接下來要做的實作與驗證工作。已完成狀態、live-test 歷史與已解決問題請放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 剩餘焦點：Instagram 收藏媒體 Live Verification

離線 foundation 已完成並記錄於 `STATE.md`。剩餘工作僅限下方由 operator 控制的 live-test gate。

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
