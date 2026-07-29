# 実装上の注意点

このファイルは次回引き継ぎでまだ重要な caveats だけを記録します。解決済みの履歴は、実装判断に影響しない限り Open に残しません。

## Open

### 1. X OAuth は実装済みだが live-verified ではない

- **Status:** 外部検証が必要。
- **Observed in:** `src/mediagent/platforms/x/`、`src/mediagent/tools/x_tools.py`
- **Current behavior:** X OAuth PKCE、exchange、refresh、status、bookmark collection は実装済みで、fake HTTP / fixture tests があります。repo には実 X OAuth client や user credentials がないため、live verification はまだです。
- **Expected next step:** ユーザー提供の X app credentials で `x.auth.start`、browser authorization flow、`x.auth.exchange`、`x.auth.status`、`x.bookmarks.collect` を順に実行します。

### 2. Sync helper がない platform の download orchestration はまだ manual

- **Status:** 設計上延期。
- **Observed in:** `src/mediagent/tools/download_tools.py`、`src/mediagent/tools/metadata_tools.py`、`src/mediagent/workflows/`
- **Current behavior:** Pixiv と Telegram には deterministic sync helpers があります。Reddit explicit links は experimental Telegram inbox resolver 経由で sync できますが、Reddit saved items と X はまだ collect-only です。Workflow V1 もまだありません。
- **Expected next step:** Explicit-link resolver behavior を harden し、次の platform sync helper を追加するか、Pixiv/Telegram の additional source tools を議論するか、sync contract が安定してから Workflow V1 を始めます。

### 3. Workflow V1 は意図的に延期

- **Status:** 設計上延期。
- **Observed in:** `src/mediagent/workflows/`
- **Current behavior:** Tools は Python と CLI から呼べますが、YAML workflow validation/execution はまだありません。
- **Expected next step:** Deterministic sync behavior が cleanup/recovery tooling 後も安定し、bottom/platform tool contracts が安定してから Workflow V1 を始めます。

## Recently Resolved

