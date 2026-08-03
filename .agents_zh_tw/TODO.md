# Mediagent 焦點 TODO

本檔只追蹤接下來要做的實作與驗證工作。已完成狀態、live-test 歷史與已解決問題請放在 `STATE.md`、`ISSUES.md` 與 `RUNBOOK.md`。

更新本 TODO 時，必須在同一次變更中同步更新英文與日文版本：

- `.agents/TODO.md`
- `.agents_jp/TODO.md`

## 目前焦點：剩餘 Missing File 策略決策

目標：決定 bounded repair 後仍指向 missing local files 的 6 筆歷史 Reddit file records 要如何處理。

明確 repair path 已實作並完成 live test。可重新解析的 missing files 已修復。剩餘 records 不是一般 downloader failure；它們的 source URLs 目前會撞到 Reddit login wall，resolver 回傳 `requires_auth:login_required`。

## 決策任務

- 決定是否把剩餘 6 筆 Reddit rows 保留為已知歷史 missing records。
- 決定是否用 `core.cleanup.media_state` reset 或 quarantine 這些 records。
- 決定 Reddit login-wall repair 是否值得新增 resolver/auth 工作，或應繼續和 Reddit OAuth/saved collection 一起延後。
- 沒有新的 dry-run 與使用者明確核准前，不要對整個 live DB 執行 broad repair。

## 驗收重點

- 除非使用者選擇 cleanup 或新的 Reddit auth/resolver work，live verification 應維持 669 valid files 與 6 missing files。
- 在解決 Reddit login-wall limitation 前，任何 agent 都不應把剩餘 6 筆 rows 當成新發現、可直接下載的 media。
- Repair feature 本身視為完成；後續工作是產品策略或 provider capability，不是原本 DB-state bug。

## 延後候選

- X explicit post-link feasibility。
- Instagram session-status TTL 與長時間 cron 驗證。
- Telegram inbox 從 experimental wrapper promoted 成 documented URL input source。
- Reddit/Redgifs follow-up，僅在新的 explicit-link examples 需要時處理。
- Workflow V1，等 link-first provider adapters 通過多次重複執行仍穩定後再做。
- Agent Core / SKILL integration，等 deterministic tools 與 workflow boundaries 穩定後再做。
