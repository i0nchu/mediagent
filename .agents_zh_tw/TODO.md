# Mediagent 焦點 TODO

本檔只追蹤接下來要做的實作與驗證工作。已完成狀態、live-test 歷史與已解決問題請放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 最近完成的 Gate：乾淨狀態 Agent Full-Source Verification

目標：證明 Agent Core 可以理解 deployment-style 自然語言任務，而不會把「全部」降級成任意限制。

已在回到 timer hardening 前完成：

- [x] 重建 active SQLite DB 與 `mediagent-data/library`，不用保留舊 live-test 狀態。
- [x] 用 execute mode 執行 `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"`。
- [x] 確認 selected tool 使用 `telegram.inbox.sync_links`，並帶 `full_sync:true`，且沒有捏造 `limit` / `max_messages`。
- [x] 用 execute mode 執行 `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"`。
- [x] 確認 selected tool 使用 `pixiv.bookmarks.sync`，並帶 `full_sync:true`、`stop_on_known:false`，且沒有捏造 `limit` / `max_pages`。
- [x] 重跑同兩個任務，確認 tool-layer dedupe 會避免重複下載。
- [x] 用 `library.file.verify` 驗證下載後的檔案健康狀態。

## 目前焦點：Agent-Mode systemd Timer Deploy MVP

目標：先把 Mediagent 做成會呼叫 Agent Core 的保守 timer-driven 背景服務，再開始建立 long-running daemon。

正式 timer entry 應呼叫 `mediagent agent run "<task>"`，而不是直接呼叫 deterministic tools。Deterministic tools 仍是 Agent Core、regression tests、debugging 與明確 operator verification 會使用的安全底層。

第一個 agent-mode 服務化目標是 Telegram inbox sync，因為它代表會反覆更新的內容來源：掃描 configured inbox、解析新連結、下載支援的媒體、保存 DB/file state，並在下一次 run 從 stored cursor 之後繼續。

第二個 timer-safe 來源是 Pixiv bookmark sync。Pixiv 不像 Telegram 有簡單的「從 cursor 之後的訊息」模型，因此服務化路徑應從最新 bookmarks 開始掃描，遇到已知 terminal item 就停止，並使用 bounded `max_pages` 作為安全上限。

## P0 Gate：Telegram Inbox Message-Link Bridge

- [x] 將 inbox 中 public `t.me/<channel>/<message_id>` 與 private `t.me/c/<chat>/<message_id>` links 導入 Telegram message sync，外部 URL 則繼續使用 link resolver pipeline。
- [x] 在 Telegram 原生媒體保留 inbox chat/message/date/run provenance，並對 protected 或 inaccessible linked messages 回傳 structured skips。
- [x] 為 `telegram.inbox.sync_links` 與 `link.media.sync` 加入 `retry_auth_skipped`，讓 platform session 可用後能重試舊的 `requires_auth` / `login_wall` queue rows。
- [x] 以 fake-client tests 覆蓋 public、private、inaccessible、protected、external 與 Telegram 混合，以及 auth retry paths。
- [ ] 執行一次 bounded live inbox check，包含 public link、可存取 private link、inaccessible link 與一個已恢復 session 的 downstream platform；不要手動 reset production DB。

## 剩餘 Deployment MVP 任務

- [ ] 新增部署導向的 environment check profile，檢查：
  - `MEDIAGENT_DATA_DIR`
  - `MEDIAGENT_DB_PATH`
  - `MEDIAGENT_LIBRARY_DIR`
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`
  - `TELEGRAM_SESSION_FILE`
  - `MEDIAGENT_TELEGRAM_INBOX_KEY`
  - `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME` 或 `MEDIAGENT_TELEGRAM_INBOX_CHAT` 其中之一
- [ ] 新增 run-lock 或 lease guard，避免 overlapping timer runs 同時處理同一個 inbox。
- [ ] 為 `systemd` Agent Core runs 新增 summary-only service output。目前完整 JSON output 對 journal 來說太大，因為它包含完整 artifact lists 與巢狀 resolution payloads。
- [ ] 讓 Pixiv `stop_on_known` 具備 source-aware 判斷，避免其他來源下載的 explicit Pixiv links 在 clean-state rebuild 時過早停止 bookmark sync。
- [ ] 新增 timer-safe failure policy：
  - auth/session failures 會停止本輪
  - rate limits 會停止本輪，不做密集 retry loop
  - partial downloads 不會推進 Telegram cursor

## 驗收標準

- [x] 乾淨 checkout 可以依照 `.env.example` 完成設定。
- [ ] `core.env.check` 或等價 CLI path 可以偵測缺少的 Telegram inbox deployment settings。
- [ ] Dry-run agent-mode timer command 可以解析 configured inbox，不需要使用者在 tool input 傳入 `chat`。
- [x] Execute agent-mode timer command 可以下載新的 inbox media，並保存 `links:<inbox_key>` cursor state。
- [x] 第二次 run 會從 stored cursor 之後開始，不重複下載相同 inbox links。
- [x] Pixiv bookmark timer runs 會從最新 bookmarks 掃描、遇到 known terminal items 停止，且 `MEDIAGENT_LIBRARY_DIR` 改變時不會重複下載已下載作品。
- [ ] Overlapping timer runs 會被防止，或在下載前乾淨失敗。
- [x] Runbook 說明下載後的檔案會放在哪裡。

## 延後到 V2 或更後面

- Long-running daemon process。
- Built-in scheduler。
- Agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory。
- Multi-turn conversation state。
- 超出 selected SKILL 的廣泛自主 planning。
- Workspace-scoped command execution。
- Library rebuild / management workflows。
- Long-running progress 或 structured streaming。
- X explicit post-link support，因為 X API tweet reads 目前需要付費 credits。
