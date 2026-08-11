# Mediagent 現在の状態

## 実装済み

- Package layout は `src/mediagent/` にあります。
- `main.py` は薄い起動入口です。
- `pyproject.toml` に console script `mediagent = mediagent.cli:main` が設定されています。
- Tool contract は `src/mediagent/core/tooling.py` にあります。
- Tool registry は `src/mediagent/tools/defaults.py` にあります。
- CLI bridge は `src/mediagent/cli.py` にあります。
- Agent Core V1 は `src/mediagent/agent/` にあり、SKILL loading、strict JSON action parsing、Ollama integration、tool allowlist enforcement、dry-run/execute boundaries、compact/redacted tool-result feedback を含みます。
- Built-in English agent SKILL files は `src/mediagent/agent/skills/builtin/` にあります。
- Agent CLI commands は `mediagent agent run`、`mediagent agent skills list`、`mediagent agent skills inspect` です。
- SQLite 初期化は `src/mediagent/core/db.py` にあり、現在の schema version は `7` です。old media item/file tables と stable `link_queue` lifecycle/retry/provenance fields の idempotent migration に対応しています。
- ファイル安全 helper は `src/mediagent/core/filesystem.py` にあります。
- credential/auth primitives は `src/mediagent/core/auth.py` にあります。
- rate-limit metadata parsing は `src/mediagent/core/rate_limit.py` にあります。
- secret redaction helper は `src/mediagent/core/redaction.py` にあります。
- HTTP abstraction は `src/mediagent/core/http.py` にあり、`download.http` は custom request headers に対応しています。
- Core URL intake と resolver helpers は `src/mediagent/core/links.py` にあります。
- Reddit public-link parsing helpers は `src/mediagent/platforms/reddit/links.py` にあります。
- X platform support は `src/mediagent/platforms/x/` にあります。
- Pixiv platform support は `src/mediagent/platforms/pixiv/` にあり、local OAuth/PKCE setup、explicit refresh-token auth、token refresh、bookmark API calls、multi-page parsing、ugoira metadata preservation を含みます。
- Telegram platform support は `src/mediagent/platforms/telegram/` にあり、Telethon-backed user-session configuration、explicit login boundaries、session status boundaries、dialog listing、message collection/link-inbox boundaries、media normalization、Telegram-specific media download を含みます。
- `telegram.dialogs.list` が返す Telegram numeric dialog selectors は、string または explicit object ID として collect/sync tools に渡せます。
- Reddit platform support は `src/mediagent/platforms/reddit/` にあり、OAuth config/auth helpers、saved-listing API calls、first-version image/gallery/video/direct-media shapes parsing を含みます。
- Reddit explicit-link support は `reddit_media_link` resolver で実装済みです。Direct `i.redd.it` image URLs、direct `v.redd.it` MP4 video-only URLs、Reddit post/share links、bounded anonymous HTML、static non-secret `over18=1` 付き `old.reddit.com` fallback、static galleries、preview-fallback galleries、manifest/login-wall cases の structured skips に対応しています。
- Instagram platform support は `src/mediagent/platforms/instagram/` にあり、saved-session auth boundaries、explicit local login、bounded session repair、post/Reel URL parsing、post-level resource normalization を含みます。
- Instagram explicit-link support は `instagram_media_link` resolver で実装済みです。Configured saved local session を使い、public `/p/<shortcode>/`、`/reel/<shortcode>/`、`/tv/<shortcode>/` URLs を resolve します。
- Dedicated resolver を持つ known platform page domains は `reserved_platform_page` guard で受け止めます。そのため unsupported Instagram pages、Pixiv non-artwork pages、Imgur gallery/album-style pages は generic HTML/media resolution に fall through せず、structured skips を返します。既存 live DB/library の `instagram_com` rows は、この guard 追加前の historical residue です。
- Deterministic sync helpers は `src/mediagent/core/sync.py` にあります。
- Universal storage planning は `src/mediagent/core/storage.py` にあります。
- Default shared-root storage layout は `scanner-friendly-v2` です: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`。
- `MEDIAGENT_<PLATFORM>_LIBRARY_DIR` による platform-specific library roots に対応しています。例: `MEDIAGENT_PIXIV_LIBRARY_DIR`。
- Platform-specific roots はすでにその platform に scoped されているものとして扱うため、default では追加の platform directory を省略します。
- Pixiv bookmark sync は collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update に対応しています。
- Pixiv bookmark sync は `media_types` filtering 使用時に、`bookmarks:public:photo` のような scoped cursor を保存します。
- Telegram message sync は durable processing が成功した後に、`messages:saved_messages:photo-video` のような per-source scoped cursors を保存します。
- Low-profile Telegram inbox link resolver support は Agent SKILL usage 向けの hidden stable tools として提供されています。Telegram は ingest provenance として扱い、解決後の `origin_source` を media item と storage layout の platform として使います。
- Conservative cleanup/recovery support は `core.cleanup.media_state` で実装済みです。Media-state cleanup を plan し、DB reset 前に files を quarantine できます。
- `media_files` は安定した非 null の `file_key` を使うため、`remote_url` または `local_path` が欠けても upsert は idempotent です。
- `media_files` は library-relative path、storage layout version、file health、source timestamp、verification timestamp を保存できます。
- stable JSON examples は `examples/tools/` にあります。
- fake HTTP と recorded-response fixtures は `tests/fixtures/` にあります。
- テストは `tests/` にあります。

## 実装済みツール

- `auth.session.status`
- `auth.session.refresh`
- `auth.session.revoke`
- `core.env.check`
- `core.db.init`
- `core.cleanup.media_state`
- `core.path.prepare`
- `core.run.record`
- `core.sync_cursor.get`
- `core.sync_cursor.set`
- `download.http`
- `library.file.verify`
- `link.queue.upsert`
- `link.media.sync`
- `link.resolve.preview`（experimental）
- `link.resolve.to_media_item`（experimental）
- `media.file.upsert`
- `media.item.upsert`
- `media.item.filter_new`
- `media.item.set_status`
- `metadata.write`
- `storage.path.plan`
- `pixiv.auth.login`
- `pixiv.auth.status`
- `pixiv.auth.refresh`
- `pixiv.link.resolve`
- `pixiv.bookmarks.collect`
- `pixiv.bookmarks.sync`
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links`（hidden stable）
- `telegram.inbox.sync_links`（hidden stable）
- `telegram.dialogs.list`
- `telegram.messages.collect`
- `telegram.media.download`
- `telegram.messages.sync`
- `reddit.auth.start`
- `reddit.auth.exchange`
- `reddit.auth.refresh`
- `reddit.auth.status`
- `reddit.saved.collect`
- `x.auth.start`
- `x.auth.exchange`
- `x.auth.refresh`
- `x.auth.status`
- `x.bookmarks.collect`

