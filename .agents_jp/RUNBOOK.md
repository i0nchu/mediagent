# Mediagent Runbook

## Local comic live test

最初の live test は repo 内の開発 path のみを使う。

```bash
export MEDIAGENT_DATA_DIR=/home/ion/projects/mediagent/tmp/live/comics
export MEDIAGENT_LIBRARY_DIR=/home/ion/projects/mediagent/tmp/live/comics/library
export MEDIAGENT_DB_PATH=/home/ion/projects/mediagent/tmp/live/comics/mediagent.sqlite3
export MEDIAGENT_NHENTAI_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/nhentai_session.json
export MEDIAGENT_JMCOMIC_SESSION_FILE=/home/ion/projects/mediagent/tmp/live/comics/credentials/jmcomic_session.json
mkdir -p "$MEDIAGENT_DATA_DIR/credentials" "$MEDIAGENT_LIBRARY_DIR"
uv run --locked mediagent tools run core.db.init --json
```

`.env` に数字で始まる旧変数が残っている場合は `source .env` しない。先に `MEDIAGENT_JMCOMIC_USERNAME`／`MEDIAGENT_JMCOMIC_PASSWORD` へ変更する。

```bash
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.nhentai.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.nhentai.json --json
uv run --locked mediagent tools run nhentai.auth.status --input examples/tools/nhentai.auth.status.json --json
uv run --locked mediagent tools run nhentai.auth.refresh --input examples/tools/nhentai.auth.refresh.json --json
uv run --locked mediagent tools run nhentai.favorites.collect --input examples/tools/nhentai.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run nhentai.favorites.sync --input examples/tools/nhentai.favorites.sync.json --dry-run --json
```

nhentai favorites では login 済み browser から cookie jar を一度 export する。Mediagent JSON session のほか、Netscape 形式 `cookies.txt` を `MEDIAGENT_NHENTAI_COOKIE_FILE` で指定できる。refresh は形式を維持し、permission を `0600` にする。username/password、CAPTCHA、proof-of-work は自動化しない。

tool result には二つの意味層がある。top-level `status` は要求した tool operation が成功したかを示す。`auth.status` inspection の top-level success は inspection 完了だけを意味し、login 成功を意味しない。`data.auth_status`、`data.authenticated`、`data.reusable`、`data.remote_verified` を確認する。local で load 可能だが provider に未検証の session は `authenticated: null`、`remote_verified: false` を返す。

browser から import した session は authenticated favorites が利用可能でも、nhentai refresh endpoint から HTTP 403 を受ける場合がある。`nhentai.auth.refresh` は診断用に read-only favorites check を行うが、refresh operation 自体は top-level failure とし、`error.code: nhentai_refresh_rejected` を返す。data は rotation 失敗と current auth が利用可能であることを明示する。check も失敗した場合は `nhentai_auth_required` となり、browser cookie の再 export が必要である。

JMComic は設定済み username/password から直接 reusable session を作成でき、browser cookie は必須ではない。`JMCOMIC_USERNAME`、`JMCOMIC_PASSWORD`、`JMCOMIC_SESSION_FILE` も受け付け、推奨の `MEDIAGENT_JMCOMIC_*` が同時に存在する場合はそちらを優先する。`jmcomic.auth.login` は invalid な旧 session を無視して置き換え、login request 前に失敗しない。

`jmcomic.favorites.collect` と `.sync` は `jmcomic_auth_required` に対し、run ごとに最大 1 回 configured credential login で recovery する。Recovered session は即時保存し、rotated cookie は collection と各 album resolve 後に checkpoint する。Summary の `auth_recovery_attempted`、`auth_recovered`、`session_checkpointed` で確認でき、session 内容は出力しない。System JMComic service は initial full sync に 18 時間を許可する。

JMComic favorite folder selection は optional `folders` array を使う。各要素は remote folder name、local registered name、numeric FID、または trusted `18comic.vip/.../favorite/albums?folder=<id>` URL にできる。省略時は `MEDIAGENT_JMCOMIC_FAVORITE_FOLDERS`（JSON array または comma-separated list）を使い、それもない場合は aggregate `all`（FID `0`）になる。複数 folder は union/dedupe され、全 folder の complete collection が成功した時だけ一つの atomic snapshot を commit する。選択を変えると新 union にない旧 membership は inactive になるが、既存 files/CBZ は削除しない。`follow` の範囲は committed union 内の active albums だけである。

