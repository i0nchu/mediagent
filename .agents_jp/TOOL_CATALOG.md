# Mediagent Tool Catalog

現在登録されている tools を理解するためのカタログです。正確な schema は次で確認します。

```bash
uv run --locked mediagent tools inspect <tool-name> --json
```

tool を実行するには:

```bash
uv run --locked mediagent tools run <tool-name> --input examples/tools/<tool-name>.json --json
```

safe preview に対応する tool では `--dry-run` を付けます。

## Agent Core V1 Commands

Agent Core V1 は local preview で、SKILL allowlists を通じて同じ `ToolRegistry` を呼びます。Separate platform layer でも scheduler でもありません。

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect <skill-name> --json
uv run --locked mediagent agent run "<natural language task>" --dry-run --json
uv run --locked mediagent agent run "<natural language task>" --json
```

Built-in SKILLs:

- `explicit_link_download`
- `instagram_link_download`
- `instagram_saved_sync`
- `library_health_check`
- `pixiv_bookmark_sync`
- `telegram_inbox_download`

明確に一致する SKILL がない場合、Agent run は tool call 前に unsupported tasks を拒否します。Ollama transport failures は structured `llm_request_failed` として返り、user が明示していない destination path fields は strip されます。

## Auth Tools

- `auth.session.status`: provider session が利用可能か確認します。secrets は出力しません。`provider: "x"` では X auth status に委譲します。
- `auth.session.refresh`: platform adapter 経由で session を refresh します。X では `credential_output_path` または `X_CREDENTIALS_FILE` に書き込めます。
- `auth.session.revoke`: 明示的な local credential revocation guidance を返します。remote session を自動 revoke せず、確認が必要です。

## Core Tools

- `core.env.check`: 必要な環境変数と設定パスを検証します。
- `core.db.init`: SQLite を初期化し、runs、media items、media files、sync cursors、auth sessions、future workflows 用の tables を作ります。
- `core.cleanup.media_state`: Conservative live-test media-state cleanup を plan または apply します。Planning mode は files や SQLite を変更しません。Apply mode は `confirm: true` を要求し、existing media files を quarantine に移動してから matching media file rows を削除し、matching media items を `discovered` に reset します。Credential paths は保護され、actionable cleanup files にはなりません。
- `core.path.prepare`: 安全な file path を解決して検証し、allowed roots の外への書き込みを拒否します。
- `core.run.record`: tool/workflow run summary を SQLite に保存します。保存前に secrets を redacts します。
- `core.sync_cursor.get`: platform sync cursor を読み取ります。
- `core.sync_cursor.set`: platform sync cursor を書き込みます。

## Media Tools

- `media.item.upsert`: `platform + remote_id` で discovered media item を upsert します。
- `media.item.filter_new`: download 前に known、downloaded、failed、skipped、new items を分類します。
- `media.item.set_status`: known media item の parent status を明示的に更新します。deterministic sync helpers は file downloads 完了後にこの path で `downloaded`、`partial`、`failed` を設定します。
- `media.file.upsert`: local media file の remote URL、local path、library-relative path、storage layout、MIME type、byte size、checksum、health、status を記録します。

対応 media types:

- `photo`
- `video`
- `audio`

## Storage And Library Tools

- `storage.path.plan`: 一つの normalized media file に対して deterministic scanner-friendly library path を計画します。Default layout は `<platform>/<storage_category>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>` です。Storage category は通常 media type と同じで、Pixiv manga source pages は `comic-pages` を使います。Library root は explicit `library_root`、`MEDIAGENT_<PLATFORM>_LIBRARY_DIR`、`MEDIAGENT_LIBRARY_DIR`、最後に `${MEDIAGENT_DATA_DIR}/library` の順で解決します。Platform-specific root はすでに一つの platform に scoped されているため、default では duplicate platform directory を追加しません。
- `library.file.verify`: SQLite の known file records をもとに local existence、size、checksum を確認し、file health を `valid`、`missing`、`corrupt`、`unknown` に mark します。Files を削除せず、source platforms に接続しません。

## Download And Metadata Tools

- `download.http`: 一つの remote file を安全な path に download します。dry-run、bounded attempts、`.partial` finalization、checksum、MIME/content-length validation、custom request headers、rate-limit metadata に対応します。
- `metadata.write`: normalized JSON metadata を downloaded files の横に書き込みます。保存前に secrets を redacts します。

## Pixiv Tools

- `pixiv.auth.login`: 明示的な local Pixiv OAuth/PKCE setup を開始または完了します。`code` または `callback_url` がない場合は login URL と code verifier を返し、`code` または `callback_url` に `code_verifier` を添えて渡すと tokens に交換して configured write roots 内の credential JSON に書けます。
- `pixiv.auth.status`: Pixiv credentials が利用可能か確認します。secrets は出力しません。user ID 付きの usable access token を検証でき、refresh token しかない場合は refresh が成功するか確認しますが、credential file は書きません。
- `pixiv.auth.refresh`: 明示的に提供された refresh token で Pixiv App API credentials を更新し、configured write roots 内の credential JSON に書けます。
- `pixiv.link.resolve`: user-provided Pixiv artwork URL または `illust_id` を normalized downloadable media candidates に resolve しますが、file は download しません。Configured Pixiv session を使い、自分で browser login は開始しません。login/refresh が必要な場合は `pixiv_auth_missing_credentials` のような structured auth errors を返します。
- `pixiv.bookmarks.collect`: configured account の Pixiv bookmarked illustrations/manga を収集し、single-page、multi-page、ugoira metadata を normalize し、cursor を SQLite に保存できます。
- `pixiv.bookmarks.sync`: Pixiv bookmarks を collect し、media items を upsert/filter し、scanner-friendly storage paths を plan し、各 `metadata.files[]` file を `.partial` finalization 付きで download し、local media files を記録して parent item status を更新します。Pixiv work type と physical media type は分離され、official manga source pages は photo files のまま `work_type:comic` と `comic-pages` storage category を使い、multi-page `illust` は `photo` に保存します。`package_comics:true` は newly downloaded complete manga ごとに `comic` 下へ一つの CBZ を作ります。Invisible/placeholder-only works は download しません。`repair_missing_files:true` は DB downloaded でも required files が missing/corrupt/unhealthy、または `local_path` に存在しない items を明示的に repair します。Default reruns は conservative のままです。Timer-style recurring sync は `stop_on_known:true` と bounded `max_pages`、explicit full rebuild は `full_sync:true`、no `limit/max_pages`、`stop_on_known:false` を使います。
- `pixiv.library.reconcile`: Offline legacy Pixiv library migration を plan/apply します。`work_type` / storage metadata を更新し、existing manga source files と sidecars を `photo` または legacy `comic` から `comic-pages` へ atomic move し、SQLite file paths を更新し、known `s.pximg.net/.../limit_*.png` placeholder downloads を quarantine してから file rows を削除します。Apply は `confirm:true` が必要で、`.trash` content は自動復元しません。
- `pixiv.comics.package`: Complete downloaded Pixiv manga ごとに deterministic CBZ を plan/create します。Page order は Pixiv metadata に従い、archive は zero-padded page names と `ComicInfo.xml` を含み、`.partial` と atomic replacement で書き込み、`comic-cbz-v1` として `media_files` に記録します。Healthy tracked CBZ は reuse し、Pixiv への接続や source pages の削除は行いません。

Pixiv collector は file を download しません。bookmark 全体の deterministic download には `pixiv.bookmarks.sync` を使います。Explicit artwork URLs は `link.media.sync` に直接渡せます。この path は 1 artwork URL を 1 media item として扱い、default で全 pages を resolve し、`pixiv.bookmarks.sync` と dedupe し、download 時に必要な Pixiv `Referer` を適用し、runtime headers を metadata に保存しません。単一 file を手動 download する場合は、戻り値の `metadata.files[].url` を `download.http` に渡します。Pixiv 画像 download には通常次が必要です。

```json
{"Referer":"https://www.pixiv.net/"}
```

## Telegram Tools

Telegram は notification、forwarding、chat-management platform ではなく media source として扱います。実装は Telethon-compatible user MTProto session boundary を使います。Telegram session file は credential です。

- `telegram.auth.login`: 明示的な local Telegram user-session login を開始または完了します。`start` は Telegram login code を要求し、第二 step に必要な `phone_code_hash` を返します。`complete` は login code と `phone_code_hash` を受け取り、`password_ref` による optional 2FA を support し、configured session file だけを allowed credential/data roots の下に書きます。
- `telegram.auth.status`: configured Telegram user-session credentials を検証し、secrets は出力しません。`TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE` / `MEDIAGENT_DATA_DIR` を確認し、安全な session/account status だけを返します。
- `telegram.dialogs.list`: configured user session から見える selectable dialogs を列挙します。safe chat identifiers、display titles、chat type、username、access hints を返し、message text や media bytes は返しません。
- `telegram.messages.collect`: explicit chats または message links から media-bearing messages を収集し、photos、videos、audio、voice/audio documents、media documents を shared media items に normalize します。Per-source cursors を読んで保存できますが、media bytes は download しません。`extract_message_links: true` を指定すると、collected message text/captions 内の Telegram message links を scan し、linked original messages を解決して、その media も normalize します。Linked source cursors は進めません。
- `telegram.media.download`: Telegram client boundary を通して一つの Telegram media object を安全な local path に download します。dry-run、`.partial` への direct streaming、checksum、MIME validation、finalization、timeout enforcement、path safety に対応します。
- `telegram.messages.sync`: selected Telegram media messages を collect し、media items を upsert/filter し、scanner-friendly storage paths を plan し、`telegram.media.download` で media を download し、local media files を記録し、parent item status を更新します。Per-source scoped cursors は durable processing 成功後だけ進めます。

Default source selection は explicit です。Saved Messages、private collection channel、allowlisted group/channel、explicit message links など trusted chat selector を使ってください。すべての dialogs を default で scan しません。

Curated Telegram usage では、`chat` を user の private collection channel に向け、`extract_message_links: true` を設定します。Configured user session が各 linked original message に access できる必要があります。Mediagent は protected または inaccessible chats を bypass しません。

Small curated media download、1 時間 linked video download、scanner-friendly layout placement、`library.file.verify`、rerun dedupe は 2026-07-24 UTC に live-verified 済みです。

## Core Link Tools

これらの tools は Phase 19 link-first product path の stable entry points です。

- `link.queue.upsert`: Explicit URLs を 1 件以上 queue し、normalized URL dedupe と source provenance merge を行います。Media の resolve や download は行いません。
- `link.media.sync`: Explicit URLs または queued link records を resolve し、cron/daemon runs では ready queued records を claim し、retryable deferred records を schedule し、clear media candidates を normalized media items に変換し、known items を dedupe し、storage paths を plan し、files を download し、optional sidecar metadata を書き、media-file state を記録し、parent item status を更新します。`retry_auth_skipped:true` は platform session が usable になった後で旧 `requires_auth` / `login_wall` rows を明示的に reclaim します。

Public CLI shortcut:

```bash
mediagent link sync <url> --json
```

この shortcut は `link.media.sync` に delegate します。User-provided links の stable non-Telegram entry point です。

`link.media.sync` は deterministic で、Python、CLI、cron、workflows、future Agent/SKILL integrations から呼び出せます。Writes は configured project-local roots 配下に制限し、resolver candidates の credential-bearing headers を永続化してはいけません。

Dedicated resolver を持つ known platform page domains は platform resolver のために reserve され、generic fallback には渡しません。Unsupported Instagram pages、Pixiv non-artwork pages、Imgur gallery/album-style pages は `generic_html_media` ではなく structured platform skips を返します。

## Instagram Tools

Instagram support は explicit post/Reel links と configured account の saved-media feed に対応します。Stories、profiles、messages、comments、likes、follows、broader account activity は scan しません。

1 件の Instagram post URL は post 全体を表します。Carousel posts は default ですべての media resources を download します。`img_index` は future explicit option がない限り source metadata としてのみ保持します。Signed Instagram CDN URLs は runtime-only download data であり、SQLite、sidecar metadata、logs、snapshots、tool output に persist してはいけません。

- `instagram.auth.status`: Configured saved Instagram session を検証します。Cookies、session IDs、username、password、raw private API payloads は露出しません。Session path は fake-client callbacks、real-client loads、network work の前に project-local roots boundary で検証されます。
- `instagram.auth.login`: Explicit local username/password credentials から saved local Instagram session を作成または置換します。Session file は allowed credential/data roots 配下に置き、restrictive permissions で書きます。
- `instagram.auth.ensure_session`: Saved session を確認し、credentials があり cooldown が許す場合だけ low-frequency relogin を試みます。Checkpoint と 2FA は automation を停止し、rate-limit と temporary-block states は work を defer します。
- `instagram.link.resolve`: Public Instagram `/p/<shortcode>/`、`/reel/<shortcode>/`、`/tv/<shortcode>/` URL 1 件を normalized media candidates に resolve します。Password login は自分で行いません。Non-Instagram hosts または shortcode missing は `instagram_media_unsupported` を返し、missing/invalid session は agent-decidable auth errors を返します。
- `instagram.saved.collect`: Configured saved session から saved posts を sequentially 読み、files を download せず normalized whole-post items を返します。Bounded `limit` / `max_pages` と explicit full pagination を support し、opaque cursors、session errors、checkpoints、rate limits は structured のまま、runtime CDN URLs は public output から除外します。
- `instagram.saved.sync`: Saved-feed collection と共通 SQLite dedupe、scanner-friendly storage、downloads、file/item status、retry、missing-file repair を組み合わせます。Recurring runs は `stop_on_known:true` と conservative page cap、explicit full sync は架空の item/page limit なしで `full_sync:true` を使います。

## Experimental Link Tools

これらの tools は public preview/compatibility story が決まるまで experimental helper surfaces として扱います。List には `--include-experimental`、inspect/run には `--allow-experimental` を使います。

- `link.resolve.preview`: Explicit URL 1 件を download せず安全に preview します。Direct media、bounded single-media HTML、実装済みの小さな provider-specific resolver behavior に対応します。
- `link.resolve.to_media_item`: Resolved link candidate を normalized media item に変換し、既存 storage/download pipeline に渡します。

現時点ではこれらの experimental names を stable public API として扱わないでください。Promotion では既存 live-test commands の aliases を保持し、examples、この catalog、`RUNBOOK.md`、localized handoff files を同時に更新する必要があります。

## Agent-Only Low-Profile Skills

- `telegram.inbox.sync_links`: selected inbox の `full_sync:true` full-source scan をサポートします。この mode では default 100-message scan limit を適用しません。URL/media/file dedupe は引き続き tool layer が扱います。Inbox の `t.me` / `telegram.me` message links は inbox provenance を保持して Telegram native message sync に bridge され、protected/inaccessible links は structured skip になります。External URLs は link resolver pipeline を使い続けます。`retry_auth_skipped:true` は旧 Telegram-ingested auth-dependent rows を再試行します。

- `telegram_inbox_download`: Agent Core が configured Telegram inbox を処理できるようにしますが、direct tool entry points は public workflow commands として文書化しません。Underlying tools は hidden stable surfaces です。Default list には表示されませんが、名前を知っている user/agent は呼び出せます。

## Reddit Tools

Reddit auth/saved tools は存在しますが、現在は deferred legacy/advanced capability です。現在の product direction は explicit-link resolution で、anonymous/bounded behavior を優先し、Redgifs を次の no-auth provider foundation として扱います。User が明示的に auth-assisted account collection を再開しない限り、saved collection を土台に開発しないでください。

Saved-collection slice は OAuth identity/history data だけを読み、direct media candidates を collect します。Posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping、third-party extractors は実装しません。

- `reddit.auth.start`: Reddit OAuth authorization URL を生成します。Default scopes は `identity` と `history` です。
- `reddit.auth.exchange`: Reddit OAuth callback code を tokens に交換し、credential JSON を configured write roots 内に書けます。Raw tokens、client secrets、authorization codes は出力しません。
- `reddit.auth.refresh`: `REDDIT_CREDENTIALS_FILE` または `refresh_token_ref` から Reddit OAuth access credentials を refresh します。Reddit が新しい refresh token を返さない場合は既存 refresh token を保持します。
- `reddit.auth.status`: configured Reddit access token を検証し、安全な account/status response を返します。
- `reddit.saved.collect`: `username` または `me` の media-bearing saved items を collect し、`after` pagination と optional cursor storage に対応し、shared media items に normalize します。Files は download せず、media-file records も書きません。

First-version parser は Reddit-hosted single images、Reddit gallery images、Reddit-hosted video fallback URLs、stable file extensions を持つ direct external image/video URLs に対応します。Unsupported embeds と direct media がない comments は skipped です。

## X Tools

- `x.auth.start`: X OAuth 2.0 PKCE authorization URL、state、code verifier、challenge を生成します。
- `x.auth.exchange`: authorization code を token に交換します。戻り値は redacted session metadata のみです。raw tokens は `credential_output_path` または `X_CREDENTIALS_FILE` に書けます。
- `x.auth.refresh`: X OAuth tokens を refresh します。X が新しい refresh token を返さない場合、既存の refresh token を保持します。
- `x.auth.status`: `/2/users/me` で token、expiration、scopes、authenticated user ID を検証します。
- `x.bookmarks.collect`: authenticated user の media-bearing bookmarks を取得し、media items に normalize し、rate-limit metadata を返し、pagination cursor を SQLite に保存できます。

## Credential Notes

- X credentials は `X_ACCESS_TOKEN` / `X_REFRESH_TOKEN`、または `X_CREDENTIALS_FILE` から読めます。
- Pixiv credentials は `PIXIV_CREDENTIALS_FILE`、`PIXIV_REFRESH_TOKEN`、または `PIXIV_ACCESS_TOKEN` から読めます。初回 local setup では `pixiv.auth.login` を優先します。
- Telegram credentials は `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE` から読めます。Session file は credential であり、`${MEDIAGENT_DATA_DIR}/credentials/` の下に置くべきです。初回 local setup では `telegram.auth.login` を優先します。
- Reddit credentials は `REDDIT_CREDENTIALS_FILE` または token environment variables から読めます。Deferred auth-assisted path を明示的に検証する場合だけ `reddit.auth.start` + `reddit.auth.exchange` を使い、必ず unique descriptive `REDDIT_USER_AGENT` を使います。
- Instagram credentials は `INSTAGRAM_ACCOUNT`、`INSTAGRAM_SECRET`、`INSTAGRAM_SESSION_FILE` から読めます。Session file は credential であり、`${MEDIAGENT_DATA_DIR}/credentials/` の下に置くべきです。Link または saved-media sync の前には `instagram.auth.ensure_session` を優先し、explicit local session creation の場合だけ `instagram.auth.login` を使います。
- `X_CREDENTIALS_FILE`、`PIXIV_CREDENTIALS_FILE`、`TELEGRAM_SESSION_FILE`、`REDDIT_CREDENTIALS_FILE`、`INSTAGRAM_SESSION_FILE` はユーザーが明示的に管理する file を指すべきです。
- token exchange と refresh の出力に raw tokens は含めません。
- SQLite run records に raw access tokens、refresh tokens、cookies、sessions、bot tokens を保存してはいけません。