## Latest Agent Core V1 State

- Agent Core V1 は LLM-driven であり、deterministic intent planner ではありません。選択された model が strict JSON action protocol を通じて SKILL actions を決めます。
- Supported actions は `call_tool`、`final`、`ask_user` です。
- 最初の LLM backend は Ollama です。Default local settings は `MEDIAGENT_LLM_PROVIDER=ollama`、`MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434`、`MEDIAGENT_OLLAMA_MODEL=qwen3:8b` です。
- Built-in SKILL files は意図的に English で書かれており、user language を前提にしません。LLM が user の自然言語を理解して応答します。
- Built-in skills は `explicit_link_download`、`instagram_link_download`、`library_health_check`、`pixiv_bookmark_sync`、`telegram_inbox_download` です。
- SKILL frontmatter は `supported_intents`、`unsupported_intents`、`requires_initial_tool_call`、`supports_unbounded` により explicit intent boundaries を公開します。
- Agent Core は selected SKILL が full-sync mode を明記している場合に限り full-source tasks を扱います。Telegram inbox と Pixiv bookmark SKILLs は、明示的な `full_sync:true` tool inputs により "all/complete/until-exhausted" requests を支援し、prompt は model に count/page limits を捏造しないよう指示します。
- Pixiv bookmark sync SKILL text は、`limit` が bookmark item count であり downloaded file count ではないことを明記しています。Multi-page artworks は item limit より多い files を生成する場合があります。
- Telegram inbox SKILL text は selected inbox workflow boundary を説明します。Explicit selector がない場合は tool に `MEDIAGENT_TELEGRAM_INBOX_*` を使わせ、V1 では inbox existence/configuration を inspect しないことを明記しています。
- `telegram.inbox.collect_links` と `telegram.inbox.sync_links` は、Agent Core、cron、systemd timer runs の default inbox selector として `MEDIAGENT_TELEGRAM_INBOX_KEY` と `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`、または `MEDIAGENT_TELEGRAM_INBOX_CHAT` を使えるようになりました。
- `mediagent agent run "<task>"` は default で execute mode です。`--dry-run` は explicit preview/development mode であり、runner は tool actions を global runtime mode に normalize するため、model が execute runs を silent に dry-run previews へ downgrade することはできません。
- LLM transport failures は Python tracebacks ではなく structured `llm_request_failed` agent errors として返ります。
- Skill selection は、明確に一致する SKILL がない場合、any tool call の前に `unsupported_task` / tool-gap outcome を返せます。
- Agent Core は user task に明示されていない `library_root`、`target_dir`、`target_path` を strip し、configured write roots 外の explicit destination paths を拒否します。
- Long-running progress/logging と structured streaming は V2 以降に deferred のままです。
- 現在の local `qwen3:8b` model は fake tools で検証済みです。English explicit-link task では `explicit_link_download`、Traditional Chinese inbox task では `telegram_inbox_download` を正しく選び、valid `call_tool` actions を生成し、global run mode を守り、successful tool feedback 後に `final` を返しました。
- `telegram_inbox_download` は action tasks で initial tool call を要求するようになりました。`同期一次inbox的內容` に対する live Ollama dry-run では inbox SKILL が選ばれ、`--allow-experimental` なしで hidden stable `telegram.inbox.sync_links` が呼ばれました。
- `我目前有存在的 telegram inbox 嗎？` に対する live Ollama dry-run は structured `unsupported_task`、`skill: null`、tool steps なしで返りました。