```bash
uv run --locked mediagent tools run jmcomic.auth.status --input examples/tools/jmcomic.auth.status.json --json
uv run --locked mediagent tools run jmcomic.auth.login --input examples/tools/jmcomic.auth.login.json --json
uv run --locked mediagent tools run jmcomic.favorites.folders.collect --input examples/tools/jmcomic.favorites.folders.collect.json --json
uv run --locked mediagent tools run jmcomic.favorites.folders.register --input examples/tools/jmcomic.favorites.folders.register.json --json
uv run --locked mediagent tools run jmcomic.favorites.folders.list --input examples/tools/jmcomic.favorites.folders.list.json --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.jmcomic-album.json --dry-run --json
uv run --locked mediagent tools run comic.link.sync --input examples/tools/comic.link.sync.jmcomic-album.json --json
uv run --locked mediagent tools run jmcomic.favorites.collect --input examples/tools/jmcomic.favorites.collect.json --dry-run --summary-json
uv run --locked mediagent tools run jmcomic.favorites.sync --input examples/tools/jmcomic.favorites.sync.json --dry-run --json
```

Optional alternative として、Netscape browser export を `MEDIAGENT_JMCOMIC_COOKIE_FILE` または `JMCOMIC_COOKIE_FILE` で指定できる。`*_SESSION_FILE` を `.txt`／`.cookies` path に直接向けてもよい。Trusted JMComic domains の cookies だけを import し、後の書き戻しも Netscape format と mode `0600` を維持する。cookie-file と session-file の両方を設定した場合は cookie-file を優先する。

同じ二回目の実行は healthy page を 0 件 download し、existing CBZ を報告すること。直接 JM album は follow を作らず、favorite sync のみが作る。実行中に SQLite `-wal`／`-shm` を削除しない。

follow は常駐 daemon ではなく、timer が `jmcomic.favorites.sync` を定期的に再実行することで実現する。complete selected-folder union snapshot で active membership を更新し、その union 内の各 active album を再 resolve して新章を発見する。system-level example は `deploy/systemd/system/` にあり、`/data/services/mediagent` unit は `server` account と明示的な `HOME`／`PATH`、shared non-blocking run lock、compact `--summary-json` journal output を使う。`nhentai.favorites.sync` も同じ timer pattern で新しい exact favorite gallery を発見できるが、series を推測／follow しない。

Filename-hash descramble fix 前に download した JMComic page が horizontal band reorder を示す場合、file は存在して DB で healthy のため `repair_missing_files` だけでは直らない。`mediagent link sync '<album-url>' --overwrite --json` でその exact album を明示的に再 download し、CBZ を rebuild する。`.partial` と atomic replacement を使うため、server deployment 前に local image/CBZ を確認する。

JMComic chapter manifest には、valid だがほぼ空で height が calculated scramble segment count より小さい WebP が含まれる場合がある。Mediagent はこの non-content strip を `media_files.status=skipped`／`file_health=ignored_spacer` として記録し、source file を書かず、CBZ／`ComicInfo.xml` page count に含めず、missing-file repair でも再試行しない。`summary.files_skipped` が件数を返す。Malformed image または normal-sized decode failure は引き続き error になる。

JMComic album chapter number は complete album episode manifest を authoritative source とする。Photo payload は lagging して chapter 1 に見える場合がある。Duplicate provider sort には `55.001` のような stable suffix を使い、Kavita が異なる photo ID を merge するのを防ぐ。最初に configured local development DB/library を read-only audit する。

```bash
uv run --locked mediagent tools run jmcomic.library.reconcile \
  --input examples/tools/jmcomic.library.reconcile.plan.json --json
```

`summary.blocked`、`failed_albums`、`missing_from_manifest`、item paths を確認する。範囲を限定する場合は input を copy して `album_id` または `album_ids` を追加する。Clean plan の後だけ apply する。

```bash
uv run --locked mediagent tools run jmcomic.library.reconcile \
  --input examples/tools/jmcomic.library.reconcile.apply.json --json
```

Apply は media を download しない。Existing metadata を更新し、complete/healthy local source pages から affected CBZ を atomic build し、replaced archive を `library/.trash/mediagent-jmcomic-reconcile/<run-id>/` に移す。すでに `.trash` にある files は触らず、復元しない。Series `Count` の変化だけなら DB metadata のみ更新し、全 archive を rewrite しない。これは development project workflow であり Production mutation の許可ではない。将来の Production run は別 approval、overlapping JMComic sync／Kavita activity の停止、Production plan review、one-time apply、Kavita rescan が必要である。

Telegram inbox と future custom inbox は provider-specific comic command を個別に呼ぶ必要がない。対応 nhentai/JMComic links は shared `link.media.sync` intake を通り、generic HTML resolution より前に exact comic adapter へ自動 dispatch される。そのため inbox の direct comic link は linked work だけを download/package し、series follow は有効にしない。`summary.comic_links_considered` と CBZ counters で dispatch を確認できる。

