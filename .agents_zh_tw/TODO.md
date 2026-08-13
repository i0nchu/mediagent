# Mediagent 焦點 TODO

## 漫畫來源後續

- [ ] 僅用 repo 內本機路徑，分別 live test 一個 nhentai gallery、JM photo、JM album 與兩個收藏來源。
- [ ] Live test 過期 cookie 的復原與瀏覽器重新匯入；provider 已用 HTTP 403 拒絕 refresh，因此不要加入密碼／CAPTCHA 自動化，也不要假設能自動續期。
- [ ] 正式部署 timer 前，先 live 驗證 JM 收藏分頁與一個小型 scrambled chapter。
- [ ] 等既有 single-run lock／timer hardening 完成後才新增部署 unit。

本檔只追蹤接下來要做的實作與驗證工作。已完成狀態、live-test 歷史與已解決問題請放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 目前焦點：systemd Timer Hardening

目標：在加入其他長期來源或 scheduler layer 前，先強化現有 Agent-mode timer 部署。

- [ ] 新增 deployment-oriented environment-check profile，檢查已啟用的 Telegram inbox、Pixiv bookmark，以及選用的 Instagram 收藏媒體來源。
- [ ] 新增 run lock 或 lease guard，讓重疊的 timer runs 在 collection 或 download 開始前乾淨失敗。
- [ ] 新增適合 systemd journal 的 Agent Core summary-only output；預設省略完整 artifacts 與巢狀 resolution payloads。
- [ ] 讓 Pixiv `stop_on_known` 具備 source-aware 判斷，避免由其他來源下載的 explicit Pixiv link 過早停止 bookmark sync。
- [ ] 套用一致的 timer-safe failure policy：
  - auth/session 與 checkpoint failures 會停止本平台本輪執行
  - rate limits 會停止執行，不進行密集 retry loop
  - partial downloads 不會推進 durable source state
  - 成功的 recurring runs 持續由 DB/file state 去重
- [ ] 新增或更新 system-level deployment examples，包含每小時 Telegram/Pixiv tasks，以及選用的保守 Instagram 收藏同步 task。

## 驗收標準

- [ ] 乾淨 checkout 可以在不接觸平台的情況下驗證所有已啟用 timer settings。
- [ ] 同一來源的兩個重疊 runs 不可同時下載。
- [ ] Journal output 每輪只包含一份精簡且去識別化的 final summary。
- [ ] 既有 Telegram 與 Pixiv recurring commands 會從預期 source state 繼續，且不重複下載。
- [ ] Instagram 收藏媒體 recurring sync 使用 `stop_on_known:true` 與 bounded page cap，且不捏造 item limit。
- [ ] Auth、checkpoint、rate-limit、partial-download 與 lock-contention paths 都有 focused offline tests。
- [ ] `uv run --locked python -m unittest discover -s tests`、`uv lock --check` 與 `git diff --check` 通過。

## 延後到 V2 或更後面

- Long-running daemon process。
- Built-in 或 agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory 與 multi-turn conversation state。
- Workspace-scoped command execution 與廣泛 library-management workflows。
- X explicit post-link support；tweet reads 仍需要付費 credits。
- 使用 normalized `metadata.comic` contract 的其他 authorized comic-source adapters；provider access、頁序、series identity 與 policy boundaries 維持 adapter-specific。
