# Mediagent Architecture

## Comic target と collection flow（2026-08-13）

コミックの元ページは `media_type: photo` のまま、`metadata.work_type: comic` と `storage_category: comic-pages` で処理を選択する。provider-neutral な `metadata.comic` が作品、series、chapter identity を保持する。宣言された全ページが正常な場合に限り、`comic/` 配下へ `ComicInfo.xml` 付き CBZ を atomic に生成する。

直接 URL は follow state を作らない。favorites は全ページを完全に収集してから一つの SQLite transaction で snapshot を commit し、途中失敗時には旧 membership を無効化しない。favorite 解除は provenance のみ停止し、既存 page／CBZ は削除しない。active な JM favorite album は次回同期で再解決して新章を検出し、nhentai favorite は exact のままとする。

JMComic album-scoped resolution では album episode list が chapter number を所有する。Per-photo payload は pages/title を提供するが、series list が lagging しても chapter number を 1 に downgrade できない。Raw duplicate number は normalized item が DB/package layer に入る前に disambiguate する。Historical repair は DB に現れる各 album を resolve、current manifest identity と DB／CBZ `ComicInfo.xml` を比較、read-only manifest を生成し、confirmed apply で healthy tracked source pages から affected archive だけを rebuild、replaced CBZ を quarantine する。Provider/network または source-health gap は mutation 前に apply を block する。

Schema v8 は `source_collections` と `source_collection_memberships`、v9 は account-scoped human name と remote collection scope を結ぶ `source_collection_scope_aliases`、v10 は `content_blobs`、`library_entries`、`library_operations`、`media_files.library_entry_id` を追加する。

`content_blobs` が checksum identity を所有し、provider `media_items` / `media_files` は全 source references を保持する。一般 media の同一 checksum は一つの scanner-visible entry だけを投影し、Immich の cross-provider duplicate を防ぐ。Comic pages/CBZ は context-specific presentation key で path を分離し、可能なら hard link を使う。Remove は source metadata を削除せず、repair/verify は explicit restore まで removed entry を skip する。Raw `download.http` は DB media identity を持たない unmanaged transport primitive であり、managed sync tools は file upsert 直後に adoption する。

安定した page file key により CDN URL のローテーションで重複 row を作らない。cookie、token、機密 header、JM runtime decode は永続 metadata に保存しない。

## Product Boundary

Mediagent は media の収集と download だけを担当します。media library management、media browsing、repost、sharing、gallery UI は提供しません。

## Package Layout

```text
src/mediagent/
  cli.py
  agent/
  core/
  tools/
  platforms/
  workflows/
```

## Core Layer

`src/mediagent/core/` は共有 primitives を置きます。

- `tooling.py`: `ToolSpec`、`ToolContext`、`ToolResult`、permissions、registry errors、`ToolRegistry`
- `db.py`: SQLite schema と persistence helpers
- `library_content.py`: global SHA-256 identity、scanner-visible projection、dedup scan/apply、verified legacy-trash reconcile、remove/restore/rename lifecycle
- `filesystem.py`: path placeholder expansion、normalization、write-boundary checks
- `auth.py`: credential refs、credential JSON helpers、redacted auth session model
- `http.py`: testable HTTP client abstraction
- `rate_limit.py`: shared rate-limit metadata extraction
- `redaction.py`: secret redaction helpers
- `schema.py`: lightweight JSON-schema-compatible input validation

Core code に platform-specific API behavior を入れてはいけません。

## Tool Layer

`src/mediagent/tools/` は agent-callable tools を置きます。

各 tool は必ず:

- 安定した `ToolSpec` を宣言する
- permissions を宣言する
- dry-run support を宣言する
- JSON-compatible input/output schemas を提供する
- `ToolResult` を返す
- secrets を漏らさない

CLI、future workflows、future Agent Core は同じ registry から tools を呼びます。

## Agent Core V1 Layer

`src/mediagent/agent/` は現在の local Agent Core V1 preview を含みます:

- `skills/`: Markdown SKILL loading と built-in English SKILL files
- `llm/`: Ollama client boundary
- `prompts.py`: strict JSON skill/action prompt builders
- `actions.py` と `schema.py`: action parsing と structured agent run contracts
- `core.py`: SKILL-scoped run loop