## Latest Clean-State Agent Full-Source Verification

- 2026-08-05 UTC、active `/home/ion/projects/mediagent/mediagent-data/library` と `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` を backup なしで削除し、rebuild しました。`mediagent-data/credentials/` 配下の credentials と session files は保持しました。
- `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"` は `telegram_inbox_download` を選択し、`telegram.inbox.sync_links` を `full_sync:true`、`store_cursor:false`、かつ捏造された `limit` / `max_messages` なしで呼びました。
- 1 回目の Telegram run: 31 links collected/considered、27 resolved、4 skipped links、27 items queued/downloaded、79 files downloaded、474005235 bytes written、0 failed、0 partial。
- Telegram rerun: 31 links considered、27 resolved、4 skipped links、27 skipped items、0 queued、0 files downloaded、0 bytes written。
- `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"` は `pixiv_bookmark_sync` を選択し、`pixiv.bookmarks.sync` を `full_sync:true`、`stop_on_known:false`、`store_cursor:false`、かつ捏造された `limit` / `max_pages` なしで呼びました。
- 1 回目の Pixiv run: 11 pages scanned、309 items collected/discovered、`collection_stop_reason:end_of_feed`、307 items queued/downloaded、2 skipped items、1758 files downloaded、2946174301 bytes written、0 failed、0 partial。
- Pixiv rerun: 11 pages scanned、309 collected/discovered、309 skipped、0 queued、0 files downloaded、0 bytes written。
- `library.file.verify` は 1837 checked files、1837 valid、0 missing、0 corrupt、0 unknown を報告しました。Verification 後の active library は約 3.2G、active DB は約 2.8M です。
- Verification 後の DB summary: downloaded media items は Pixiv 309、Redgifs 10、Instagram 8、Reddit 3、および少数の generic/source-host items を含みます。Downloaded media files は Pixiv 1800、Instagram 18、Redgifs 10、Reddit 5、および source-host/generic files を含みます。
- Telegram inbox runs 中、Instagram resolver は大きな `JSONDecodeError in public_request` HTML diagnostics を stdout/stderr に出力しました。Runs は成功しましたが、open summary-only/logging hardening task の根拠として残ります。

## Latest systemd Timer MVP State

- Telegram inbox sync は最初の timer-deploy target ですが、formal deployment では direct deterministic tools ではなく `mediagent agent run "<task>"` 経由で起動するべきです。
- `.env.example` は default inbox selection 用に `MEDIAGENT_TELEGRAM_INBOX_KEY` と、`MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`、または `MEDIAGENT_TELEGRAM_INBOX_CHAT` を document しています。
- Local `.env` には current live test 用の non-secret Telegram inbox selector values として、`MEDIAGENT_TELEGRAM_INBOX_KEY=mediagent_inbox` と numeric inbox chat id を追加しました。
- `telegram.inbox.collect_links` と `telegram.inbox.sync_links` は、default inbox env vars が設定されている場合、explicit `chat`/`chats` input なしで実行できます。
- 2026-08-04 UTC の Telegram inbox execute live verification は selector key `mediagent_inbox` を使い、existing cursor `links:mediagent_inbox=34` を読み、3 new links を collect、3 links を resolve、3 video files を download、40603018 bytes を書き込み、cursor `links:mediagent_inbox=38` を保存しました。
- Follow-up dry-run と `幫我同步更新下載 telegram inbox 中的內容` に対する Agent Core execute run は、cursor `38` の後に 0 new links と 0 queued downloads を返し、current inbox の rerun cursor continuation が正常であることを確認しました。
- Pixiv bookmark sync は timer-safe `stop_on_known` scanning に対応しました。有効にすると newest bookmarks から scan し、bounded `max_pages` まで進み、known terminal media item を含む page に到達したら停止します。
- `stop_on_known` mode では、Pixiv sync は known item で停止した時に API pagination cursor を保存しません。そのため Telegram-style continuation cursor と誤解されません。
- Agent Core の Pixiv recurring sync は、invented default item `limit` ではなく、`stop_on_known:true` と bounded `max_pages` を指定した `pixiv.bookmarks.sync` を使います。
- 2026-08-04 UTC の `幫我同步更新下載 pixiv bookmark 中的內容` に対する Pixiv Agent Core live dry-run は 1 page を scan し、30 known bookmark items を collect し、`collection_stop_reason: known_item_seen`、queued 0 downloads、0 files written を返しました。
- Alternate `MEDIAGENT_LIBRARY_DIR` を使った direct Pixiv dry-run も queued 0 downloads で、library root の変更が DB-based media item dedupe を reset しないことを確認しました。
- `deploy/systemd/user/` には local example user units、timers、JSON inputs、Telegram inbox sync と Pixiv bookmark sync 用の minimal runbook があります。
- 2026-08-05 UTC の clean-state user-systemd verification では old library/live-test outputs を削除し、old SQLite DB を `mediagent-data/backups/mediagent.sqlite3.20260805014915.bak` に backup し、schema v7 を initialize し、credential files は保持しました。
- 以前失敗していた exact full-source Agent Core tasks `下載所有 telegram inbox 內所有可下載的媒體來源` と `下載 pixiv bookmark 所有可下載媒體來源` は code 上で修正済みです。次の verification step は clean DB/library rebuild でこの 2 つの natural-language tasks を再実行することです。
- `systemctl --user start mediagent-telegram-inbox.service` は clean DB で成功しました。First run は 31 links collected、27 resolved、4 skipped、79 files downloaded、474005235 bytes written、cursor `links:mediagent_inbox=39` stored。Second run は 0 new links、0 files downloaded でした。
- `systemctl --user start mediagent-pixiv-bookmarks.service` は Telegram run 後に成功しました。First run は 1 page scanned、30 bookmarks collected、Telegram が先に explicit Pixiv item を 1 件 download 済みだったため 1 skipped、29 bookmark items を 293 files として download、447025170 bytes written。Known item で stop したため API pagination cursor は保存していません。Second run は queued 0、skipped 30 でした。
- Verification 後の library state: 372 downloaded file records、372 valid files、0 missing、0 corrupt、0 unknown。Rebuilt library は約 880M です。

