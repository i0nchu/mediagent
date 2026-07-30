# Mediagent 現在の状態

## 実装済み

- Package layout は `src/mediagent/` にあります。
- `main.py` は薄い起動入口です。
- `pyproject.toml` に console script `mediagent = mediagent.cli:main` が設定されています。
- Tool contract は `src/mediagent/core/tooling.py` にあります。
- Tool registry は `src/mediagent/tools/defaults.py` にあります。
- CLI bridge は `src/mediagent/cli.py` にあります。
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
- Deterministic sync helpers は `src/mediagent/core/sync.py` にあります。
- Universal storage planning は `src/mediagent/core/storage.py` にあります。
- Default shared-root storage layout は `scanner-friendly-v2` です: `<platform>/<media_type>/<yyyy>/<mm>/<filename>`。
- `MEDIAGENT_<PLATFORM>_LIBRARY_DIR` による platform-specific library roots に対応しています。例: `MEDIAGENT_PIXIV_LIBRARY_DIR`。
- Platform-specific roots はすでにその platform に scoped されているものとして扱うため、default では追加の platform directory を省略します。
- Pixiv bookmark sync は collect -> upsert -> status filter -> storage path plan -> partial download finalization -> file record -> item status update に対応しています。
- Pixiv bookmark sync は `media_types` filtering 使用時に、`bookmarks:public:photo` のような scoped cursor を保存します。
- Telegram message sync は durable processing が成功した後に、`messages:saved_messages:photo-video` のような per-source scoped cursors を保存します。
- Undocumented Telegram inbox link resolver support は experimental boundaries の後ろに実装済みです。Telegram は ingest provenance として扱い、解決後の `origin_source` を media item と storage layout の platform として使います。
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
- `pixiv.bookmarks.collect`
- `pixiv.bookmarks.sync`
- `instagram.auth.login`
- `instagram.auth.status`
- `instagram.auth.ensure_session`
- `instagram.link.resolve`
- `telegram.auth.login`
- `telegram.auth.status`
- `telegram.inbox.collect_links`（experimental）
- `telegram.inbox.sync_links`（experimental）
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
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --dry-run --json
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --dry-run --json
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

最新の local full suite は 187 tests passing です。

Phase 16 Telegram inbox link resolver verification:

- `link.resolve.preview`、`link.resolve.to_media_item`、`telegram.inbox.collect_links`、`telegram.inbox.sync_links` は experimental tools として実装済みです。
- 通常の `mediagent tools list` は experimental tools を隠し、`--include-experimental` を指定した場合だけ表示します。
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
- Telegram real downloads は `.partial` に直接 stream し、Telethon download call に `timeout_seconds` を enforce し、checksum は chunked で計算し、atomic move で finalize します。

Deterministic Pixiv sync verification:

- `pixiv.bookmarks.sync` は fake-client tests で multi-file success、second-run skip、dry-run no writes、partial failure、path safety、Pixiv `Referer`、scanner-friendly storage layout、file records、item status updates、安全な cursor advancement を覆っています。
- `pixiv.bookmarks.sync` には、photo-only sync が media-type filtering 後も cursor を保存する regression coverage があります。
- `storage.path.plan` には platform-specific library roots の regression coverage があります。
- `storage.path.plan` と `pixiv.bookmarks.sync` には `scanner-friendly-v2` platform layer と、platform-specific roots の下で duplicate platform directories を避ける regression coverage があります。
- `media_items.downloaded_at` がない old-style SQLite DB は `core.db.init` / tool initialization で migration され、`media.item.set_status` が downloaded state を更新できます。

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
- LLM Agent Core
- visual workflow editor

## 次の推奨作業

Phase 20 Instagram explicit-link foundation は完了済みです。次の implementation focus は Phase 21 Pixiv explicit artwork-link resolution で、shared link-first pipeline を使います。

Reddit OAuth/saved collection と X live auth verification は deferred legacy/advanced paths として扱います。User が明示的に workflow work を選ばない限り、link-first provider-adapter contract が少なくとももう 1 つの provider adapter または複数回の cron-style runs で安定するまで Workflow V1 や Agent Core は始めません。