Agent Core V1 は scheduler でも broad autonomous planner でもありません。SKILL を選び、その SKILL allowlist 内の tools だけを `ToolRegistry` 経由で呼び、明確に一致する SKILL がない場合は tool call 前に unsupported tasks を拒否し、model の dry-run choices を global runtime mode に normalize し、user が明示していない destination paths を strip しなければなりません。

## Platform Layer

`src/mediagent/platforms/` は platform-specific client、auth、parser を置きます。

現在:

- `platforms/x/` 実装済み
- `platforms/pixiv/`
- `platforms/telegram/`
- `platforms/reddit/`
- `platforms/instagram/`

`platforms/x/` には次があります。

- `auth.py`: OAuth 2.0 PKCE、token refresh、credential file support、auth status checks
- `client.py`: X API `/2/users/me` と authenticated-user bookmarks
- `parser.py`: X tweet/media expansions を normalized media items に変換

`platforms/pixiv/` には次があります。

- `auth.py`: local OAuth/PKCE setup、explicit refresh-token auth、token refresh、credential file support、auth session model
- `client.py`: Pixiv App API user detail、bookmarked illustrations、artwork detail、ugoira metadata calls
- `links.py`: shared link-first resolver pipeline 用の explicit artwork URL/id normalization と Pixiv artwork-detail resolution
- `parser.py`: Pixiv works を normalized media items に変換し、`illustration` / `comic` / `animation` work types、multi-page works、unavailable placeholder rejection、ugoira metadata を扱う

`platforms/telegram/` には次があります。

- `auth.py`: Telethon-compatible user-session configuration、session-path safety、safe auth-session modeling
- `client.py`: fake-client hooks と lazy Telethon usage を持つ Telegram client boundary
- `parser.py`: Telegram message/media shapes を normalized media items に変換し、grouped media/albums を扱う

`platforms/reddit/` には次があります。

- `auth.py`: Reddit OAuth config、token exchange/refresh/status、credential file helpers、Reddit rate-limit metadata parsing
- `client.py`: Reddit OAuth API `/api/v1/me` と authenticated-user saved listings calls
- `parser.py`: saved listing entries を first-version image/gallery/video/direct-media shapes の normalized media items に変換

`platforms/instagram/` には次があります。

- `auth.py`: saved-session status、explicit local username/password login、bounded ensure-session behavior、credential path safety、agent-decidable auth/session error mapping
- `links.py`: Instagram post/Reel/tv URL parsing、canonical identity、whole-post resource normalization、runtime-only signed CDN download URL handling
- `client.py` と `parser.py`: one-page saved-feed access、opaque cursor、whole-post saved-media normalization

## CLI Flow

```text
CLI args
-> read JSON input
-> create ToolContext
-> find tool in ToolRegistry
-> validate input
-> run tool
-> print JSON or human-readable output
-> return stable exit code
```

Exit codes:

- `0`: success
- `1`: runtime/network/rate-limit failure
- `2`: validation、auth、permission、filesystem、database、user input error

## 現在の Link-First Flow