## Latest Repair-Mode State

- `link.media.sync` は `repair_missing_files: true` による明示的 file-health-aware repair をサポートします。
- `telegram.inbox.sync_links` と `telegram.messages.sync` も、既存 sync logic 上の compatibility paths として同じ option を公開します。
- Default rerun は conservative のままです。Repair mode が明示されない限り、downloaded items は skip されます。
- Repair mode は required file records が missing/corrupt/unhealthy、または DB row は `downloaded` だが `local_path` の実体 file が存在しない場合だけ、downloaded items を再 queue します。
- Dry-run repair は同じ candidate selection を使い、file write や DB mutation なしで `planned_downloads` を返します。
- Focused regression tests は missing-file queue、healthy downloaded skip、default rerun unchanged、dry-run no-write planning を覆っています。
- 2026-08-03 UTC に live DB へ dry-run repair planning を実行しました。14 件の missing downloaded file records から 12 unique source URLs を導出し、8 repair downloads を 4 providers に対して resolve/plan しました。4 links は resolution 段階で skip されました。0 bytes written、0 files downloaded で、live DB は 675 downloaded file records と同じ 14 missing on disk のままです。
- 2026-08-03 UTC の bounded non-dry repair は同じ 12-source scope を使い、Danbooru、nhentai、Redgifs、rule34 にまたがる 8 repaired files を download し、76755767 bytes を書き込み、failed/partial items は 0 件でした。
- Repair 後の `library.file.verify` は、675 downloaded file records に対して 669 valid、6 missing、0 corrupt、0 unknown を報告しました。Remaining 6 missing rows はすべて Reddit records で、4 unique source URLs から来ています。Diagnostic dry-run はこれらの source に `requires_auth:login_required` を返しました。

## Telegram Inbox Message-Link Bridge State

- `telegram.inbox.sync_links` は同じ inbox message 内の external URLs と Telegram message links を分離します。External URLs は shared resolver/download path を維持し、public/private `t.me` / `telegram.me` message links は Telegram native collect/sync/download logic に delegate します。
- Telegram native items は inbox chat ID、source message ID/date、collector run ID、merged source provenance を保持し、inbox message text は永続化しません。
- Protected、missing、private、deleted、その他 inaccessible な linked messages は inbox run を abort せず、per-link structured skips を返します。
- `link.media.sync` の `retry_auth_skipped:true` は旧 auth-dependent queue rows を retry し、`telegram.inbox.sync_links` の同 option は Telegram-ingested rows に限定されます。両 path は lease で claim し、explicit operator intent を必要とします。
- Fake-client regressions は public、private、inaccessible、protected、external と Telegram の mixed case、old auth-skip retry paths をカバーします。この実装では live download を行っていません。

## 検証済み

最後に確認した検証コマンド:

