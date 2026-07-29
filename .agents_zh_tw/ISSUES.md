# 實作議題

本檔記錄下一次接手仍需要注意的 caveats。已解決的歷史問題不應長期保留在 Open，除非仍影響實作判斷。

## Open

### 1. X OAuth 已實作，但尚未 live-verified

- **狀態：** 需要外部驗證。
- **觀察位置：** `src/mediagent/platforms/x/`、`src/mediagent/tools/x_tools.py`
- **目前行為：** X OAuth PKCE、exchange、refresh、status、bookmark collection 已實作，並有 fake HTTP / fixture tests。repo 內沒有真實 X OAuth client 或使用者 credentials，因此尚未做現場驗證。
- **下一步：** 使用使用者提供的 X app credentials，依序跑 `x.auth.start`、完成 browser authorization flow、跑 `x.auth.exchange`，再用 `x.auth.status` 與 `x.bookmarks.collect` 驗證。

### 2. Daemon/workflow orchestration 仍延後

- **狀態：** 設計上延後。
- **觀察位置：** `src/mediagent/tools/download_tools.py`、`src/mediagent/tools/metadata_tools.py`、`src/mediagent/workflows/`
- **目前行為：** Pixiv 與 Telegram 已有 deterministic sync helpers。Explicit URLs 可透過 stable core link tools `link.queue.upsert` 與 `link.media.sync` 處理，Telegram inbox 也仍保留 compatibility wrapper。Reddit saved items 與 X 仍是 collect-only legacy/advanced paths。Workflow V1 也仍不存在。
- **下一步：** 維持 Workflow V1 deferred，等 link-first sync contract 通過更多 provider adapters 與多次 cron-style runs 後仍保持穩定再開始。

### 3. Workflow V1 刻意延後

- **狀態：** 設計上延後。
- **觀察位置：** `src/mediagent/workflows/`
- **目前行為：** Tools 可從 Python 與 CLI 呼叫，但 YAML workflow validation/execution 還不存在。
- **下一步：** 等 deterministic sync 行為通過 cleanup/recovery tooling 後仍保持穩定，且底層/platform tool contracts 保持穩定，再開始 Workflow V1。

### 4. TODO 已寫入 link-first 決策，但沒有指定下一個 active slice

- **狀態：** 規劃交接清晰度缺口。
- **觀察位置：** `.agents/TODO.md`、`.agents_zh_tw/TODO.md`、`.agents_jp/TODO.md`
- **目前行為：** TODO 已清楚說明使用者明確提供的 links 是主要產品路徑，Phase 19 也已完成；但它沒有定義 current/next focus 與排序後的 acceptance criteria。剩餘 post-19 items 同時提到 Imgur provider migration、Pixiv artwork links、X post links、Telegram inbox promotion、Reddit auth fallback policy、RuleSpec、Workflow V1 與 Agent Core。後面的 RuleSpec/Workflow gate 仍使用較舊的 Pixiv/Telegram deterministic-sync wording，而不是其他文件已採用的 link-first stability gate。
- **如何發生：** 新接手的實作者可能合理地挑到較低優先順序項目，或因為 Pixiv/Telegram sync 看似已穩定而開始 Workflow/Agent Core，也可能新增 account/bookmark collector，偏離目前 explicit-link provider adapters 優先的方向。
- **下一步：** 新增一小段 `Current Focus` 或下一 phase section，明確指定下一個 link-first implementation slice、non-goals 與 verification targets。並把 RuleSpec/Workflow wording 改成等 link-first contract 通過更多 provider adapters 與多次 cron-style runs 仍穩定後再開始，三語 TODO 同步更新。

## Recently Resolved