- Telegram numeric dialog selectors は直接再利用できるようになりました。`telegram.dialogs.list` は `"3779502941"` のような selector を返すことがあります。Real Telegram entity selector は Telethon lookup の前に numeric strings を integers へ変換し、regression coverage は string IDs、negative channel IDs、saved messages、username selectors を確認しています。
- Phase 18 Reddit video-only explicit-link support は実装済みです。`reddit_media_link` は generic direct-media fallback の前に direct `v.redd.it` MP4 URLs を解決し、Reddit post/legacy pages から explicit `v.redd.it/...DASH_*.mp4` candidates を抽出し、`video` / `v0` / `library/reddit/video/...` に map し、`audio_status: "not_merged"` と `mux_required: true` を記録します。Direct `v.redd.it/<id>` manifest links は引き続き `unsupported_media_type` / `reason: video_manifest_unsupported` で skip します。Audio muxing と full DASH/HLS handling は deferred です。
- Phase 17 Reddit explicit-link resolver foundation は実装済みです。`reddit_media_link` は direct `i.redd.it` images、Reddit post/share links、bounded anonymous HTML、static `over18=1` 付き `old.reddit.com` fallback に対応します。Fake-client tests は direct image resolution、modern markup extraction、JS verification fallback、gallery skip behavior、single-MP4 Reddit video resolution、highest DASH candidate selection、Telegram inbox sync into Reddit layout を覆っています。2026-07-29 UTC の Telegram inbox live verification では Reddit JPEG を `/home/ion/projects/mediagent/mediagent-data/live-test-phase17/library/reddit/photo/2026/07/20260728__reddit__t3_1v8yi6w__p0.jpg` に resolve/download しました。Second-run dedupe は queued downloads 0、`library.file.verify` は 4 live-test files valid を報告しました。
- Phase 16 generic HTML resolver candidate selection は、明確な original/full media URL を preview/thumbnail candidates より優先するようになりました。単一の winner がない場合は引き続き ambiguous skip します。Telegram inbox live verification では valid な Danbooru original PNG を download し、以前に download 済みの nhentai page は dedupe されました。Reddit short-link page は返却 HTML に static media candidates がなかったため skip のままです。
- Phase 16 generic HTML media discovery は domain allowlist なしで実装済みです。単一で明確な public HTML media target、HEAD-forbidden HTML pages、X age/login wall の skip に対応し、default preview images は download しません。Telegram inbox live verification では public HTML test link から valid PNG を 1 件 download し、X link は `requires_auth` として skip しました。
- Phase 16 undocumented Telegram inbox link resolver は experimental boundaries の後ろに実装済みです。`link.resolve.preview`、`link.resolve.to_media_item`、`telegram.inbox.collect_links`、`telegram.inbox.sync_links`、hidden experimental CLI routing、`link_queue` schema v6、origin-source storage metadata、link-safe GET downloads を含みます。
- Phase 16 URL safety は normalization 前に userinfo を拒否し、malformed URLs を structured unsafe skips として扱います。Regression tests は username-only URLs、username/password URLs、invalid ports、extraction skip behavior、resolver preview skip behavior を覆っています。
- Phase 16 experimental tool boundaries は enforce 済みです。Normal `tools list` は experimental tools を隠し、normal inspect/run は拒否し、top-level help は hidden experimental command path を露出しません。
- Phase 16 link sync は link-safe GET path を使います。Redirects を再検証し、byte limits を enforce し、oversized bodies を拒否し、GET 時に MIME を検証し、GET final URL 自体が `.mov` suffix を持つ場合だけ MOV fallback を適用します。
- Reddit Phase 14 foundation は `reddit.auth.start`、`reddit.auth.exchange`、`reddit.auth.refresh`、`reddit.auth.status`、`reddit.saved.collect` として実装済みです。Fake-client tests は auth flows、redaction、generic user-agent rejection、unsafe credential paths、saved-listing normalization、cursor storage、dry-run behavior、media-type filtering、saved comment skip、unsupported embed skip を覆っています。
- Phase 14 Reddit unsafe DB path handling は修正済みです。`reddit.saved.collect` は network work や cursor writes の前に input `db_path` を `context.allowed_write_roots()` で検証し、out-of-root path には `unsafe_db_path` を返します。Regression coverage は外部 SQLite file が作成されないことを確認します。
- Phase 14 Reddit auth failure redaction は修正済みです。`reddit.auth.exchange` / refresh failure payloads は Reddit-specific auth sanitization を通り、`code`、`authorization_code`、`access_token`、`refresh_token`、`client_secret` が redacted されます。Regression coverage は `SECRET_AUTH_CODE` が `ToolResult.to_dict()` に含まれないことを確認します。
- Phase 13E cleanup/recovery foundation は `core.cleanup.media_state` で実装済みです。Dry-run planning、explicit apply confirmation、quarantine-before-DB-reset behavior、credential path protection、selector validation、path-safety tests に対応しています。
- Direct Telegram `download_ref` validation は完了しました。`telegram.media.download` は dry-run または network work の前に direct / nested refs を検証し、chat selector、`message_id`、`media_id` を必須にします。empty、partially populated、missing nested refs の regression coverage も追加済みです。
- Telegram sync cancellation recovery は item boundary で実装済みです。Streaming media download が `.partial` 作成後に cancel された場合、sync は failed file を記録し、item を failed/retryable にし、failed run record を挿入し、partial file を削除し、追加 download を続けず current run を停止します。
- Telegram stream-safe real downloads は実装済みです。Real Telethon adapter は `.partial` に直接書き込み、download call は `timeout_seconds` を enforce し、checksum は chunked で計算します。2026-07-24 UTC に 1 時間の Telegram video download が成功しました。
- Telegram real live verification は現在 phase の目標として完了しました。2026-07-24 UTC に real user session で `telegram.auth.login`、`telegram.auth.status`、curated link-inbox collection、2 件の小さな media downloads、1 件の long video download、scanner-friendly layout placement、`library.file.verify`、second-run dedupe を検証しました。
- Real Telethon client は `telegram.auth.login start` 中に Telethon interactive prompt へ入らなくなりました。Adapter は explicit connect/disconnect boundaries を使います。
- Private Telegram `t.me/c/...` download links は linked media の download 時に numeric `-100...` chat IDs として正しく解決されます。
- Telegram inline 2FA password input は support しません。`telegram.auth.login` public schema は `password_ref` のみを公開し、handler は Telegram に接続する前に raw `password` input を拒否します。Regression coverage は raw value が漏れないことを確認します。
- Localized runbooks に、English runbook と同じ `/tmp` scoped real-download smoke test と cleanup guidance を追加しました。
- `telegram.auth.login` は Telegram user session の two-step local login helper として実装済みです。Tests は start、`password_ref` 付き complete、config なし dry-run、missing code/hash validation、secret redaction を覆っています。
- Telegram curated link-inbox support は `telegram.messages.collect` と `telegram.messages.sync` の `extract_message_links` で実装済みです。Tests は user-controlled inbox channel から message link を抽出し、linked media を解決し、inbox cursor だけを進めることを覆っています。
- Telegram malformed media download validation gap は修正済みです。必要な `download_ref` fields が欠けている場合、`telegram.media.download` は `telegram_download_missing_ref` structured validation failure を返します。
- Telegram Phase 12 media-source foundation は実装済みです。Telethon-backed user-session boundaries、`telegram.auth.login`、`telegram.auth.status`、`telegram.dialogs.list`、`telegram.messages.collect`、`telegram.media.download`、`telegram.messages.sync` を含みます。Tests は fake auth/session status、dialog filtering、protected-content exclusion、album/grouped media normalization、link-inbox extraction、dry-run no writes、Telegram-specific download finalization、deterministic sync、dedupe、partial failure、scoped cursor storage を覆っています。
- Photo-only Pixiv sync cursor semantics は修正済みです。`pixiv.bookmarks.sync` は `limit_truncated` を media-type-filtered item set で判定し、`bookmarks:public:photo` のような scoped cursor を保存します。Regression tests は non-dry-run photo-only cursor storage を覆い、filtered sync が unscoped cursor を変更しないことも確認します。
- Platform-specific library roots に対応しました。Tools は explicit `library_root` / `target_dir`、`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`、`MEDIAGENT_LIBRARY_DIR`、最後に `${MEDIAGENT_DATA_DIR}/library` の順で root を選びます。`storage.path.plan` には platform-specific root の regression coverage があります。
- Formal Pixiv full-bookmark automation gap は解決しました。`pixiv.bookmarks.sync` は committed `max_pages` pagination と `media_types` filtering に対応し、multi-page photo-only dry-run test があります。Live download 後の dry-run と non-dry run では `{"max_pages":20,"media_types":["photo"]}` により 11 pages、raw items 309、photo items 306 を scan し、already-downloaded 306 items をすべて skip、queued downloads は 0 でした。Non-dry run は SQLite に successful tool run を 1 件記録しました。
- Pixiv sync cursor advancement bug は修正済みです。`pixiv.bookmarks.sync` は raw collector が cursor を保存しないようにし、sync boundary で untruncated page が完全成功した場合だけ cursor を保存します。`limit` が collected page を truncate した場合、または run が partial/failed の場合、cursor は進みません。Regression tests は `limit < collected` で cursor が進まないことと、full success で cursor が保存されることを覆います。
- Phase 9 deterministic sync status ownership は `media.item.set_status`、`db.update_media_item_status`、`media_items.downloaded_at`、`src/mediagent/core/sync.py` で実装されました。`pixiv.bookmarks.sync` は file processing 後に parent item status を `downloaded`、`partial`、`failed` に更新します。
- old database に `downloaded_at` がない compatibility bug は `_ensure_media_items_schema()`、`SCHEMA_VERSION = "4"`、`media.item.set_status` の old-v3 regression test で修正されました。
- 初期 `pixiv.bookmarks.sync` の missing-helper runtime crash は修正済みです。sync helpers、`examples/tools/pixiv.bookmarks.sync.json`、dry-run、already-downloaded skip、multi-file success、partial failure、path safety、Pixiv `Referer`、metadata writing、file records、status transitions の tests が追加されました。
- Phase 5 bottom tool hardening は examples、CLI smoke tests、structured error categories、rate-limit metadata、sync cursor helpers、media file helpers、platform-agnostic fixtures まで完了しました。
- Credential tools は `read_credentials` / `write_credentials` を使い、token-bearing output を redact し、explicit credential files に対応し、credential writes を configured write roots 内に制限します。
- `media.file.upsert` は stable non-null `file_key` を使うため、`remote_url` または `local_path` が欠けても idempotent です。
- X generic auth status は `access_token`、`refresh_token`、`scope`、`expires_at` などの semantic keys を持つ credential refs を扱えます。
- `AuthSession.to_dict()` は `refresh_available` などの safe status fields を保持しつつ、metadata secrets を redact します。
- Public auth/X schemas は checked tools に raw `access_token` や raw `refresh_token` input fields を出さなくなりました。`x.bookmarks.collect` は configured credentials を使い、`auth.session.refresh` は `refresh_token_ref` / credential files を使います。
- Pixiv Phase 8 の最初の slice は refresh-token auth、bookmark collection、multi-page normalization、ugoira metadata preservation、credential-file safety checks、fixture tests まで実装済みです。
- `download.http` は custom request headers に対応したため、Pixiv media downloads で `Referer: https://www.pixiv.net/` を付けられます。
- generic `auth.session.status` と `auth.session.refresh` は Pixiv sessions に route するようになり、focused tests があります。
- generic `auth.session.status` は Pixiv `credential_refs` と `refresh_token` などの semantic keys に対応し、X credential-ref path と揃いました。
- generic `auth.session.status` は environment variables と `credential_refs` の両方から usable Pixiv access-token sessions を検証でき、`pixiv.auth.status` と一致しました。
- `pixiv.auth.login` は two-step local OAuth/PKCE helper として実装済みです。start は login URL と code verifier を出力し、exchange は短命 callback URL または raw callback code を受け取り credential file を書きます。Pixiv password は保存しません。
- `.env` と `.env.example` は `PIXIV_CREDENTIALS_FILE` を `pixiv.auth.login` の通常の出力先として説明し、`PIXIV_REFRESH_TOKEN` を optional fallback として明記し、実 token values は追加していません。
- Pixiv login exchange failure payloads は structured error を返す前に、submitted authorization code と upstream `"code"` fields を redact します。
- `pixiv.auth.login` には PKCE start、exchange success、dry-run、unsafe credential path、failed exchange redaction、credential writing の fixture/fake-client coverage があります。
- Pixiv live verification は 2026-07-21 UTC に一度完了しました。user-provided login 後、`pixiv.auth.status` は usable session を返し、`pixiv.bookmarks.collect` は public bookmark items を 30 件返し、`download.http` は JPEG bookmark image を `/home/ion/projects/mediagent/mediagent-data/pixiv/live-test/143734851_p0.jpg` に download しました。checksum は `sha256:72c9988b5d32786423966ff7aae99166041b532571a83f7e4bda1adcd442e2fe` です。
- Localized issue handoffs は現在の英語版 issue state に同期済みです。
- Localized TODO handoffs は Pixiv `pixiv.auth.login` / OAuth PKCE planning update に対応済みで、authorization-code exchange、credential-file writing、redaction tests、skipped-by-default live browser tests を含みます。
- 英語、繁体字中国語、日本語の handoff docs は Pixiv first-slice status に同期済みです。
- default test suite は green です: `uv run --locked python -m unittest discover -s tests` が 160 tests passing です。