## 環境

Python 3.12+ を使います。

推奨コマンド:

```bash
uv run --locked ...
```

ローカル開発時の fallback:

```bash
PYTHONPATH=src python3 -m mediagent ...
```

## Managed Trash と Immich Cleanup

Mediagent service と同じ account で namespace を inspect/create します。

```bash
uv run --locked mediagent library trash status --library-root /data/nas/mediagent --json
uv run --locked mediagent library trash prepare --library-root /data/nas/mediagent --json
```

Legacy `.trash` parent が writable でない場合、administrator は service account 用の
`.trash/mediagent` だけを pre-create し、legacy trash owner を recursive に変更しません。
Review 済み Immich bridge は `deploy/integrations/immich/` にあり、sync services と同じ
flock の下で `mediagent library remove` を呼び、direct file move は行いません。V1 に
automatic purge はありません。

## テスト実行

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## lockfile 確認

```bash
uv lock --check
```

## CLI Smoke Checks

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect core.env.check --json
uv run --locked mediagent tools inspect x.bookmarks.collect --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools run x.auth.start --input examples/tools/x.auth.start.json --json
```

## Agent Core V1 Smoke Checks

Agent Core V1 は default で Ollama を使います:

```bash
export MEDIAGENT_LLM_PROVIDER=ollama
export MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434
export MEDIAGENT_OLLAMA_MODEL=qwen3:8b
```

Built-in SKILL を inspect します:

```bash
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
```

Explicit-link task を preview します:

```bash
uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --dry-run --json
```

Unsupported tasks は any tool call の前に failure になるべきです:

```bash
uv run --locked mediagent agent run "我目前有存在的 telegram inbox 嗎？" --dry-run --json
```

Expected shape: `status:"failure"`、`error.code:"unsupported_task"`、`skill:null`、tool steps なし。

LLM transport failures は Python traceback ではなく structured result にします:

```bash
MEDIAGENT_OLLAMA_BASE_URL=http://127.0.0.1:9 MEDIAGENT_OLLAMA_TIMEOUT_SECONDS=0.2 \
  uv run --locked mediagent agent run "download https://example.com/media.jpg" --skill explicit_link_download --json
```

Expected shape: `status:"failure"` with `error.code:"llm_request_failed"`。

## 一時 database の初期化

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run core.db.init --json
```

## Cleanup / Recovery の preview

Cleanup を apply する前に必ず preview します。Planning mode は files や SQLite を変更しません。

```bash
uv run --locked mediagent tools run core.cleanup.media_state \
  --input examples/tools/core.cleanup.media_state.json --json
```

Apply mode は `confirm: true` を要求し、matching media state を reset する前に files を quarantine します。

```bash
printf '{"mode":"apply","platform":"pixiv","status":"downloaded","confirm":true}' \
  | uv run --locked mediagent tools run core.cleanup.media_state --input - --json
```

## filesystem operation の dry-run

```bash
printf '{"path":"${MEDIAGENT_DATA_DIR}/pixiv","kind":"directory","create":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-data uv run --locked mediagent tools run core.path.prepare --input - --dry-run --json
```

## Real download smoke test

Smoke tests は `/tmp` にだけ書きます。

```bash
printf '{"url":"https://example.com/","target_path":"/tmp/mediagent-download-test/example.html","expected_mime_prefix":"text/html","overwrite":true}' \
  | MEDIAGENT_DATA_DIR=/tmp/mediagent-download-test uv run --locked mediagent tools run download.http --input - --json
```

Cleanup:

```bash
rm -rf /tmp/mediagent-download-test /tmp/mediagent.sqlite3
```

## X OAuth setup shape

OAuth credentials を commit してはいけません。local testing では credential file を `MEDIAGENT_DATA_DIR` 配下に置きます。

```bash
export MEDIAGENT_DATA_DIR=/tmp/mediagent-data
export X_CREDENTIALS_FILE="$MEDIAGENT_DATA_DIR/credentials/x-oauth.json"
```

または local `.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

authorization URL を生成:

```bash
uv run --locked mediagent tools run x.auth.start --json
```

browser callback で code を取得したら、`examples/tools/x.auth.exchange.json` を元に input を作成して実行します。

```bash
uv run --locked mediagent tools run x.auth.exchange --input examples/tools/x.auth.exchange.json --json
```

session を確認:

```bash
uv run --locked mediagent tools run x.auth.status --input examples/tools/x.auth.status.json --json
```

bookmarks を収集:

```bash
MEDIAGENT_DB_PATH=/tmp/mediagent.sqlite3 \
  uv run --locked mediagent tools run x.bookmarks.collect --input examples/tools/x.bookmarks.collect.json --json