```bash
uv run --locked python -m unittest discover -s tests
uv lock --check
uv run --locked mediagent tools list --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect reddit.saved.collect --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools inspect pixiv.link.resolve --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
PIXIV_ACCESS_TOKEN= PIXIV_REFRESH_TOKEN= PIXIV_CREDENTIALS_FILE= uv run --locked mediagent tools run pixiv.link.resolve --input examples/tools/pixiv.link.resolve.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

最新の local full suite は 227 tests passing です。

Phase 16 Telegram inbox link resolver verification:

- `link.resolve.preview` と `link.resolve.to_media_item` は experimental tools として実装済みです。`telegram.inbox.collect_links` と `telegram.inbox.sync_links` は Agent SKILL usage 向けの hidden stable tools です。
- 通常の `mediagent tools list` は experimental tools と low-profile hidden tools を隠します。`--include-experimental` は experimental tools を表示しますが、hidden tools は名前を知っていれば呼び出せます。
- 通常の `mediagent tools run link.resolve.to_media_item` は `experimental_tool_not_allowed` で実行を拒否します。
- Top-level `mediagent --help` は hidden `experimental` command path を露出しません。
- Tests は URL normalization、`normalized_url` uniqueness、userinfo rejection、malformed URL skip behavior、unsafe schemes、localhost/private IP rejection、unresolved host rejection、redirect limits、unsupported MIME rejection、`.mov` / `video/quicktime`、generic single-media HTML discovery、HEAD-forbidden HTML fallback、X age/login wall skip behavior、Imgur single-page resolution、ambiguous multi-media skip、Pixiv artwork-link `requires_auth`、duplicate Telegram URL queueing、origin-source storage layout、raw message text を含まない Telegram provenance metadata、safe GET redirect revalidation、oversized GET body rejection、MOV redirect-to-non-media rejection を覆っています。
- Isolated live network smoke verification を実行し、`https://www.gstatic.com/webp/gallery/1.jpg` を temporary scanner-friendly path に resolve/download しました: `gstatic_com/photo/2026/07/20260728__gstatic_com__url_3e125a8d7d4f4d6e6dea2830__p0.jpg`、44891 bytes、`image/jpeg`、checksum present、DB file record written、metadata sidecar written、temporary directory cleaned up。
- Real Telegram auth status は usable でした。Real Telegram inbox sync は local `inbox` channel に対して integer chat selector `3779502941` で実行しました。Phase 16 live verification は nhentai/Danbooru の generic public HTML handling と X login-wall skip を確認しました。Reddit short links は現在、下の Phase 17 で扱います。

Phase 17/18 Reddit explicit-link resolver verification:

- Fake-client tests は direct `i.redd.it`、direct `v.redd.it` MP4 video-only resolution、modern Reddit `shreddit-post` extraction、modern JS verification fallback to `old.reddit.com`、Reddit pages からの explicit `v.redd.it/...DASH_*.mp4` extraction、highest DASH MP4 candidate selection、gallery skip behavior、direct `v.redd.it/<id>` manifest skip behavior を覆っています。
- Reddit MP4 resolutions は `media_type: "video"`、`part: "v0"`、`library/reddit/video/...` に map され、audio muxing が未実装の間は metadata に `audio_status: "not_merged"` / `mux_required: true` を記録します。
- Telegram inbox sync fake-client coverage は Reddit MP4 links が `library/reddit/video/...` に download され、Telegram は `ingested_from` としてのみ残ることを確認しています。
- 2026-07-29 UTC の real Telegram auth status は usable でした。
- Real Telegram inbox sync は chat selector `3779502941` から 5 external links を収集し、4 件を resolved しました。X link は `requires_auth` / `login_or_age_gate` として skip しました。
- Reddit share link は `reddit_media_link` と `old.reddit.com` fallback で解決し、JPEG を `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/reddit/photo/2026/07/20260728__reddit__t3_1v8yi6w__p0.jpg` に download しました。
- 同じ live run で rule34 PNG、nhentai JPEG、Danbooru PNG も `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/<platform>/photo/2026/07/...` に download しました。
- Second-run dedupe は成功し、queued downloads 0、bytes written 0 でした。
- `library.file.verify` は 4 live-test files を checked しました: valid 4、missing 0、corrupt 0、unknown 0。

Phase 19 link-first live verification:

- Stable core link tools `link.queue.upsert` と `link.media.sync` は実装済みで、experimental flags なしで discovery できます。
- Public CLI entry point `mediagent link sync <url>` は `link.media.sync` に delegate します。そのため non-Telegram link automation も Telegram inbox compatibility wrapper と同じ resolver/download/storage pipeline を使います。
- Public CLI live smoke は known Redgifs URL を `mediagent link sync <url>` で rerun しました。同じ pipeline で resolve し、already-downloaded item を skip し、duplicate bytes は 0 でした。
- Queued `link.media.sync` runs は `lease_owner` / `lease_expires_at` で ready links を claim し、他 worker の non-expired leases を無視し、retryable failures を bounded `next_attempt_at` backoff 付きの `deferred` records として schedule します。
- Reddit explicit links は、publicly visible な external post URL が 1 件だけある場合、それを resolver chain に delegate できます。Redgifs delegated results は Redgifs storage/layout を維持しつつ Reddit upstream metadata も保存します。
- Telegram inbox compatibility wrapper `telegram.inbox.sync_links` は 2026-07-29 UTC に chat selector `3779502941` に対して live-run 済みです。`store_cursor:false`、output root は `/home/ion/projects/mediagent/mediagent-data/live-test-phase19/library` でした。
- First run は 13 external links を collect し、9 件を resolve し、6 件の新規 media items を queue/download し、4 links を structured reasons で skip し、failed/partial downloads は 0 でした。
- 以前 skipped だった Reddit gallery link は `link.media.sync` で rerun しました。Anonymous `old.reddit.com` public HTML が `preview.redd.it` candidates を公開し、preview fallback により `t3_1v8boac` の JPEG files 3 件を download しました。
- Latest compatibility-wrapper rerun は 13 links を collect し、12 件を resolve し、1 件の expected X/auth link を skip し、2 件の新規 Reddit-delegated Redgifs MP4 files を download し、10 件の already-known items を skip しました。Failed/partial downloads は 0 でした。
- Phase 19 live-test library の downloaded files は 5 件の Redgifs MP4 videos と 6 件の Reddit photo/GIF/JPEG files で、`library/redgifs/video/2026/07/...` と `library/reddit/photo/2026/07/...` 配下に保存され、合計 211178527 bytes でした。
- Platform selectors を使った `library.file.verify` は Redgifs 5/5 valid、Reddit 6/6 valid を確認しました。`.partial` / `.tmp` files は残っていません。