```text
explicit URL source
-> link.queue.upsert
-> URL safety and canonicalization
-> resolver chain
-> media candidates
-> deterministic candidate selection
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

これが現在の主要 product direction です。URL source は CLI JSON、queued DB rows、Telegram inbox links、future workflow steps、future Agent/SKILL calls になり得ます。

Shared link intake は generic resolver chain より前に dedicated comic dispatch を行います。`link.media.sync`、queued `link_queue` rows、`telegram.inbox.sync_links` から受け取った nhentai gallery と JMComic album/photo/cover links は、すべて exact scope で `comic.link.sync` に入ります。Telegram は ingest provenance のままで、source platform を変更せず、favorite/follow state も作りません。Future inbox implementation は provider routing を個別実装せず、link を enqueue して `link.media.sync` を呼びます。

`link_queue.normalized_url` は最初の intake dedupe layer にすぎません。Resolvers は可能な場合、canonical aliases と source/media identity も出力し、short links、canonical post links、old site links、provider watch URLs、direct media URLs が同じ content を指す場合に duplicate downloads が発生しないようにします。

`link_queue` には schema v7 lifecycle foundation があり、cron または daemon usage の土台になります。これは URL resolution queue であり、file-download lifecycle ではありません。

```text
queued
-> resolving
-> resolved
```

Permanent skips と retryable failures は分けます。

```text
skipped
failed
deferred
```

Schema は retry counts、last error、retryable flag、next attempt time、source provenance merge fields を保持し、tool layer は batch limit を持ち、lease columns も持ちます。`link.media.sync` は queued runs で active claim/lease behavior を使い、retryable failures を bounded `next_attempt_at` backoff 付きで schedule します。Explicit URL と explicit `link_id` runs は意図的に queue claiming を bypass します。

Successful な `link.media.sync` run は 1 回の tool call で resolve と download を行うことがありますが、URL resolution が完了した link row は `resolved` のままです。Download progress と最終 file state は `media_items` と `media_files` が source of truth で、`downloaded`、`partial`、`failed` などの状態を持ちます。

`MediaCandidate` は credential-bearing request headers を永続化してはいけません。Persistable download hints は allowlisted かつ non-secret に限定し、必要な場合の public `Referer` などだけを許可します。`Authorization`、`Cookie`、signed URL tokens、session headers、CSRF headers など runtime-only headers は download context reference 経由で memory に保持し、SQLite、sidecar metadata、logs、snapshots には保存しません。

Multi-candidate resolution は、Reddit galleries のような simple static file groups ではすでに対応しています。現在の contract は、それらの static groups について group id、required files、optional files、partial-success status、`metadata.files` mapping を記録します。Muxed video/audio tracks やより complex な multi-file posts は deferred のままです。

Instagram は同じ link-first contract を使いますが、platform session boundary を持ちます。Instagram の `/p/<shortcode>/`、`/reel/<shortcode>/`、`/tv/<shortcode>/` URL 1 件は post 全体を表します。Carousel resources は 1 件の media item 配下の multiple files として normalize され、signed Instagram CDN URLs は runtime-only として扱い、canonical media identity として persist しません。

## 既存 Collector Flow

```text
platform collector output
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http or platform-specific downloader
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

`pixiv.bookmarks.collect`、`pixiv.bookmarks.sync`、`telegram.messages.collect`、`telegram.messages.sync` は有用な実装済み flows として維持します。X bookmark collection と Reddit saved collection は fixture/fake-client coverage を持ちますが、user が明示的に auth-assisted account collection を再開しない限り、現在の expansion path ではありません。

Pixiv では physical media type と work type を分離します。Manga source pages は `media_type: photo` のままですが、`metadata.work_type: comic` が `comic-pages` を選択し、Kavita-oriented CBZ layer は `comic/<series-directory>/` に archive を書きます。One-shot は unique series identity と `Number=1`、`Count=1`、`Format=One-Shot` を使い、real series は同じ directory を共有して normalized `Series`、`Number`、optional `Volume`、optional `Count` を使います。Normal illustrations は `illustration` / `photo`、ugoira は `animation` / `video` のままです。

`core/comics.py` の descriptor/CBZ writer は platform-neutral です。Pixiv、nhentai、JMComic は現在 normalized `metadata.comic` と `comic-pages` -> `comic` packaging contract を共有します。Future authorized-source adapters も reliable な `work_type:comic`、ordered pages、series/chapter/volume identity、work metadata を提供できれば同じ flow に参加できます。Multi-image count だけで comic と推定してはいけません。

## Future Policy Layer

RuleSpec は planned policy layer であり、implemented runtime feature ではありません。

将来の想定形:

```text
explicit URL source or collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM または Agent Core integrations は natural-language intent を explicit tool calls または future RuleSpec files に変換する補助に使えます。ただし daemon / cron execution は stored deterministic rules を実行するべきです。Platform adapters に quality scoring や content-preference logic を入れてはいけません。

## Deferred Workflow Layer

`src/mediagent/workflows/` は placeholder です。ユーザーが明示的に選ぶか、次の platform foundation により shared sync contract が十分安定したと判断できるまで、Workflow V1 は実装しないでください。