```

## Pixiv local login and live test

Pixiv V1 は明示的な local OAuth/PKCE helper に対応しています。browser profile を取得せず、password を保存せず、ユーザーが refresh token を手動で探す必要もありません。

`.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

Pixiv login URL と PKCE verifier を生成します:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input examples/tools/pixiv.auth.login.start.json --json > /tmp/pixiv-login-start.json
```

返された `data.authorization_url` を browser で開きます。Pixiv login 完了後、callback URL 全体を copy するか、`code` query parameter だけを copy します。callback URL の形は次の通りです:

```text
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?...&code=...
```

`examples/tools/pixiv.auth.login.exchange.json` を copy して exchange input を作り、次を置き換えます:

- `callback_url`: callback URL 全体。あるいは `code` を使い、短命 callback code だけを入れます。
- `code_verifier`: `/tmp/pixiv-login-start.json` の `data.code_verifier`。
- `credential_output_path`: `MEDIAGENT_DATA_DIR` 配下の path。

callback URL/code を交換し、local credential file を書きます:

```bash
uv run --locked mediagent tools run pixiv.auth.login --input /tmp/pixiv-login-exchange.json --json
```

credential file の想定位置:

```text
$MEDIAGENT_DATA_DIR/credentials/pixiv-oauth.json
```

既に refresh token がある場合、従来の refresh-token path も使えます。`.env` に入れます。

```bash
PIXIV_REFRESH_TOKEN=...
PIXIV_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/pixiv-oauth.json
```

credential file を作成または更新:

```bash
uv run --locked mediagent tools run pixiv.auth.refresh --input examples/tools/pixiv.auth.refresh.json --json
```

session を確認:

```bash
uv run --locked mediagent tools run pixiv.auth.status --input examples/tools/pixiv.auth.status.json --json
```

1 件の explicit Pixiv artwork URL を download せずに resolve:

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851"}' \
  | uv run --locked mediagent tools run pixiv.link.resolve --input - --json
```

Shared link-first pipeline で 1 件の explicit Pixiv artwork URL を download:

```bash
printf '%s\n' '{"url":"https://www.pixiv.net/artworks/143734851","write_sidecar_metadata":false}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

この path は 1 artwork URL を artwork 全体として扱い、default で全 original pages を resolve し、`pixiv.bookmarks.sync` と dedupe し、download 時に必要な Pixiv `Referer` を適用しますが、runtime headers は永続化しません。Credentials が missing/expired の場合、`pixiv.link.resolve` は browser login を自分で開始せず、recommended Pixiv auth tool を含む structured auth error を返します。

bookmarked works を収集:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.collect --input examples/tools/pixiv.bookmarks.collect.json --json
```

collector は normalized media items を返すだけで、file は download しません。通常の bookmark download には deterministic sync helper を使います。

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --json
```

どの file を download するかだけ preview し、file/DB writes を避けるには:

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync --input examples/tools/pixiv.bookmarks.sync.json --dry-run --json
```

Default example は downloaded files を scanner-friendly library root に置きます。

```text
$MEDIAGENT_LIBRARY_DIR/<platform>/<storage_category>/<yyyy>/<mm>/<yyyymmdd>__<platform>__<remote_id>__<part>.<ext>
```

Library root の解決順:

1. 明示的な tool input: `library_root` または legacy `target_dir`。
2. Platform-specific environment variable: `MEDIAGENT_<PLATFORM>_LIBRARY_DIR`。例: `MEDIAGENT_PIXIV_LIBRARY_DIR`。
3. Global environment variable: `MEDIAGENT_LIBRARY_DIR`。
4. Fallback: `${MEDIAGENT_DATA_DIR}/library`。

Pixiv を独自の top-level directory に置きたい場合は、次を設定します。

```bash
MEDIAGENT_PIXIV_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/pixiv
```

この root はすでに Pixiv-specific なので、その下では media/date layout を使い、`pixiv/pixiv` は追加しません。

Operator note: `MEDIAGENT_LIBRARY_DIR` の変更は future target planning にだけ影響します。同じ SQLite DB を使う場合、すでに terminal state の Pixiv items は dedupe され、新しい root には自動で再配置されません。Deployment を移動する時は、DB と library files を 1 つの state bundle として扱ってください。

Pixiv は file media type と work type を分離します。Manga source pages は photo files のままですが、official Pixiv `type:manga` は `work_type:comic` と `comic-pages` storage category を使います。Multi-page `type:illust` は illustration のままで `photo` に保存します。Packaged CBZ は `comic` を使います。