- `STATE.md` 的已實作工具清單現在已在三語版本中把 stable `link.queue.upsert` 與 `link.media.sync` 加在 experimental preview helpers 之前。
- Phase 19 handoff docs 與 TODO 已同步到已實作的 link-first 狀態。三語 `STATE.md`、`TODO.md`、`RUNBOOK.md`、`TOOL_CATALOG.md` 與 `ARCHITECTURE.md` 目前都描述 schema v7、stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>` entry point、queue claim/retry behavior，以及 Reddit/Redgifs delegation。
- `link_queue` 現在明確記錄為 URL resolution lifecycle。Link row 在 resolution 完成後會停在 `resolved`；下載狀態由 `media_items` 與 `media_files` 負責，包含 downloaded、partial 與 failed outcomes。
- Phase 19 第一版 stable link layer 已實作，包含 schema-v7 `link_queue` lifecycle/retry/provenance fields、stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>`、Reddit static 與 preview-fallback gallery resolution、Reddit-to-Redgifs delegation、Redgifs direct/watch resolution、simple static groups 的多檔 candidates、持久化前移除 credential-bearing candidate headers、regression coverage，以及 2026-07-29 UTC live verification。
- Phase 19 queue lifecycle hardening 已實作。Queued `link.media.sync` runs 會用 leases claim ready links、將 temporary failures 排成有 `deferred` schedule 的 links、讓 permanent skips 保持 non-retryable、以 `platform + remote_id` 對 resolved media items 去重，並把 Telegram inbox sync 保留為同一 link pipeline 上的 hidden compatibility wrapper。
- `library.file.verify` 現在會拒絕沒有 `platform` 或 `remote_id` selector 的 explicit non-default `library_root`。這可以避免 live-test roots 被套到 DB 裡所有共享 scanner-friendly relative paths 的 downloaded rows。
- Telegram numeric dialog selectors 現在可以直接回填使用。`telegram.dialogs.list` 可能回傳像 `"3779502941"` 這樣的 selector；真實 Telegram entity selector 現在會在 Telethon lookup 前把 numeric strings 轉成 integers，regression coverage 已確認 string IDs、negative channel IDs、saved messages 與 username selectors。
- Phase 18 Reddit video-only explicit-link support 已實作。`reddit_media_link` 現在會在 generic direct-media fallback 前解析 direct `v.redd.it` MP4 URLs，並可從 Reddit post/legacy pages 擷取 explicit `v.redd.it/...DASH_*.mp4` candidates，映射到 `video` / `v0` / `library/reddit/video/...`，且標記 `audio_status: "not_merged"` 與 `mux_required: true`。Direct `v.redd.it/<id>` manifest links 仍會以 `unsupported_media_type` 與 `reason: video_manifest_unsupported` skip；audio muxing 與完整 DASH/HLS handling 仍延後。
- Phase 17 Reddit explicit-link resolver foundation 已實作。`reddit_media_link` 支援 direct `i.redd.it` images、Reddit post/share links、bounded anonymous HTML，以及搭配靜態 `over18=1` 的 `old.reddit.com` fallback。Fake-client tests 覆蓋 direct image resolution、modern markup extraction、JS verification fallback、gallery skip behavior、single-MP4 Reddit video resolution、highest DASH candidate selection，以及 Telegram inbox sync 進 Reddit layout。2026-07-29 UTC 的 Telegram inbox live verification 已解析並下載 1 張 Reddit JPEG 到 `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/reddit/photo/2026/07/20260728__reddit__t3_1v8yi6w__p0.jpg`；第二次執行去重 queued downloads 0，`library.file.verify` 回報 4 個 live-test files 都是 valid。
- Phase 16 generic HTML resolver candidate selection 現在會在有清楚標記的 original/full media URL 與 preview/thumbnail candidates 之間優先選原始檔；若沒有單一勝出候選，仍維持 ambiguous skip。Telegram inbox live verification 已下載 1 個 valid Danbooru original PNG，並對先前已下載的 nhentai page 成功去重；Reddit short-link page 因回傳 HTML 沒有暴露 static media candidates，仍以 skip 處理。
- Phase 16 generic HTML media discovery 已在沒有 domain allowlist 的前提下實作。它支援單一明確 public HTML media target、HEAD-forbidden HTML pages，並能在 X age/login wall 時跳過而不下載 default preview images。Telegram inbox live verification 已從 public HTML test link 下載 1 個 valid PNG，並把 X link 判定為 `requires_auth`。
- Phase 16 undocumented Telegram inbox link resolver 已放在 experimental boundaries 後方實作，包含 `link.resolve.preview`、`link.resolve.to_media_item`、`telegram.inbox.collect_links`、`telegram.inbox.sync_links`、hidden experimental CLI routing、目前已遷移到 schema v7 的初版 `link_queue` schema support、origin-source storage metadata 與 link-safe GET downloads。
- Phase 16 URL safety 現在會在 normalization 前拒絕 userinfo，並把 malformed URLs 視為 structured unsafe skips。Regression tests 覆蓋 username-only URLs、username/password URLs、invalid ports、extraction skip behavior 與 resolver preview skip behavior。
- Phase 16 experimental tool boundaries 已 enforce。Normal `tools list` 會隱藏 experimental tools，normal inspect/run 會拒絕，top-level help 不會暴露 hidden experimental command path。
- Phase 16 link sync 改用 link-safe GET path，會重新驗證 redirects、enforce byte limits、拒絕 oversized bodies、在 GET 時驗證 MIME，且只有 GET final URL 本身是 `.mov` suffix 時才套用 MOV fallback。
- Reddit Phase 14 foundation 已實作，包含 `reddit.auth.start`、`reddit.auth.exchange`、`reddit.auth.refresh`、`reddit.auth.status` 與 `reddit.saved.collect`；fake-client tests 覆蓋 auth flows、redaction、generic user-agent rejection、unsafe credential paths、saved-listing normalization、cursor storage、dry-run behavior、media-type filtering、saved comment skip 與 unsupported embed skip。
- Phase 14 Reddit unsafe DB path handling 已修正。`reddit.saved.collect` 會在 network work 或 cursor writes 前，先用 `context.allowed_write_roots()` 驗證 input `db_path`；out-of-root path 會回傳 `unsafe_db_path`，且 regression coverage 確認外部 SQLite 檔不會被建立。
- Phase 14 Reddit auth failure redaction 已修正。`reddit.auth.exchange` / refresh failure payloads 會先經過 Reddit-specific auth sanitization，讓 `code`、`authorization_code`、`access_token`、`refresh_token`、`client_secret` 都被 redacted；regression coverage 確認 `SECRET_AUTH_CODE` 不會出現在 `ToolResult.to_dict()`。
- Phase 13E cleanup/recovery foundation 已透過 `core.cleanup.media_state` 實作。它支援 dry-run planning、explicit apply confirmation、quarantine-before-DB-reset behavior、credential path protection、selector validation 與 path-safety tests。
- Direct Telegram `download_ref` validation 已完成。`telegram.media.download` 現在會在 dry-run 或 network work 前驗證 direct 與 nested refs，至少要求 chat selector、`message_id` 與 `media_id`，並已補上 empty、partially populated 與 missing nested refs 的 regression coverage。
- Telegram sync cancellation recovery 已在 item boundary 實作。若 streaming media download 在建立 `.partial` 後被取消，sync 會記錄 failed file、把 item 標成 failed/retryable、插入 failed run record、移除 partial file，並停止目前 run，不再繼續下載其他檔案。
- Telegram stream-safe real downloads 已實作。真實 Telethon adapter 會直接寫入 `.partial`，download call 外層會 enforce `timeout_seconds`，checksum 會分塊計算，並已於 2026-07-24 UTC 成功下載一支一小時 Telegram 影片。
- Telegram real live verification 已完成目前階段目標。2026-07-24 UTC 已用真實 user session 驗證 `telegram.auth.login`、`telegram.auth.status`、curated link-inbox collection、兩個小型 media downloads、一支長影片下載、scanner-friendly layout placement、`library.file.verify` 與第二次執行去重。
- 真實 Telethon client 在 `telegram.auth.login start` 時不會再進入 Telethon interactive prompt；adapter 已改用 explicit connect/disconnect boundaries。
- Private Telegram `t.me/c/...` download links 現在會在下載 linked media 時正確解析 numeric `-100...` chat IDs。
- Telegram inline 2FA password input 已不再支援。`telegram.auth.login` public schema 只暴露 `password_ref`，handler 會在接觸 Telegram 前拒絕 raw `password` input，regression coverage 也確認 raw value 不會外洩。
- Localized runbooks 現在已補上與英文版相同、限定 `/tmp` 的 real-download smoke test 與 cleanup guidance。
- `telegram.auth.login` 已實作為 Telegram user session 的兩步驟本機 login helper。Tests 覆蓋 start、透過 `password_ref` complete、無 config dry-run、缺少 code/hash validation 與 secret redaction。
- Telegram curated link-inbox support 已透過 `telegram.messages.collect` 與 `telegram.messages.sync` 的 `extract_message_links` 實作。Tests 覆蓋從使用者控制的 inbox channel 抽出 message link、解析 linked media，並只推進 inbox cursor。
- Telegram malformed media download validation gap 已修正。當必要的 `download_ref` 欄位缺失時，`telegram.media.download` 現在會回傳 structured validation failure：`telegram_download_missing_ref`。
- Telegram Phase 12 media-source foundation 已實作，包含 Telethon-backed user-session boundaries、`telegram.auth.login`、`telegram.auth.status`、`telegram.dialogs.list`、`telegram.messages.collect`、`telegram.media.download`、`telegram.messages.sync`。測試已覆蓋 fake auth/session status、dialog filtering、protected-content exclusion、album/grouped media normalization、link-inbox extraction、dry-run no writes、Telegram-specific download finalization、deterministic sync、dedupe、partial failure、scoped cursor storage。
- Photo-only Pixiv sync cursor semantics 已修正。`pixiv.bookmarks.sync` 現在會用 media-type-filtered item set 判斷 `limit_truncated`，並把 cursor 存到包含 scope 的名稱，例如 `bookmarks:public:photo`。Regression tests 覆蓋 non-dry-run photo-only cursor storage，並確認 filtered sync 不會修改 unscoped cursor。
- 已支援平台專屬 library root。工具會依序使用 explicit `library_root` / `target_dir`、`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`、`MEDIAGENT_LIBRARY_DIR`，最後才 fallback 到 `${MEDIAGENT_DATA_DIR}/library`；`storage.path.plan` 已有平台專屬 root 的 regression coverage。
- 正式 Pixiv 全 bookmark 自動化缺口已關閉。`pixiv.bookmarks.sync` 現在支援已提交的 `max_pages` pagination 與 `media_types` filtering，並有 multi-page photo-only dry-run 測試。Live download 後用 `{"max_pages":20,"media_types":["photo"]}` 做 dry-run 與 non-dry run，皆掃描 11 頁、收集 309 個 raw items、過濾 306 個 photo items，並正確跳過全部 306 個已下載項目，queued downloads 為 0；non-dry run 已在 SQLite 記錄 1 筆 successful tool run。
- Pixiv sync cursor advancement bug 已修正。`pixiv.bookmarks.sync` 現在會阻止 raw collector 自行儲存 cursor，只在 sync boundary 確認整頁未被 `limit` 截斷且本輪完全成功後才寫 cursor；若 `limit` 截斷 collected page，或本輪 partial/failed，cursor 會維持不變。Regression tests 覆蓋 `limit < collected` 不推進 cursor，以及整頁成功後才儲存 cursor。
- Phase 9 deterministic sync status ownership 現在已透過 `media.item.set_status`、`db.update_media_item_status`、`media_items.downloaded_at` 與 `src/mediagent/core/sync.py` 建立；`pixiv.bookmarks.sync` 會在檔案處理後把 parent item status 更新為 `downloaded`、`partial` 或 `failed`。
- 舊資料庫缺少 `downloaded_at` 的 compatibility bug 已透過 `_ensure_media_items_schema()`、`SCHEMA_VERSION = "4"`，以及 `media.item.set_status` 的舊 v3 regression test 修正。
- `pixiv.bookmarks.sync` 初版缺少 helper 導致 runtime crash 的問題已修正：sync helpers 已補齊，新增 `examples/tools/pixiv.bookmarks.sync.json`，並已覆蓋 dry-run、already-downloaded skip、multi-file 成功下載、partial failure、path safety、Pixiv `Referer`、metadata writing、file records 與 status transitions。
- Phase 5 bottom tool hardening 已完成 examples、CLI smoke tests、structured error categories、rate-limit metadata、sync cursor helpers、media file helpers 與 platform-agnostic fixtures。
- Credential tools 已使用 `read_credentials` / `write_credentials`，token-bearing outputs 會 redacted，支援 explicit credential files，且 credential writes 會限制在 configured write roots。
- `media.file.upsert` 使用 stable non-null `file_key`，即使 `remote_url` 或 `local_path` 缺少也能 idempotent。
- X generic auth status 可透過 semantic keys 讀取 credential refs，例如 `access_token`、`refresh_token`、`scope`、`expires_at`。
- `AuthSession.to_dict()` 保留 `refresh_available` 等安全 status fields，同時仍會 redact metadata secrets。
- Public auth/X schemas 不再針對已檢查工具暴露 raw `access_token` 或 raw `refresh_token` input fields；`x.bookmarks.collect` 使用 configured credentials，`auth.session.refresh` 使用 `refresh_token_ref` / credential files。
- Pixiv Phase 8 第一版已有 refresh-token auth、bookmark collection、多頁作品 normalization、ugoira metadata preservation、credential-file safety checks 與 fixture tests。
- `download.http` 支援 custom request headers，因此 Pixiv media downloads 可以帶 `Referer: https://www.pixiv.net/`。
- Generic `auth.session.status` 與 `auth.session.refresh` 現在會 route Pixiv sessions，並有 focused tests。
- Generic `auth.session.status` 現在支援 Pixiv `credential_refs` 與 `refresh_token` 等 semantic keys，與 X credential-ref 路徑一致。
- Generic `auth.session.status` 現在支援從環境變數與 `credential_refs` 驗證可用的 Pixiv access-token sessions，與 `pixiv.auth.status` 一致。
- `pixiv.auth.login` 已實作為兩步驟本機 OAuth/PKCE helper：start 會印出 login URL 與 code verifier；exchange 接收短效 callback URL 或原始 callback code、寫入 credential file，且永遠不保存 Pixiv 密碼。
- `.env` 與 `.env.example` 已將 `PIXIV_CREDENTIALS_FILE` 說明為 `pixiv.auth.login` 的正常輸出位置，將 `PIXIV_REFRESH_TOKEN` 標成 optional fallback，且沒有加入真實 token 值。
- Pixiv login exchange 失敗時，會在回傳 structured error 前 redact 使用者提交的 authorization code 與 upstream `"code"` fields。
- `pixiv.auth.login` 已有 PKCE start、exchange success、dry-run、unsafe credential path、failed exchange redaction 與 credential writing 的 fixture/fake-client coverage。
- Pixiv live verification 已於 2026-07-21 UTC 完成一次：使用者提供 login 後，`pixiv.auth.status` 回傳 usable session，`pixiv.bookmarks.collect` 回傳 30 個 public bookmark items，`download.http` 成功下載一張 JPEG bookmark 圖片到 `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test/143734851_p0.jpg`，checksum 為 `sha256:72c9988b5d32786423966ff7aae99166041b532571a83f7e4bda1adcd442e2fe`。
- Localized issue handoffs 已同步到目前英文 issue 狀態。
- Localized TODO handoffs 已包含 Pixiv `pixiv.auth.login` / OAuth PKCE planning update，包括 authorization-code exchange、credential-file writing、redaction tests，以及 skipped-by-default live browser tests。
- 英文、繁中、日文 handoff docs 已同步到 Pixiv first-slice status。
- 預設測試是綠燈：`.venv/bin/python -m unittest discover -s tests` 通過 176 個測試。
