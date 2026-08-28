# Mediagent 焦點 TODO

## 全域內容識別後續

- [x] 新增 schema-v10 全域 SHA-256 identity、一般媒體路徑收斂、漫畫脈絡 hard links，以及所有 managed download paths 的 adoption。
- [x] 新增完整 library dedup dry-run/apply 與具 audit、checksum conflict、removed-state suppression、中斷恢復的一次性 remove/restore/rename。
- [x] Merge/push schema v10、停止 Production timers、備份並遷移 Production DB，完成第一次全域 dedup dry-run。
- [x] 完成並部署 `library.trash.reconcile`、匯入全部 807 筆 verified rows、套用全域 dedup，並驗證 Production idempotent 結果。
- [x] 部署 managed trash 與 audited Pixiv legacy-CBZ retirement，完成並驗證全部 16 個 cleanup packages。
- [x] 解耦外部 cleanup service：其 API client、policy、分頁、credentials 與 systemd units 留在所屬專案；Mediagent 只保留平台中立的 lifecycle CLI 契約。已修正外部 client 的 cursor／executable 處理，並對全部 392 個選取 assets 完成 live 驗證。
- [x] 稽核 repo 是否有類似外部服務耦合；除正式 acquisition platforms 外沒有其他外部 API client 或 scheduler，留下的 Kavita／Immich 字樣只描述輸出相容性或 operator 設定。
- [x] 本階段維持延後 trash expiry/purge，不加入自動刪除。

## 漫畫來源後續

- [ ] 重新匯出新的 nhentai browser cookie，再跑完整收藏 collect/sync live test；先前由使用者驗證的 cookie 現在回 HTTP 401。
- [ ] Live test 過期 cookie 的復原與瀏覽器重新匯入；provider 已用 HTTP 403 拒絕 refresh，因此不要加入密碼／CAPTCHA 自動化，也不要假設能自動續期。
- [x] 已 live 驗證 JM 帳密登入/session 重用、三頁收藏分頁、42 albums／1,081 chapters 完整 dry-run，以及一個 108 頁的 bounded 真實收藏同步與 CBZ/dedupe。
- [x] 已新增 system-level 漫畫收藏 timer 範例，具備共用 run lock 與 summary-only journal。
- [x] JMComic 遠端 session 過期時每輪最多以帳密恢復一次，在 collection／album resolve 後 checkpoint 輪替 cookie，並將初次完整同步 timeout 調整為 18 小時。
- [x] 將有效的 JMComic 1-12 px spacer strip 分類為 ignored non-content，從 CBZ／page count 排除，並避免 repair 重複嘗試。
- [x] 加入 JMComic 遠端名稱/FID/URL 多資料夾選擇、本機 alias fallback、原子 union membership 與選擇變更 follow 語意，並 live 驗證名稱模式 7 本與 aggregate All 49 本。
- [x] 讓 JM album episode manifest 成為章號權威來源、加入 deterministic 重號 collision suffix，並新增全 library 的 plan／明確確認 apply reconciliation 工具，以本機原始頁重封裝受影響 CBZ。
- [ ] 本機驗收後再獨立部署章號修正；停止重疊的 JMComic／Kavita 活動，先檢視 production `jmcomic.library.reconcile` plan，取得明確 production 授權後才 apply 與重掃 Kavita。
- [ ] 將 JMComic 資料夾選擇部署至 server，初次先用數字 FID 指定目標資料夾，確認 service snapshot 後再交由 timer。

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