Legacy DB/library update は apply 前に plan します。

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.plan.json --json
```

Summary を review し、overlapping Pixiv sync jobs を停止してから apply します。

```bash
uv run --locked mediagent tools run pixiv.library.reconcile \
  --input examples/tools/pixiv.library.reconcile.apply.json --json
```

Apply mode は existing manga source files と adjacent JSON sidecars を `comic-pages` に atomic move し、SQLite paths/metadata を更新し、known Pixiv placeholder downloads を quarantine します。Pixiv には接続しません。すでに `.trash` に移動された files は missing として扱い、trash から自動復元しません。

Reconciliation 後、DB では completed だが library path に存在しない files の source-backed repair を preview します。

```bash
uv run --locked mediagent tools run pixiv.bookmarks.sync \
  --input examples/tools/pixiv.bookmarks.sync.repair.json --dry-run --json
```

Missing library files を再 download する意図がある場合だけ、`--dry-run` を外して同じ command を実行します。`repair_missing_files` は opt-in です。Normal timer runs は DB `downloaded` state を尊重し、repair は planned path に新しい copy を download して `.trash` を変更しません。

Reconciliation と必要な repair の後、legacy manga packaging を preview します。

```bash
uv run --locked mediagent tools run pixiv.comics.package \
  --input examples/tools/pixiv.comics.package.json --dry-run --json
