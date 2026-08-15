# 實作議題

## nhentai browser cookie 更新仍待 live re-verification

- **狀態：** JMComic live 驗證完成；nhentai cookie 更新仍需外部操作。
- **目前行為：** JM 帳密登入/session 重用、三頁 42 個 favorite albums、1,081 chapters／49,137 pages 完整 dry-run，以及 108 頁 bounded 真實同步、有效 CBZ 與 0-download rerun 都已 live 驗證。使用者先前驗證過 nhentai cookie auth/favorites/direct download，但該 cookie 現在回 HTTP 401；密碼/CAPTCHA 自動化仍刻意不支援。
- **下一步：** 重新匯出新的 nhentai browser cookie，先在 repo-local paths 重跑 `nhentai.favorites.collect` 與 bounded sync，再啟用其 timer。

## 外部 provider contract 可能變動

nhentai 登入需要瀏覽器提供 cookie session，因 CAPTCHA／proof-of-work 而刻意不自動化帳密登入。JMComic 使用未公開的加密 mobile API 與 CDN scramble 規則。auth、rate-limit 或 response 錯誤必須結構化返回；收藏收集失敗時保留上一次完整 snapshot。

JMComic API 目前會在 adapter 宣告支援 gzip 時壓縮 JSON envelope。Transport 已加入 bounded gzip／deflate decode；損壞、不完整、超限或不支援的 encoding 會回傳不含 response body 的 sanitized structured error。

JMComic segment-count hash 必須排除圖片副檔名。若把 `.jpg`／`.webp` 算進 hash，會選到錯誤的水平分段數，但輸出仍是結構有效圖片，因此 filesystem health check 無法發現。舊受影響檔案需明確使用 `--overwrite`；missing-file repair 刻意不會取代仍存在的檔案。

本檔記錄下一次接手仍需要注意的 caveats。已解決的歷史問題不應長期保留在 Open，除非仍影響實作判斷。

## Open

### 0. Pixiv CBZ 封裝已實作但尚未 live migration

- **狀態：** 等待外部驗證。
- **位置：** `src/mediagent/core/comics.py`、`src/mediagent/tools/pixiv_library_tools.py`
- **目前行為：** Unit tests 已覆蓋 `comic-pages` 分類、Kavita one-shot/series directories、normalized `ComicInfo.xml`、deterministic atomic CBZ、V1 quarantine migration、DB 記錄、重跑重用，以及 `package_comics:true` sync integration；本次開發沒有遷移正式 library 或 database。
- **建議下一步：** 停止重疊 Pixiv jobs，對預定 deployment inputs 執行 reconciliation 與 package dry-run，確認後明確 apply，並在 Immich 同時排除 `comic` 與 `comic-pages`。

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
- **下一步：** 等 link-first sync contract 通過更多 provider adapters、cleanup/recovery tooling 與多次 cron-style runs 後仍保持穩定，再開始 Workflow V1。

### 4. systemd service logs 對一般運維來說過於冗長

- **狀態：** Logging hardening。
- **觀察位置：** `deploy/systemd/user/*.service`、`src/mediagent/cli.py`、Instagram resolver live output。
- **目前行為：** Example user services 目前用 `--json` 呼叫 tools，因此成功執行時會把完整 artifact lists 與巢狀 resolution payloads 寫進 journald。2026-08-05 Telegram service verification 期間，Instagram client 也從 `public_request` 輸出很大的 HTML `JSONDecodeError` diagnostics。該 run 仍成功，但 journal output 對日常 timer operations 來說太大且太吵。
- **下一步：** 新增 summary-only CLI output mode 或 systemd wrapper，預設只記錄穩定 summary，避免把完整 debug payloads 寫進 journald。第三方平台 debug output 也應在未啟用 explicit debug mode 時被抑制或導向其他位置。

### 5. Pixiv `stop_on_known` 可能因非 bookmark 來源的已知項目而停止

- **狀態：** Sync semantics bug。
- **觀察位置：** `src/mediagent/tools/pixiv_tools.py`、2026-08-05 systemd clean-state verification。
- **目前行為：** `pixiv.bookmarks.sync` 只要目前頁面包含任何已在 `media_items` 中是 terminal 狀態的 Pixiv media item 就會停止。Clean-state verification 期間，Telegram inbox sync 先下載了一個 explicit Pixiv artwork link；後續 Pixiv bookmark service run 在第一頁看到同一個 artwork，將它視為 `known_item_seen`，因此掃完第 1 頁就停止。這對 dedupe 是安全的，但對 clean-state bookmark rebuild 來說過於寬鬆，因為該 known item 不一定代表先前 bookmark sync 的邊界。
- **下一步：** 讓 `stop_on_known` 具備 source-aware 判斷。它應該只在 item 來自先前 Pixiv bookmark sync 或其他可信 bookmark-source marker 時停止，而不是任何透過 explicit link 或其他平台 inbox 發現的 terminal Pixiv item。在此之前，clean-state full Pixiv rebuild 應避免使用 `stop_on_known`，或先 reset state 並先跑 Pixiv，再跑 cross-source Telegram inbox sync。