Phase 20 Instagram foundation verification:

- Stable `instagram.auth.status`、`instagram.auth.login`、`instagram.auth.ensure_session`、`instagram.link.resolve` は実装済みで、default tool registry に登録され、fake-client regression tests で覆われています。
- Local Instagram saved session は `/home/ion/projects/mediagent/mediagent-data/credentials/instagram_session.json` にあり、permission は `0600` です。これは credential として扱う必要があります。
- `instagram.link.resolve` は platform-bound です。Non-Instagram direct media は `instagram_media_unsupported` で拒否され、out-of-root saved-session paths は fake-client callbacks、real-client loads、network work の前に `unsafe_credential_path` を返します。
- 1 件の Instagram post URL は post 全体を表します。Carousel/multi-resource posts は default ですべての resources を download し、`img_index` は future explicit option がない限り source metadata としてのみ保持します。
- Instagram CDN media URLs は signed/expiring runtime data です。Download run 中だけ使い、SQLite、sidecar metadata、logs、snapshots、tool output には persist しません。
- 2026-07-30 UTC の direct formal-tool live verification は user-provided Instagram links 3/3 件を resolve し、auth/rate-limit/checkpoint failures は 0 でした。その後 `link.media.sync` で `/home/ion/projects/mediagent/mediagent-data/library/instagram/` 配下に 9 files を download しました。内訳は JPEG photos 7 件、MP4 videos 2 件です。
- Direct test の 2 件の `/p/<shortcode>/` links は carousels でした。1 件は JPEG resources 3 件、もう 1 件は JPEG resources 4 件と MP4 resource 1 件を download しました。`/reel/<shortcode>/` link は MP4 resource 1 件を download しました。
- 2026-07-30 UTC の Telegram inbox live verification は user-posted Instagram links を collect し、選択した Reel links 3/3 件を resolve し、MP4 files 3 件を `/home/ion/projects/mediagent/mediagent-data/library/instagram/video/2026/07/` に download しました。Rerun は 3 件すべてを already-downloaded として skip し、duplicate bytes は 0 でした。
- Filesystem verification は JPEG/MP4 container types が valid で、Instagram library root 配下に `.partial` / `.tmp` files が残っていないこと、mixed-carousel layout が正しいことを確認しました。Photo resources は `instagram/photo/...`、video resources は `instagram/video/...` に配置されます。
- SQLite/sidecar checks は direct と inbox live tests 合計で Instagram media items 6 件、media-file rows 12 件を確認し、signed CDN hosts ではなく stable Instagram post/resource URLs が使われていることを確認しました。

Reddit foundation verification:

- `reddit.auth.start`、`reddit.auth.exchange`、`reddit.auth.refresh`、`reddit.auth.status` は実装済みで、CLI discovery が可能です。
- `reddit.saved.collect` は実装済みで、CLI discovery が可能です。
- Fake-client tests は auth URL generation、token exchange credential-file writing、refresh token preservation、status checks、redaction、generic user-agent rejection、unsafe credential paths、saved-listing normalization、cursor storage、dry-run no DB writes、unsafe collector DB paths、media-type filtering、saved comment skip、unsupported embed skip を覆っています。
- `reddit.saved.collect` は normalized media items だけを返し、`media_files` は書きません。
- Reddit auth/saved live verification は auth-assisted account collection を明示的に再開するまで deferred です。

Cleanup/recovery foundation verification:

- `core.cleanup.media_state` は dry-run planning で files や DB を変更しないことを覆っています。
- Apply mode は `confirm: true` を要求します。
- Apply mode は existing media files を quarantine してから matching media items を `discovered` に reset し、matching media file rows を削除します。
- Credential paths は保護され、actionable cleanup file paths として出力されません。
- Unsafe quarantine paths は拒否されます。

Telegram foundation verification:

- `telegram.auth.login` は login-code start、`password_ref` 付き complete、Telegram config なし dry-run、missing code/hash validation、inline password rejection、secret redaction を覆っています。
- `telegram.auth.status` は missing config、unsafe session paths、usable fake sessions、secret redaction を覆っています。
- `telegram.dialogs.list` は message/media content を返さない filtered dialog listing を覆っています。
- `telegram.messages.collect` は explicit chat selection、media type filtering、protected-content exclusion、album/grouped media normalization、private/public message-link parsing、curated link-inbox extraction、linked media resolution、scoped cursor storage を覆っています。
- `telegram.media.download` は safe writes、`.partial` finalization、checksum output、MIME validation、path safety を覆っています。
- `telegram.media.download` は malformed direct / nested `download_ref` input で generic runtime error ではなく `telegram_download_missing_ref` を返すことも覆っています。
- `telegram.messages.sync` は collect -> upsert -> status filter -> storage path plan -> Telegram-specific download -> file record -> item status update -> scoped cursor storage を覆っています。
- `telegram.messages.sync` は `.partial` 作成後の download cancellation も覆っています。failed file/item/run state を記録し、partial file を削除します。
- Fake client を使った Telegram dry-run sync は DB と library files を書かないことを確認しています。
- Telegram real login、auth status、curated link-inbox collection、小さな media download、long-video download、layout placement、`library.file.verify`、rerun dedupe は 2026-07-24 UTC に live-verified 済みです。
- Telegram real downloads は `.partial` に直接 stream し、`timeout_seconds` は no-progress idle timeout として扱い、checksum は chunked で計算し、atomic move で finalize します。

Deterministic Pixiv sync verification:

- `pixiv.bookmarks.sync` は fake-client tests で multi-file success、second-run skip、dry-run no writes、partial failure、path safety、Pixiv `Referer`、scanner-friendly storage layout、file records、item status updates、安全な cursor advancement を覆っています。
- `pixiv.bookmarks.sync` には、photo-only sync が media-type filtering 後も cursor を保存する regression coverage があります。
- `storage.path.plan` には platform-specific library roots の regression coverage があります。
- `storage.path.plan` と `pixiv.bookmarks.sync` には `scanner-friendly-v2` platform layer と、platform-specific roots の下で duplicate platform directories を避ける regression coverage があります。
- `media_items.downloaded_at` がない old-style SQLite DB は `core.db.init` / tool initialization で migration され、`media.item.set_status` が downloaded state を更新できます。

Phase 21 Pixiv explicit-link implementation verification は 2026-08-03 UTC に完了しました。

- `pixiv.link.resolve` は 1 件の Pixiv artwork URL または `illust_id` を resolve する stable public tool として実装済みです。
- Core `pixiv_artwork_link` resolver は Pixiv artwork detail を使い、normalized media candidates を生成し、multi-page works を support し、ugoira zip candidates を保持し、structured Pixiv auth/rate-limit/unavailable errors を返します。
- `link.media.sync` は Pixiv artwork URLs を直接扱い、既存 Pixiv bookmark-sync items/files と dedupe し、runtime headers を永続化せずに必要な Pixiv `Referer` を適用できます。
- Fake-client tests は URL/id parsing、localized aliases、artwork detail request shape、multi-page resolution、ugoira zip candidates、missing credentials、unsafe credential-file paths、`pixiv.link.resolve` platform boundary、Pixiv `Referer`、bookmark-sync dedupe を覆っています。
- CLI inspect は `pixiv.link.resolve` と `link.media.sync` で動作します。No-credential dry-run は structured `pixiv_auth_missing_credentials` と `recommended_tool: "pixiv.auth.login"` を返します。

Phase 21 Telegram inbox live verification は 2026-08-03 UTC に完了しました。

- Natural-language task「download all new media in inbox」を、configured inbox chat に対する `telegram.inbox.sync_links`、cursor storage enabled、shared link resolver/download pipeline として解釈しました。
- First live run は 27 external links を collected/considered し、24 resolved、3 skipped、9 new media items queued、9 items / 22 files downloaded、134098941 bytes written、partial 0、failed 0 でした。
- Pixiv explicit links は `pixiv_artwork_link` で resolve されました。`112418327` は 4 files を `library/pixiv/photo/2023/10/...` に download し、`137814756` は 38 already-known valid files として dedupe skip されました。
- Second live run は collected links 0、downloaded files 0 で、inbox path cursor advancement を確認しました。
- `library.file.verify` は 675 DB file records を checked しました: valid 661、missing 14、corrupt 0、unknown 0。14 missing rows は古い already-recorded link-first live-test files であり、この run で download した新規 files ではありません。
- この run の newly downloaded 22 artifact paths はすべて存在します。Pixiv persisted media metadata に runtime headers や tokens はなく、Pixiv link resolution rows も `runtime_headers` または runtime `download_context` keys を永続化しません。

Pixiv live verification は 2026-07-21 UTC に一度完了しました。

- `pixiv.auth.status` は user-provided account に対して usable session を返しました。
- `pixiv.bookmarks.collect` は public bookmark items を 30 件返しました。
- `download.http` は JPEG bookmark image を `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test/143734851_p0.jpg` に download しました。
- download verification: 330936 bytes、`image/jpeg`、checksum `sha256:72c9988b5d32786423966ff7aae99166041b532571a83f7e4bda1adcd442e2fe`。

Phase 11 live storage verification は 2026-07-22 UTC に完了しました。