```

`--dry-run` を外すと CBZ を作成します。Committed example は `migrate_legacy:true` を設定します。Tool は complete/healthy source pages だけを読み、`.partial` と atomic replacement で Kavita V2 archive を書き、SQLite に記録し、source pages は保持します。V2 成功後、old V1 archive は audited `.trash/mediagent/<removal-id>/` lifecycle で retire され、source row は removed state に link したまま保持されます。Rerun はその row を無視します。Future bookmark sync は `package_comics:true` で newly downloaded manga を自動 package できます。

Kavita V2 は series ごとに一つの directory を使います。Pixiv one-shot は unique series identity を持ち、real Pixiv series metadata のある works は同じ directory を共有し、normalized comic contract の `Series`、`Number`、optional `Volume`、optional `Count` を使います。

Pixiv image examples:

```text
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/pixiv/comic/作品タイトル [pixiv-139193091]/作品タイトル [pixiv-139193091].cbz
```

`MEDIAGENT_PIXIV_LIBRARY_DIR` が未設定の場合、shared-root examples は次の通りです。

```text
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/photo/2026/07/20260722__pixiv__143734851__p1.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic-pages/2026/07/20260722__pixiv__139193091__p0.jpg
$MEDIAGENT_DATA_DIR/library/pixiv/comic/作品タイトル [pixiv-139193091]/作品タイトル [pixiv-139193091].cbz
```

Immich が Pixiv external library を scan し、comics を別 reader に任せる場合、その external library の Scan Settings に次の二つの exclusion patterns を追加して rescan します。

```text
**/comic/**
**/comic-pages/**
```

Kavita は `pixiv/comic` だけを対象にし、`pixiv` や `comic-pages` を対象にしません。`comic` の immediate child はそれぞれ一つの series directory で、comic root に archive は直接置きません。`comic-pages` は Mediagent が repair または CBZ rebuild に使う lossless source として保持します。

SQLite database は `MEDIAGENT_DB_PATH` で決まります。完了した file は `media_files` に記録され、library-relative path、storage layout version、checksum、size、MIME type、file health を保持します。Parent item は `media_items` で `downloaded`、`partial`、`failed` のいずれかに更新されます。

Public library paths には default で JSON sidecar metadata を書きません。Source metadata は SQLite/internal records に保持します。明示的な debugging の時だけ `write_sidecar_metadata: true` を使います。

Pixiv に接続せず、known library files を verify するには:

```bash
uv run --locked mediagent tools run library.file.verify --json
```

Global content dedup を apply する前に preview します。

```bash
uv run --locked mediagent library reconcile-trash --dry-run --json
uv run --locked mediagent library reconcile-trash --json
uv run --locked mediagent library deduplicate --dry-run --json
uv run --locked mediagent library deduplicate --json
```

`reconcile-trash` は pre-v10 migration 専用で、apply 前に必ず dry-run を review します。Original path が missing の downloaded rows を調べ、legacy `.trash` 下で path/size が一致する candidates のみを DB 記録 SHA-256 で検証し、complete/unblocked plan だけを一つの SQLite transaction で removed state に import します。Files は移動せず、v10 `.trash/mediagent/` と JMComic reconciliation backups を無視し、古い duplicate trash copies を保持します。Unmatched row または active global identity conflict が一つでもあれば apply 全体を block します。Repair が意図的に removed された legacy content を再 download しないよう、global dedup より先に実行してください。

Apply/remove/restore/rename の前に overlapping sync services を停止するか、同じ deployment-wide `mediagent-sync.lock` を保持してください。SQLite busy timeout だけでは filesystem mutation を serialize できません。

Managed entry を one-shot remove、restore、rename します（この 3 operations は dry-run 非対応）。

```bash
uv run --locked mediagent library remove \
  --path "$MEDIAGENT_LIBRARY_DIR/photo/YYYY/MM/example.jpg" \
  --reason 'external library cleanup' --external-ref 'immich:asset-id' --json
uv run --locked mediagent library restore --removal-id 'rmv_replace_with_operation_id' --json
uv run --locked mediagent library rename \
  --path "$MEDIAGENT_LIBRARY_DIR/photo/YYYY/MM/example.jpg" \
  --name 'new display name' --external-ref 'immich:asset-id' --json
```

Remove 後の file は `.trash/mediagent/` に無期限で残り、expiry/purge job はありません。SQLite state を同期する必要がある場合、この interface を迂回して直接 `.trash` へ移動しないでください。External Immich cleanup systemd integration は今回まだ延期しています。

単一 file の manual debugging では `download.http` を使い、Pixiv referer header を付けます。

```bash
uv run --locked mediagent tools run download.http --input examples/tools/download.http.pixiv.json --json
```

download 後の file location は `download.http` input で決まります。example では次に置きます。

```text
$MEDIAGENT_DATA_DIR/pixiv/...
```

Debugging では `metadata.write` で JSON metadata を手動で書けますが、public library の default metadata format ではありません。Workflow V1 までは、deterministic sync helper がない platform は manual CLI/tool composition または external script が必要です。

## Telegram Local Session And Live Test Shape

Telegram V1 foundation は実装済みで、現在 phase の live verification は完了しています。2026-07-24 UTC に real login/status、curated link-inbox collection、2 件の小さな media downloads、1 件の 1 時間 video download、scanner-friendly layout placement、`library.file.verify`、second-run dedupe を確認しました。Real Telegram downloads は `.partial` files に直接 stream し、validation と chunked checksum calculation 後だけ finalization します。

Telethon-compatible user MTProto session を使います。送信、forward、削除、chat management には使わないでください。

`.env` に local-only values を追加します。

```bash
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_PHONE_NUMBER=...
TELEGRAM_SESSION_FILE=${MEDIAGENT_DATA_DIR}/credentials/telegram.session
```

API ID/hash は user の Telegram developer app から取得します。Session file は credential です。`MEDIAGENT_DATA_DIR` の下に置き、commit せず、public media library にコピーしないでください。

`.env` を読み込みます。

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

初回 Telegram login は二段階の local flow です。

Login code を要求します。

```bash
uv run --locked mediagent tools run telegram.auth.login --input examples/tools/telegram.auth.login.json --json
```

出力された `phone_code_hash` と Telegram から届いた code を使います。

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output"
}
```

次を実行します。

```bash
uv run --locked mediagent tools run telegram.auth.login --input /path/to/local-telegram-login-complete.json --json
```

Telegram が 2FA password を要求する場合は、`password_ref` を使います。Inline 2FA password は拒否されます。

```json
{
  "mode": "complete",
  "code": "12345",
  "phone_code_hash": "value-from-start-output",
  "password_ref": {
    "source": "env",
    "name": "TELEGRAM_2FA_PASSWORD"
  }
}
```

Configured session を検証します。

```bash
uv run --locked mediagent tools run telegram.auth.status --input examples/tools/telegram.auth.status.json --json
```

Media を download せず selectable dialogs を列挙します。

```bash
uv run --locked mediagent tools run telegram.dialogs.list --input examples/tools/telegram.dialogs.list.json --json
```

明示的な trusted source から media-bearing messages を収集します。

```bash
uv run --locked mediagent tools run telegram.messages.collect --input examples/tools/telegram.messages.collect.json --json
```

Files や DB rows を書かず deterministic Telegram sync を preview します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --dry-run --json
```

Source selector と limits を確認してから bounded sync を実行します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.json --json
```

Curated Telegram media では、private collection channel を作り、download したい media message links をそこに貼ります。その後 `examples/tools/telegram.messages.sync.link-inbox.json` の channel selector を設定して実行します。

```bash
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --dry-run --json
uv run --locked mediagent tools run telegram.messages.sync --input examples/tools/telegram.messages.sync.link-inbox.json --json
```

Configured user session が各 linked original message に access できる必要があります。この flow では Mediagent は Telegram chats への送信、forward、削除、管理を行いません。

最新の small-media live run は次を書き込みました。

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260720__telegram__1004315643983-26-6264845769908428204__v0.mov
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260710__telegram__1004315643983-15-6233357569825116111__p0.jpg
```

Long-video live run は次を書き込みました。

```text
$MEDIAGENT_DATA_DIR/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4
```

同じ direct-link sync を再実行すると、completed items は skipped になります。

Shared-root Telegram files は次の形になります。

```text
$MEDIAGENT_DATA_DIR/library/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/library/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram を独自の top-level directory に置きたい場合は次を設定します。

```bash
MEDIAGENT_TELEGRAM_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/telegram
```

その場合 files は次に置かれます。

```text
$MEDIAGENT_DATA_DIR/telegram/photo/2026/07/20260722__telegram__saved_messages-12345-photo-0__p0.jpg
$MEDIAGENT_DATA_DIR/telegram/video/2026/07/20260722__telegram__trusted-12345-video-0__v0.mp4
```

Telegram cursors は source と media-type scope ごとに保存されます。例: `messages:saved_messages:photo-video`。Durable sync processing が成功した後だけ進みます。

## Instagram Saved Session And Link Test

Instagram support は explicit-link first です。User-provided public post、carousel、Reel、tv URLs にだけ使います。Resolver は 1 件の Instagram post URL を post 全体として扱うため、carousel links は default ですべての resources を download します。Resolver は password login を自分で実行しません。

Local setup は `.env` values を使います。

```bash
set -a
source .env
set +a
```

Saved session を確認します。

```bash
uv run --locked mediagent tools run instagram.auth.status --json
```

Session が missing または invalid で credentials がある場合は、明示的に repair tool を呼びます。

```bash
uv run --locked mediagent tools run instagram.auth.ensure_session --json
```

Download せず link を inspect します。

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/"}' \
  | uv run --locked mediagent tools run instagram.link.resolve --input - --json
```

Shared link pipeline で post 全体を download します。

```bash
printf '%s\n' '{"url":"https://www.instagram.com/p/<shortcode>/","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

Downloaded files は次に保存されます。

```text
$MEDIAGENT_DATA_DIR/library/instagram/photo/<yyyy>/<mm>/
$MEDIAGENT_DATA_DIR/library/instagram/video/<yyyy>/<mm>/
```

Signed Instagram CDN URLs は runtime-only です。SQLite、sidecar metadata、logs、snapshots、committed fixtures に含まれていないことを確認してください。

## Link-First Resolver Smoke Checks

現在の primary path は explicit URL resolution であり、account saved/bookmark collection ではありません。Phase 19 link-first tools を開発するときは、まず以下を確認します。

Experimental link tools を list します:

```bash
uv run --locked mediagent tools list --json --include-experimental
```

Stable link tools を inspect します:

```bash
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
```

Explicit URL を queue します。Download は行いません:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","ingest_platform":"cli"}' \
  | uv run --locked mediagent tools run link.queue.upsert --input - --json
```

Core link pipeline で explicit URL を resolve して download します:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","write_sidecar_metadata":true}' \
  | uv run --locked mediagent tools run link.media.sync --input - --json
```

Tool JSON を書かずに同じ workflow を実行する public link entry point:

```bash
uv run --locked mediagent link sync 'https://example.com/path/to/media.jpg' --write-sidecar-metadata --json
```

Cron または daemon worker から queued links を実行:

```bash
uv run --locked mediagent tools run link.media.sync --json
```

Queued runs は短い lease で ready links を claim し、他 worker の未期限切れ leases を skip し、`next_attempt_at` を過ぎた retryable `deferred` links だけを含めます。Login walls、unsafe URLs、unsupported media、deleted/removed content、access controls などの permanent skips は retry しません。

現在の preview resolver を inspect します:

```bash
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
```

Download せず explicit URL を preview します:

```bash
printf '%s\n' '{"url":"https://example.com/path/to/media.jpg","record":false}' \
  | uv run --locked mediagent tools run link.resolve.preview --input - --json --allow-experimental
```

Expected behavior:

- Direct public image/video/audio URLs は full HTML fetch の前に resolve されます
- Public single-media HTML は clear candidate が 1 件だけある場合に resolve できます
- Reddit static galleries は複数 photo candidates として resolve できます。Complex galleries、login-required、JavaScript-required、blocked、unsafe、ambiguous pages は structured skip reasons を返します
- Download steps は preview output を信用せず、URL safety、redirect、MIME、byte-limit checks を再実行します
- Sync/download command を使う場合、output files は `${MEDIAGENT_DATA_DIR}` 配下に置きます

Redgifs direct/watch links は no-auth provider foundation として実装済みです。Public HTML が direct MP4 candidate を公開している場合、direct `redgifs.com/watch/<id>` links は `origin_source: "redgifs"`、`media_type: "video"`、file key `v0`、scanner-friendly storage `library/redgifs/video/<yyyy>/<mm>/...` に resolve されるべきです。

Reddit explicit links は現在 anonymous/bounded behavior を使います。Reddit page が external media を login wall や dynamic client data の後ろに隠している場合、resolver は `login_wall` または `external_source_hidden` で skip します。User が明示的に auth-assisted collection を再開しない限り、Reddit saved collection を次の product path として扱わないでください。

## Deferred Reddit OAuth and Saved Collection

Reddit V1 auth/saved tooling は fake-client coverage を持ちますが、deferred legacy/advanced capability です。Posting、commenting、voting、save/unsave、moderation、chat、subreddit scanning、HTML scraping、third-party extractors は実装しません。

この section は legacy auth-assisted path を明示的に検証する場合だけ使います。

`.env` に local-only values を追加します:

```bash
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_REDIRECT_URI=http://127.0.0.1:8765/reddit/callback
REDDIT_USER_AGENT='linux:mediagent:0.1 (by /u/your_username)'
REDDIT_CREDENTIALS_FILE=${MEDIAGENT_DATA_DIR}/credentials/reddit-oauth.json
```

`.env` を読み込みます:

```bash
set -a
source .env
set +a
mkdir -p "$MEDIAGENT_DATA_DIR/credentials"
```

authorization URL を生成:

```bash
uv run --locked mediagent tools run reddit.auth.start --input examples/tools/reddit.auth.start.json --json
```

返された `data.authorization_url` を browser で開きます。Redirect 後、`code` query parameter を `examples/tools/reddit.auth.exchange.json` を元にした local exchange input に入れて実行します。

```bash
uv run --locked mediagent tools run reddit.auth.exchange --input /path/to/local-reddit-auth-exchange.json --json
```

configured session を確認:

```bash
uv run --locked mediagent tools run reddit.auth.status --input examples/tools/reddit.auth.status.json --json
```

Saved media candidates だけを collect し、download しません:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --json
```

Credentials、DB writes、network なしで collector preview:

```bash
uv run --locked mediagent tools run reddit.saved.collect --input examples/tools/reddit.saved.collect.json --dry-run --json
```

`reddit.saved.collect` は normalized media items と optional cursor state だけを返します。Download orchestration は現在の方向ではありません。User が明示的に auth-assisted account collection を再開する場合を除き、`reddit.saved.sync` を追加しないでください。

## よくある問題

- `ModuleNotFoundError: mediagent`: `uv run --locked ...` を使うか、`PYTHONPATH=src` を設定します。
- exit code `2`: input、config、auth、permission、filesystem、database validation の問題です。
- exit code `1`: runtime、network、rate-limit failure です。
- unsafe path error: `MEDIAGENT_DATA_DIR` を設定し、その配下に書き込みます。
- X auth failure: token expiration、required scopes、`X_CREDENTIALS_FILE` が allowed write root の中か確認します。
- Pixiv auth failure: `PIXIV_CREDENTIALS_FILE`、token expiration、callback URL/code freshness、credential file が `MEDIAGENT_DATA_DIR` 配下か確認します。従来の refresh-token path を使う場合は `PIXIV_REFRESH_TOKEN` も確認します。
- Pixiv download 403: `download.http` headers に `{"Referer":"https://www.pixiv.net/"}` を追加します。
- Telegram auth failure: `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、`TELEGRAM_SESSION_FILE`、session file が `MEDIAGENT_DATA_DIR` の下か確認します。
- Reddit explicit link が `login_wall` または `external_source_hidden` を返す: public HTML が media URL を公開していない場合は expected skip です。利用できる場合は Redgifs など direct provider links を優先します。
- Deferred saved-collection tooling の Reddit auth failure: `REDDIT_CLIENT_ID`、`REDDIT_REDIRECT_URI`、`REDDIT_USER_AGENT`、`REDDIT_CREDENTIALS_FILE`、callback code freshness、credential file が `MEDIAGENT_DATA_DIR` 配下か確認します。

## Safety Reminder

現在の expansion path は explicit link resolution で、まず no-auth behavior を優先します。User が明示的に再開しない限り、X と Reddit auth-assisted collection は deferred です。Pixiv と Telegram は現在の deterministic sync slice について user-assisted live verification を完了しています。Platform-specific login tool を使う future live runs では引き続き user-provided credentials が必要です。