## Recently Resolved

- JMComic manifest 可能包含僅 1-12 px 高但結構有效的 WebP spacer strip。它們先前會永遠撞上 `height < segment_count` safety check，雖然 CDN 都回 HTTP 200，仍留下 12 個 failed files／9 個 partial chapters。下載管線現在會記錄 terminal `skipped`／`ignored_spacer` 而不落地，並從 CBZ 與 `ComicInfo.xml` page count 排除；malformed image 仍然拒絕。Focused tests 已覆蓋混合內容／spacer、全 spacer chapter、malformed tiny data、封裝輸出與第二輪 repair dedupe。
- JMComic recurring favorites 不再因長任務未執行最後一次 session 保存就結束而持續失敗。`jmcomic.favorites.collect`／`.sync` 遇到 `jmcomic_auth_required` 時，每輪最多使用設定好的帳密登入重試一次，立即保存 recovered session，並在 collection 與每個 album resolve 後 checkpoint 輪替 cookie。其他錯誤不會觸發登入，第二次 auth 拒絕會乾淨停止；system service 也為初次完整同步保留 18 小時。
- 已知平台頁面網域不再 fallback 到 generic direct-media 或 generic HTML resolution。Unsupported Instagram page URLs，例如 stories，現在會回傳 structured `instagram_url_unsupported` skip，而不是從偶然出現在 HTML 中的 CDN URL 建立 `instagram_com` media item。Pixiv 非 artwork 頁面與 Imgur gallery/album 類頁面也使用同一個 `reserved_platform_page` guard；Reddit 與 Redgifs 原本就由完整網域 resolver 接管，會繼續回傳平台專屬 structured skips。
- Agent Core V1 現在支援 deployment-style 的「全部」同步任務，但僅限明確暴露 full-source mode 的 SKILL。`telegram_inbox_download` 與 `pixiv_bookmark_sync` 目前宣告 `supports_unbounded:true`；SKILL instructions 會要求模型在 all/complete/until-exhausted 任務中不要捏造 numeric limits，而是交給 tool-layer dedupe、狀態追蹤與檔案安全處理。`telegram.inbox.sync_links` 現在接受 `full_sync:true`，因此 selected-inbox message collection 不會套用預設 100 messages scan limit；一般 `telegram.messages.*` paths 會刻意忽略這個 hidden input 並保留預設 bounded scan。`pixiv.bookmarks.sync` 現在接受 `full_sync:true`，完整 bookmark rebuild 會翻頁到 feed 結束，同時保留直接 CLI/tool 呼叫時舊有的一頁預設。Regression tests 已覆蓋 Agent full-sync action selection、Telegram full-sync message collection、scoped `full_sync` behavior 與 Pixiv full-feed pagination。
- Agent Core V1 收尾已定義 structured SKILL intent boundaries。SKILL frontmatter 包含 `supported_intents`、`unsupported_intents`、`requires_initial_tool_call` 與 `supports_unbounded`；skill selection prompt 會要求使用這些欄位。若 selected SKILL 沒有支援 requested scope 的 full-source mode，action prompt 會要求模型詢問使用者或說明能力缺口，而不是捏造較窄的任務。Pixiv bookmark SKILL 文字已說明 `limit` 是 bookmark item count，不是 file count。Telegram inbox SKILL 文字會讓工具使用 `MEDIAGENT_TELEGRAM_INBOX_*` defaults，並說明 V1 不檢查 inbox 設定。Long-running progress/logging 與 structured streaming 已刻意延後到 V2 或更後面。
- Agent CLI 現在會把 Ollama transport failures 顯示為 structured agent failures，而不是 Python tracebacks。`AgentRunner` 會包住 skill selection 與 action generation 的 LLM calls 並回傳 `llm_request_failed`；CLI JSON mode 會回傳正常 `{status:"failure", error:{...}}` payload。Regression tests 覆蓋 selection-time 與 action-time LLM failures，無法連線 Ollama 的 smoke check 也會以 exit code 1 結束且不出現 traceback。
- Agent Core V1 文件已在英文、繁中與日文 handoff files 同步。`README.md`、`STATE.md`、`RUNBOOK.md`、`TOOL_CATALOG.md` 與 `ARCHITECTURE.md` 現在會把 Agent Core V1 描述為本機 SKILL-scoped preview，列出 agent CLI commands 與 Ollama settings，並移除 LLM Agent Core 尚未實作或不應開始的舊指引。
- Agent Core skill selection 現在支援在任何 tool call 前回傳明確的 `unsupported_task` / tool-gap path。Skill-selection prompt 允許 unsupported outcomes，`AgentRunner` 會把它們映射成 `skill:null` 的 structured failures；針對 `我目前有存在的 telegram inbox 嗎？` 的 live Ollama dry-run 會回傳 `unsupported_task`，且沒有 tool steps。
- Agent Core 現在會在工具執行前套用 destination path policy。它會移除使用者任務中沒有明確出現的 `library_root`、`target_dir` 與 `target_path`；只有位於 configured write roots 內的 explicit user paths 才會保留；不安全的 explicit destinations 會以 `unsafe_agent_destination` 拒絕。Regression tests 覆蓋被移除的 hallucinated paths、被接受的 in-root explicit paths，以及被拒絕的 out-of-root paths。
- Telegram inbox link tools 不再是 experimental。它們現在是 hidden stable tools：預設不出現在 `mediagent tools list`，但知道名稱時仍可呼叫，也可讓 `telegram_inbox_download` SKILL 不加 `--allow-experimental` 直接使用。這反映目前產品決策：功能已穩定且安全，但對 Agent SKILL 之外的外部使用保持低調。
- Agent Core 在 selected SKILL reference 到無法 inspect 的 tool 時，不再以 Python traceback 崩潰。`AgentRunner` 現在會在建構 allowed tool specs 時捕捉 `ToolRegistryError`，並回傳 structured agent failure。
- Agent execute mode 不再允許模型偷偷把真實執行降級成 dry-run preview。`mediagent agent run "<task>"` 現在預設為 execute mode，`--dry-run` 是明確的預覽/開發路徑，`AgentRunner` 會把 tool actions 正規化到全域 runtime mode。若模型在 execute mode 中輸出 `dry_run:true`，被記錄與實際執行的 tool action 會使用 `dry_run:false`；dry-run mode 仍會拒絕模型嘗試執行。Regression coverage 已證明 execute mode 會覆寫模型的 dry-run action。
- Downloaded DB state 不再於明確 repair mode 下遮蔽 missing local files。`link.media.sync`、`telegram.inbox.sync_links`、`telegram.messages.sync` 與 `pixiv.bookmarks.sync` 都接受 `repair_missing_files`；預設重跑仍保守跳過 downloaded items，而 repair mode 會檢查既有 file records 是否 missing/corrupt/unhealthy，或 DB 標記 `downloaded` 但 `local_path` 實體檔案不存在。Pixiv `.trash` 內的檔案視為 missing，但永遠不從 trash 自動還原。Dry-run repair 會回傳 planned downloads 而不寫檔，focused regression tests 已覆蓋 missing、healthy、default 與 dry-run 行為。Bounded live repair 已恢復 8 個可解析的非 Pixiv missing files；仍有 6 筆歷史 Reddit rows missing，原因是 source URLs 目前解析為 `requires_auth:login_required`。`retry_auth_skipped:true` 現在提供 relevant platform session 可用後、不需修改 DB 的明確 retry path；live verification 尚待執行。
- Pixiv runtime download headers 不再持久化到 link resolution storage。Storage sanitizer 現在會省略 runtime-only `runtime_headers` 與 runtime `download_context` keys，而不是存成 `null`；既有 Pixiv live link rows 已清理，focused tests 也已覆蓋 sanitizer behavior。
- Generic link sync 現在會 enforce Instagram session-file 邊界。`ResolveRequest` 會攜帶 allowed write roots，`InstagramMediaLinkResolver` 會傳給 Instagram adapter，adapter 會在 fake-client callbacks、real-client loads 或 network work 前拒絕 out-of-root `INSTAGRAM_SESSION_FILE`。Regression coverage 現在已證明 `link.media.sync` 會回傳 structured `unsafe_credential_path` skipped resolution，且不會呼叫 Instagram fake client。
- Instagram session-file 讀取邊界已修正。`instagram.auth.status` 與 `instagram.link.resolve` 現在會在 fake-client callbacks、real-client loads 或 network work 前，用 `context.allowed_write_roots()` 驗證 resolved saved-session path；out-of-root path 會回傳 `unsafe_credential_path`。兩個工具都有 regression tests。
- `instagram.link.resolve` 現在會 enforce Instagram 平台邊界。非 Instagram hosts 或缺少 supported shortcode 的 URL 會回傳 `instagram_media_unsupported`，且工具也會拒絕任何不是由 `instagram_media_link` 解析出的 resolved result。Regression coverage 已證明非 Instagram direct media 無法透過 Instagram 專屬工具成功解析。
- Phase 20 Instagram foundation 已實作並 live-verified。Stable `instagram.auth.status`、`instagram.auth.login`、`instagram.auth.ensure_session` 與 `instagram.link.resolve` tools 已註冊；`link.media.sync` 可解析並下載 Instagram post/Reel links；直接三連結 live verification 下載 9 個 files，Telegram inbox live verification 另外下載 3 個 Instagram Reel videos 並通過重跑去重。
- Phase 20 TODO 與 STATE docs 已記錄 Instagram carousel 決策：一個 post URL 代表整個貼文，carousel/multi-resource posts 預設應下載所有 resources，`img_index` 只保留為 source metadata，除非未來加入明確選項改變行為。
- 三語 TODO 已修正 stale RuleSpec/Workflow gate。後續 RuleSpec、Workflow V1、scheduling 與 Agent Core 現在必須等更多 provider adapters 與多次 cron-style runs 證明 link-first provider-adapter contract 穩定後再開始，不再只以 Pixiv/Telegram deterministic sync 穩定作為條件。
- Instagram exploratory live-smoke findings 已被三語 STATE 與 TODO 中的正式 Phase 20 foundation verification 取代。正式 direct-tool run 下載 9 個 files，Telegram inbox run 又下載 3 個 Instagram Reel videos，位置都在 `/home/ion/projects/mediagent/mediagent-data/library/instagram/` 底下。
- TODO 現在已在三語版本中聚焦 bounded Phase 21 Pixiv explicit-link Telegram inbox live verification；已完成的 Phase 21 實作細節放在 `STATE.md`。
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
- Phase 16 引入 link resolver pipeline，包含 experimental `link.resolve.preview` / `link.resolve.to_media_item`、低調的 hidden stable `telegram.inbox.collect_links` / `telegram.inbox.sync_links`、目前已遷移到 schema v7 的初版 `link_queue` schema support、origin-source storage metadata 與 link-safe GET downloads。
- Phase 16 URL safety 現在會在 normalization 前拒絕 userinfo，並把 malformed URLs 視為 structured unsafe skips。Regression tests 覆蓋 username-only URLs、username/password URLs、invalid ports、extraction skip behavior 與 resolver preview skip behavior。
- Phase 16 experimental tool boundaries 已 enforce。Normal `tools list` 會隱藏 experimental tools，normal inspect/run 會拒絕，top-level help 不會暴露 hidden experimental command path。
- Phase 16 link sync 改用 link-safe GET path，會重新驗證 redirects、enforce byte limits、拒絕 oversized bodies、在 GET 時驗證 MIME，且只有 GET final URL 本身是 `.mov` suffix 時才套用 MOV fallback。
- Reddit Phase 14 foundation 已實作，包含 `reddit.auth.start`、`reddit.auth.exchange`、`reddit.auth.refresh`、`reddit.auth.status` 與 `reddit.saved.collect`；fake-client tests 覆蓋 auth flows、redaction、generic user-agent rejection、unsafe credential paths、saved-listing normalization、cursor storage、dry-run behavior、media-type filtering、saved comment skip 與 unsupported embed skip。
- Phase 14 Reddit unsafe DB path handling 已修正。`reddit.saved.collect` 會在 network work 或 cursor writes 前，先用 `context.allowed_write_roots()` 驗證 input `db_path`；out-of-root path 會回傳 `unsafe_db_path`，且 regression coverage 確認外部 SQLite 檔不會被建立。
- Phase 14 Reddit auth failure redaction 已修正。`reddit.auth.exchange` / refresh failure payloads 會先經過 Reddit-specific auth sanitization，讓 `code`、`authorization_code`、`access_token`、`refresh_token`、`client_secret` 都被 redacted；regression coverage 確認 `SECRET_AUTH_CODE` 不會出現在 `ToolResult.to_dict()`。
- Phase 13E cleanup/recovery foundation 已透過 `core.cleanup.media_state` 實作。它支援 dry-run planning、explicit apply confirmation、quarantine-before-DB-reset behavior、credential path protection、selector validation 與 path-safety tests。
- Direct Telegram `download_ref` validation 已完成。`telegram.media.download` 現在會在 dry-run 或 network work 前驗證 direct 與 nested refs，至少要求 chat selector、`message_id` 與 `media_id`，並已補上 empty、partially populated 與 missing nested refs 的 regression coverage。
- Telegram sync cancellation recovery 已在 item boundary 實作。若 streaming media download 在建立 `.partial` 後被取消，sync 會記錄 failed file、把 item 標成 failed/retryable、插入 failed run record、移除 partial file，並停止目前 run，不再繼續下載其他檔案。
- Telegram stream-safe real downloads 已實作。真實 Telethon adapter 會直接寫入 `.partial`，`timeout_seconds` 代表無進度 idle timeout，checksum 會分塊計算，並已於 2026-07-24 UTC 成功下載一支一小時 Telegram 影片。
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
- 預設測試是綠燈：`uv run --locked python -m unittest discover -s tests` 通過 323 個測試。