- 古い Pixiv live download output `/home/ion/projects/mediagent/mediagent-data/media` を削除しました。
- `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` の Pixiv media item/file/cursor state を reset し、credentials は保持しました。
- Pixiv public bookmarks 11 pages を再収集しました: raw bookmark items 309、photo items 306、image files 1797。
- `scanner-friendly-v1` で 1797 image files すべてを `/home/ion/projects/mediagent/mediagent-data/library` に再 download しました。
- Public library verification: media files 1797、JSON sidecars 0、`.partial` files 0。
- SQLite verification: schema version `5`、Pixiv photo items 306 件が `downloaded`、Pixiv media files 1797 件が `downloaded`。すべて `library_relative_path`、`storage_layout = scanner-friendly-v1`、`file_health = valid` を持ちます。
- `library.file.verify` は 1797 files を確認しました: valid 1797、missing 0、corrupt 0、unknown 0。
- Second-run dedupe check: Pixiv photo items 306 件は skip、再 download 対象は 0。
- その後、committed `pixiv.bookmarks.sync` を non-dry で `max_pages = 20`、`media_types = ["photo"]` として実行しました。11 pages を scan し、downloaded photo items 306 件をすべて skip、queued downloads は 0、SQLite に successful tool run を 1 件記録しました。

Pixiv live artifacts cleanup は 2026-07-24 UTC に完了しました。

- 古い single-file Pixiv smoke output `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test` を削除しました。
- 古い full Pixiv live library output `/home/ion/projects/mediagent/mediagent-data/library` を削除しました。
- 空の `/home/ion/projects/mediagent/mediagent-data/pixiv` directory を削除しました。
- `/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3` の Pixiv `media_items`、`media_files`、`sync_cursors` を 0 に reset しました。
- `/home/ion/projects/mediagent/mediagent-data/credentials/pixiv-oauth.json` は保持しました。

Phase 13 Telegram + Pixiv layout live verification は 2026-07-24 UTC に実行しました。

- Telegram `telegram.auth.login` は user-provided app code で完了し、`telegram.auth.status` は usable session を返しました。
- Telegram `telegram.dialogs.list` は user-controlled collection channel を見つけました。
- Telegram `telegram.messages.collect` は 3 件の curated message links を 3 件の media items に解決しました: long private video 1 件、小さな video/GIF-style file 1 件、photo 1 件。
- Long private Telegram video は最初に real-download buffering issue を露出して `failed` に marked されましたが、stream-safe download support 実装後に retry 成功しました。
- Telegram direct link sync は 2 件の小さな media files を shared scanner-friendly library に download しました:
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov`
  - `/home/ion/projects/mediagent/mediagent-data/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg`
- 同じ Telegram direct link sync の再実行では queued downloads 0、already-downloaded items 2 件が skipped でした。
- Telegram stream-safe long-video sync は `/home/ion/projects/mediagent/mediagent-data/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4` を download し、660481192 bytes を write、failed files は 0 件でした。
- 同じ long-video sync の再実行では queued downloads 0、already-downloaded item 1 件が skipped、bytes written は 0 でした。
- Bounded Pixiv sync は `max_pages = 4`、`limit = 100`、`media_types = ["photo"]` で raw bookmark items 120 件を collection し、photo targets 100 件を discover、100 items / 624 files を download、1131771564 bytes を write、failed files は 0 件でした。
- Pixiv files は `scanner-friendly-v2` で `/home/ion/projects/mediagent/mediagent-data/library/pixiv/photo/2026/...` に置かれました。
- `library.file.verify` は Telegram と Pixiv 合計 627 files を確認しました: valid 627、missing 0、corrupt 0、unknown 0。
- Filesystem verification は Pixiv files 624、Telegram files 3、`.partial` files 0 を確認しました。
- 同じ bounded input の Pixiv second dry-run は queued downloads 0、already-downloaded items 100 件を skipped でした。

## 未実装または未検証

- Workflow V1 runner
- built-in scheduler
- cron examples
- 実 X OAuth account による live verification
- 実 Reddit OAuth / saved-collection live verification。現在は auth-assisted collection を明示的に再開するまで deferred
- Reddit audio muxing、DASH/HLS manifest handling、complex multi-file `v.redd.it` support
- `reddit.saved.sync`。現在は auth-assisted collection を明示的に再開するまで deferred
- Pixiv localhost callback server
- Instagram feed、saved-post、stories、profile scraping、messaging、posting、comments、likes、follows、broad account collection
- Instagram session status TTL、および checkpoint/2FA/rate-limit/thumbnail-only Reel cases の追加 edge-case fixtures
- visual workflow editor

## 次の推奨作業

File-health-aware repair mode は実装済みで、bounded live repair により resolve 可能な missing files は復元済みです。次の recommended task は、remaining 6 historical Reddit rows の扱いを決めることです。Known missing として残す、cleanup tooling で reset/quarantine する、または Reddit auth/resolver work を再開するまで deferred にします。

Reddit OAuth/saved collection と X live auth verification は deferred legacy/advanced paths として扱います。User が明示的に workflow work を選ばない限り、link-first provider-adapter contract が少なくとももう 1 つの provider adapter または複数回の cron-style runs で安定するまで Workflow V1、built-in scheduling、broad autonomous planning は始めません。
