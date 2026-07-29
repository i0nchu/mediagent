# Mediagent Focused TODO

このファイルは今後実装する作業だけを追跡します。詳細な状態、検証履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新する場合は、同じ変更で英語版と繁体字中国語版も同期してください。

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## 完了済みベースライン

以下の基盤は現在の開発ベースラインとして扱える程度に完了しています。

- `src/mediagent/` の Python package layout
- Tool contract、registry、CLI bridge
- env、DB、paths、run records、media items、media files、HTTP download、metadata writing、sync cursors、storage path planning、library verification の底層 tools
- redaction と credential-file boundary を持つ credential/auth foundation
- 汎用 scanner-friendly storage layout：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`
- X auth と bookmark collection の fixture/fake-client tests。Live verification は未実施
- Pixiv auth、bookmark collection、deterministic `pixiv.bookmarks.sync`
- Pixiv bounded live layout verification は 100 bookmark items / 624 photo files で `scanner-friendly-v2` を確認済み
- Telegram media-source foundation：explicit local `telegram.auth.login`、Telethon-backed user-session config、auth status、dialog listing、message/link-inbox collection、Telegram-specific media download、deterministic message sync、CLI examples、fake-client tests
- Telegram real login、curated link-inbox collection、小さな photo/video download、rerun dedupe は live-verified 済み
- Telegram stream-safe real downloads と 1 時間 video live verification は現在 phase の目標として完了済み
- Phase 16 undocumented Telegram inbox link resolver foundation は experimental boundaries の後ろに実装済みです。URL queueing、URL safety、direct media / generic single-media HTML / Imgur single-page / Pixiv artwork-link resolver behavior、origin-source storage metadata、link-safe download、regression tests を含みます
- Phase 17/18 Reddit explicit-link resolver foundation は credential-light single-media links に対応済みです。Direct `i.redd.it` images、direct `v.redd.it` MP4 video-only files、Reddit post/share links、bounded anonymous HTML、static `over18=1` 付き `old.reddit.com` fallback、unsupported gallery/manifest cases の structured skips、Reddit metadata preservation、Telegram inbox live verification、dedupe verification、file verification を含みます
- Phase 19 first stable link layer は実装済みです。schema-v7 `link_queue` lifecycle fields、active claim/lease と retry scheduling、source provenance merge、stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>` CLI entry point、Redgifs direct/watch resolver foundation、Reddit static/preview gallery support、Reddit external-provider delegation、simple static groups の multiple file candidates、resolver header persistence sanitizer、regression tests を含みます
- Conservative cleanup/recovery foundation は `core.cleanup.media_state` で実装済みです。dry-run planning、manifest output、explicit apply confirmation、quarantine-before-DB-reset behavior、credential path protection に対応します
- Phase 19 live verification は public `mediagent link sync <url>` entry point、Redgifs direct/watch links、Reddit-to-Redgifs delegation、anonymous Reddit single-file photo/GIF links、preview fallback で解決した Reddit multi-image gallery 1 件で完了済みです。Latest compatibility wrapper rerun は 13 inbox links 中 12 件を resolve し、1 件の expected X/auth link を skip し、2 件の新規 delegated Redgifs MP4 files を download し、failed/partial downloads は 0 でした。Phase19 live-test library には現在 5 件の Redgifs MP4 files と 6 件の Reddit photo/GIF/JPEG files があります
- Reddit foundation は実装済みです。OAuth config/auth tools、saved-listing collector、image/gallery/video/direct media shapes の media parser、CLI examples、credential path safety、cursor path safety、fake-client tests を含みます。User が明示的に auth-assisted account collection を再開しない限り、deferred legacy/advanced capability として扱います

完了済み phase の詳細をここに展開しないでください。今後の作業に直接影響する場合だけ短い baseline note を追加します。

## 完了済み焦点：Phase 19 Link-First Resolver Hardening

Phase 19 の operational slice は完了済みです。この section 内の未チェック項目は post-19 promotion、future provider planning、または deferred policy/test follow-ups であり、現在の stable link-first baseline の blocker ではありません。

Goal: user が明示的に提供した link を Mediagent の主要な product path にします。

従来の auth-first path は主方向ではありません。

```text
auth
-> account bookmarks / saved items / feeds
-> automatic discovery
-> download
```

新しい primary path は次の形です。

```text
explicit URL source
-> URL normalization and uniqueness check
-> link queue lifecycle control
-> safe resolver chain
-> normalized media candidates
-> deterministic candidate selection
-> existing media/download/storage pipeline
```

Pixiv bookmark sync はすでに実装済みで有用なため、例外として維持します。新しい platform work は account collection より先に explicit-link resolution から始めます。

### 19A. Public Link Tool Surface

- [x] 現在 hidden になっている link resolver work を Telegram-only secret feature から first-class core link workflow へ昇格する。
- [x] 安定するまでは CLI surface を conservative に保つ。実装は Telegram-only code ではなく core link tools に置く。
- [x] URL intake と normalized-URL dedupe のために `link.queue.upsert` を追加する。
- [x] Lifecycle、retry metadata、source provenance、future leases のために schema-v7 queue fields を追加する。
- [x] Permanent skips と retryable failures を分ける。Login wall、unsupported domain、unsafe URL、ambiguous page は無期限に retry しない。
- [x] CLI、Telegram inbox、workflow、future Agent/SKILL calls など複数 source から同じ URL が投入された場合、source provenance を merge する。
- [x] Deterministic orchestration tool として `link.media.sync` を追加する。Queued URLs の読み取り、resolve、media item upsert、known item filter、storage path planning、file download、metadata write、file state record を行う。
- [x] CLI JSON、queued `link_id` records、Telegram inbox links、future workflow steps、future Agent/SKILL calls を URL input として受け付ける。
- [x] Dry-run mode は files を書かず、DB state を変更せず、media-file records も作成しない。
- [x] 現在の single-worker path では、JSON output は cron、workflows、future agents が利用できる程度に安定している。
- [x] Queue claim/lease behavior を有効化し、concurrent cron または daemon runs が同じ queued link を処理しないようにする。
- [x] `next_attempt_at`、bounded attempts、retryable skip handling による retry scheduling を追加する。
- [ ] Public preview/debug API が決まった後、`link.resolve.preview` と `link.resolve.to_media_item` を promote または置き換える。

### 19B. Resolver Contract

- [x] `MediaCandidate` を定義する。JSON-compatible fields は `url`、`media_type`、`mime_type`、`extension`、`size_bytes`、`source`、`quality_rank`、`file_index`、`content_identity`、`persistable_headers`、`download_context_ref`、`details`。
- [x] `persistable_headers` は allowlisted、non-secret set として扱う。Public media delivery に必要な場合は `Referer` を保存できるが、`Authorization`、`Cookie`、bearer tokens、signed URL secrets、session headers、CSRF headers は runtime-only とし、SQLite、sidecar metadata、log、snapshot へ保存しない。
- [x] Link resolution state を永続化する前に credential-bearing candidate headers を strip する。
- [x] `LinkResolution` を定義する。`status`、`skip_reason`、`original_url`、`normalized_url`、`canonical_url`、`aliases`、`final_url`、`origin_source`、`resolver_chain`、`auth_used`、`media_candidates`、`selected_candidate`、`warnings`、`details` を含める。
- [x] Resolver は可能な場合に canonical source identity を出す。例：`platform + remote_id`、provider media id、canonical post URL、direct content URL。
- [x] Simple static file groups では multiple internal candidates に対応する。
- [x] Simple static file groups の multi-candidate group semantics を定義する：group id、required files、optional files、candidate ordering、partial-success status、`metadata.files` mapping。
- [x] `requires_auth`、`login_wall`、`unsupported_domain`、`unsupported_media_type`、`unsupported_multi_media`、`javascript_required`、`blocked`、`unsafe_url`、`too_large`、`ambiguous_candidates` などの structured skip reasons を使う。
- [x] Debugging と indexing に必要な metadata は保持するが、raw HTML dumps、raw Telegram message text、cookies、tokens、credential-bearing headers は保存しない。
- [x] Storage layout は変更しない：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`。

### 19C. Canonical Dedupe

- [x] `link_queue.normalized_url` は最初の intake dedupe layer として扱い、final media identity とは見なさない。
- [x] First link alias strategy を追加し、`redd.it/<id>`、`reddit.com/r/.../comments/<id>/...`、`old.reddit.com/...`、provider watch URL、direct media URL が同じ queued link または resolved source を指せるようにする。
- [x] Resolver output を使い、link aliases と `platform + remote_id` media item layers で dedupe する。Known file records と checksums は既存 target の re-download を防ぐ。
- [x] すべての known source URLs を provenance として保持するが、duplicate download work は作らない。
- [x] Rerun は既存 link の resolution metadata を更新できるが、completed media-file state を reset しない。

### 19D. Generic Resolver

- [x] Full HTML を取得する前に direct public media URLs を resolve する。
- [x] `.mov` / `video/quicktime` を含む bounded image/video/audio MIME checks を支援する。
- [x] HEAD、range GET、または bounded GET fallback で redirects、final URL、MIME type、size を再検証する。
- [x] Bounded public HTML から `og:image`、`og:video`、`twitter:image`、`twitter:player:stream`、`<video>`、`<source>`、direct media anchors、simple JSON-LD/media URL fields を parse する。
- [x] Candidate scoring により、明らかな original/full-size media を thumbnails、icons、avatars、decorative images より優先する。
- [x] Deterministic に clear media candidate を 1 件選べる場合だけ download する。
- [x] Page が複数の plausible media files を出す場合、download せず `ambiguous_candidates` または `unsupported_multi_media` を返す。
- [x] JavaScript 実行、CAPTCHA 解決、DRM 回避、credential scraping、page dumps 保存は行わない。

### 19E. Reddit Resolver

- [x] Anonymous resolution を優先する。Direct `i.redd.it`、direct `v.redd.it` MP4、Reddit post/share links、`redd.it/<id>`、`old.reddit.com` fallback を扱う。
- [x] Login walls、blocked pages、no-media pages を structured skip reasons で検出する。
- [ ] Real examples または fixtures が利用可能になったら、deleted/removed/quarantined pages の structured skip coverage を拡張する。
- [x] 現在 phase では Reddit auth fallback を実装しない。Resolve できない login-wall posts は `login_wall` または `external_source_hidden` で skip する。
- [x] Publicly visible な Reddit metadata fields がある場合だけ parse する。例：`url_overridden_by_dest`、`secure_media`、`media_embed`、`preview`、`reddit_video`、static gallery metadata。
- [x] Publicly visible な Reddit metadata が external URL を指す場合、Reddit resolver 内に one-off domain logic を書かず、その URL を resolver chain に戻す。
- [x] Live test で Reddit rich-video posts が Redgifs に delegate することを確認したため、Redgifs を priority provider adapter として扱う。
- [x] Unknown external providers は Generic Resolver に fallback する。
- [x] Public HTML が direct `i.redd.it` candidates を公開している場合、static Reddit image galleries を支援する。
- [x] DASH/HLS muxing、multi-file `v.redd.it` support は multi-candidate contract が test されるまで deferred にする。
- [x] Reddit posting、commenting、voting、save/unsave、moderation、chat-management features は追加しない。

### 19F. Redgifs Foundation

Goal: Redgifs を stable no-auth provider adapter にします。Direct Redgifs links は今 download できるようにし、将来 `reddit link -> Redgifs link` がつながった場合も同じ downstream path を再利用できるようにします。

- [x] Public `redgifs.com/watch/<id>` と known Redgifs host variants 向けの dedicated Redgifs resolver を追加する。
- [x] Redgifs URLs を canonical watch URL と stable remote id に normalize する。
- [x] Bounded public Redgifs watch-page HTML から direct MP4 candidates を抽出する。
- [x] Preview images や decorative assets より、`media.redgifs.com/<Id>.mp4` または `media.redgifs.com/<Id>-silent.mp4` のような clear video candidates を優先する。
- [x] `audio_status` を `unknown`、`silent`、`not_detected` として記録する。ただし muxed audio は約束しない。
- [x] Generic Resolver と `download.http` と同じ redirect、MIME、size、URL safety checks で direct Redgifs media を validate する。
- [x] Resolved items を `origin_source: "redgifs"`、`media_type: "video"`、file key `v0`、storage path `library/redgifs/video/<yyyy>/<mm>/...` に map する。
- [x] Redgifs が別 resolver から到達された場合、Telegram inbox や future Reddit delegation などの upstream provenance を保持する。
- [x] Unavailable videos、region blocks、login/age gates、JavaScript-only pages、ambiguous multi-media pages、unsupported MIME、oversized media には structured skips を返す。
- [x] この phase では Redgifs API credentials や third-party API access を使わない。
- [x] Creator profiles、searches、feeds、related videos、comments、account data は scrape しない。
- [x] Telegram inbox 内の direct と Reddit-delegated Redgifs links で live-test する。5 件の Redgifs watch links は resolve され、MP4 files が phase19 live-test library に download されました。

### 19G. Post-19 Connected Provider Adapters

- [ ] Imgur single-media support は維持しつつ、同じ provider-adapter pattern に移行する。
- [ ] Pixiv artwork-link resolution は Pixiv bookmark sync と分けて計画する。
- [ ] X post-link resolution は X bookmark APIs と分けて計画し、login wall / anti-bot failures が normal skip states になり得る前提で扱う。
- [ ] Generic、Redgifs、Reddit resolver contracts が安定するまで Instagram は deferred にする。

### 19H. Deferred Auth Fallback Policy

- [ ] 現在 phase では Reddit app-only auth を実装しない。
- [ ] Reddit user OAuth と script password grant は later optional local-only fallbacks として残し、primary project direction にはしない。
- [ ] Reddit API approval が将来利用可能になった場合、explicit Reddit links の optional metadata-only fallback を再検討する。
- [ ] 将来の Reddit auth fallback は user-provided explicit links の metadata だけを読み、saved items、feeds、subreddits、comments、votes、account history は読まない。
- [ ] 将来 Reddit Data API を使う場合は、registered OAuth token、unique descriptive `REDDIT_USER_AGENT` を必須にし、`X-Ratelimit-Used`、`X-Ratelimit-Remaining`、`X-Ratelimit-Reset` から rate-limit backoff を行う。
- [ ] Official policy が変わらない限り、現在の Reddit free Data API guidance である OAuth client id ごと 100 QPM、10 分 window 平均を守る。
- [ ] Reddit API limits、login walls、deleted content、removed content、access controls の bypass を試みない。
- [ ] API fallback で Reddit metadata を保存する場合、feature promotion 前に deleted Reddit user content の retention/deletion strategy を追加する。

References:

- Reddit Data API Wiki: <https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki>
- Reddit Data API Terms: <https://redditinc.com/policies/data-api-terms>

### 19I. Promotion And Compatibility

- [x] Queue intake と sync orchestration の stable public tool names を決定する：`link.queue.upsert` と `link.media.sync`。
- [x] `telegram.inbox.sync_links` は wrapper として残し、既存 live-test commands を壊さない。
- [x] `link.queue.upsert` と `link.media.sync` の examples を追加する。
- [x] Stable core link tools に合わせて `TOOL_CATALOG.md`、`RUNBOOK.md`、`ARCHITECTURE.md`、localized handoff files を更新する。
- [x] Normal tool listing は conservative に保つ。Stable link tools は public、experimental Telegram inbox と preview helpers は引き続き explicit opt-in flags を要求する。
- [x] Promoted link tools の exit codes、JSON result shape、dry-run behavior、queue behavior、structured skip reasons を文書化する。

### 19J. Verification And Post-19 Test Follow-Ups

- [x] URL normalization、canonicalization、normalized URL uniqueness を unit-test する。
- [x] Initial link queue lifecycle metadata、retryable vs permanent skips、source provenance merge、batch limits を unit-test する。
- [x] Claim/lease execution の実装後に active retry scheduling と concurrent claim behavior を unit-test する。
- [x] Distinct Reddit links と provider/direct media identities で alias/canonical/media-item dedupe を unit-test する。
- [x] Credential-bearing headers が link resolution state を通じて SQLite に保存されないことを unit-test する。
- [ ] Runtime-only download contexts ができた後、metadata sidecars、logs、snapshots、signed runtime download data、`download_context_ref` まで secret persistence tests を拡張する。
- [x] SSRF protections を unit-test する：unsafe schemes、userinfo、localhost/private IPs、unresolved hosts、redirect limits、redirect-to-private-target。
- [x] Direct media resolution を unit-test する：images、GIF、MP4、WebM、MOV、audio MIME types。
- [x] Generic HTML candidate parsing、thumbnail rejection、ambiguous candidate skip、no-JS behavior を unit-test する。
- [x] Redgifs URL normalization、watch-page extraction、direct MP4 candidate selection、preview rejection、unavailable video skip、live-test fixture parsing を unit-test する。
- [x] Reddit external URL delegation to Redgifs の実装後に対応する unit test を追加する。
- [x] Reddit anonymous fallback、login-wall detection、static gallery resolution、structured skips を unit-test する。
- [x] Static file groups について、multi-candidate planning fixtures で partial success、required-file failure、`metadata.files` mapping を unit-test する。
- [ ] Reddit API fallback を promote する前に、Reddit rate-limit metadata parsing と backoff behavior を unit-test する。
- [x] `link.media.sync` の dry-run no writes と rerun dedupe を unit-test する。
- [x] Live-test は user-provided explicit URLs のみを使い、output paths は `${MEDIAGENT_DATA_DIR}` 配下に限定する。

## Side Decisions And Post-19 Guidance

これらの items は future work の guidance であり、Phase 19 の未完了 implementation として扱わないでください。

- [ ] Auth-assisted account collection は optional legacy/advanced behavior として扱う。Pixiv bookmark sync が現時点の唯一の例外。
- [ ] Reddit、X、Instagram、future platforms は saved/bookmark/feed collectors より explicit-link resolvers を優先する。
- [ ] No-auth Generic Resolver、Redgifs foundation、Reddit anonymous resolver が安定するまで Reddit auth fallback は deferred にする。
- [ ] X live OAuth verification は未実施。API access に paid credits が必要な可能性がある。
- [ ] Phase 19 core link tools の後に X と Pixiv の explicit-link resolvers を計画し、inbox automation が bookmark/feed access に依存せず explicit post/artwork links から download できるようにする。
- [ ] Pixiv に bookmarks 以外の source tools が必要か議論する。例：following-user works、explicit artwork IDs。
- [ ] Core link tools ができてから、Telegram inbox link behavior は URL input source の一種として promote する。
- [ ] Pixiv、Telegram、Reddit、X の boundaries が安定するまで Instagram は deferred にする。

## 設計決定: Option B Hidden Telegram Link Resolver

Goal: Telegram inbox の external-link feature は domain allowlist でも unrestricted crawler でもなく、bounded resolver pipeline として実装します。

これにより、user-curated Telegram inbox には有用なまま、security と maintenance surface を限定できます。

Target shape:

```text
Telegram inbox message
-> external URL extraction
-> URL normalization and uniqueness check
-> resolver chain
-> normalized media item
-> existing sync/download/storage pipeline
```

Implementation boundaries:

- Telegram は ingest source と provenance のみとして扱い、storage platform は解決後の `origin_source` で決めます。
- `link_queue.normalized_url` は最初の intake uniqueness key として扱います。Resolver canonical aliases と final media identity により、異なる URL が同じ content を指す場合の duplicate downloads を防ぎます。
- Resolver behavior は明示的に promote するまで experimental/undocumented tool boundaries の後ろに置きます。
- Public HTML pages には domain allowlist を要求しません。
- First-version resolvers は public HTTPS direct media URLs、bounded public HTML parsing で見つかる明確な単一 media target、必要な場合だけ追加する少数の explicit provider adapters に限定します。
- Direct media は bounded image/video MIME types を対象にし、`.mov` / `video/quicktime` を含めます。
- Public HTML pages は resolver が downloadable media file を正確に 1 件だけ deterministic に識別できる場合だけ対応します。
- Login-required pages、multi-media pages、JavaScript-rendered pages、safe redirects を超える URL shortener expansion、unknown providers は structured reasons 付きで skip します。
- 実際の GET download では preview result を信用せず、URL safety、redirect validation、MIME validation、byte limits を再実行します。
- Metadata には resolved source、original Telegram provenance、normalized URL、checksum、MIME type、file size を保存できますが、raw message text、credentials、cookies、page dumps は保存しません。

Testing targets:

- URL normalization と `normalized_url` uniqueness。
- Userinfo、malformed URLs、unsafe schemes、localhost/private IPs、unresolved hosts、redirect limits、unsupported MIME、oversized responses、redirect-to-non-media rejection。
- Direct media と `.mov` handling。
- `og:image`、`og:video`、`twitter:image`、`twitter:player:stream`、`<video>`、`<source>`、`<a>`、page data 内の direct media URLs から generic HTML media discovery を行うこと。
- Single-media provider resolution と multi-media provider skip behavior。
- Raw message text を保存しない Telegram inbox collection。
- Dry-run で files、DB、media-file records を変更しないこと。
- Hidden experimental boundary: normal tool listing はこれらの tools を隠し、normal inspect/run は拒否し、top-level help は hidden command path を露出しません。

## この Phase で完了: Phase 16 Undocumented Telegram Inbox Link Resolver

- [x] Hidden experimental tool boundaries と Telegram inbox link sync CLI routing を追加しました。
- [x] URL normalization、unique `link_queue` storage、SQLite schema version 6 を追加しました。
- [x] Direct media URLs、public single-image Imgur pages、Pixiv artwork-link identification の safe resolver behavior を追加しました。
- [x] Schemes、userinfo、malformed URLs、DNS/private IPs、redirects、MIME types、`.mov`、max media size に対する strict URL safety を追加しました。
- [x] Phase 16 downloads は generic downloader ではなく link-safe GET download を使います。
- [x] `origin_source` を storage platform として保持し、Telegram は ingest provenance として保存します。
- [x] `ISSUES.md` に記録されたすべての Phase 16 acceptance criteria と security issues の regression tests を追加しました。
- [x] Isolated live network smoke verification を実行しました。Real Telegram inbox sync も実行しましたが、その時点の inbox には download 可能な external URL がありませんでした。

## この Phase で完了: Cleanup / Recovery Foundation

- [x] Conservative live-test cleanup/recovery tool として `core.cleanup.media_state` を追加しました。
- [x] `mode: "plan"` は files や SQLite を変更せず matching media items/files を preview できます。
- [x] `mode: "apply"` は `confirm: true` を要求します。
- [x] Apply mode は existing media files を quarantine へ移動してから matching DB state を reset します。
- [x] Tool は platform selector を必須にし、optional `remote_id` と `status` selectors に対応します。
- [x] Credential paths は保護され、actionable cleanup files には含まれません。
- [x] Tests は dry-run no mutation、selector validation、credential protection、quarantine-before-reset、confirmation、path safety を覆っています。

## Later: RuleSpec Policy Layer

Pixiv と Telegram の deterministic platform sync behavior が安定してから行います。

Goal: 各 platform の curation model を hard-code せず、user が source selection と filtering rules を記述できるようにします。

Proposed flow:

```text
explicit URL source or collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM または Agent Core は natural-language intent から RuleSpec への変換を助けられますが、scheduled daemon runs は毎回 LLM に即興判断させず、保存済み deterministic rules を実行するべきです。

## Later: Workflow, Scheduling, And Agentic Composition

- [ ] Pixiv と Telegram の deterministic sync behavior が安定してから YAML Workflow V1 を追加する。
- [ ] Headless workflows が reliable になるまで scheduler は cron/systemd を使う。
- [ ] Tool discovery と safe usage の SKILL documentation を追加する。
- [ ] Platform internals ではなく同じ registry を呼ぶ Agent Core を追加する。
- [ ] Deterministic scheduling が reliable になってから agentic scheduler を追加する。

## この Phase で完了: Telegram Large-Media Hardening

- [x] Real Telethon downloads は `bytes` を返さず planned `.partial` file に直接 stream します。
- [x] Tool-level finalization は completed `.partial` を validate し、chunked checksum を計算し、final path へ atomic move します。
- [x] Real Telegram download call は `timeout_seconds` を enforce します。
- [x] Fake-client tests は `.partial` への streaming と streaming failure 時の partial cleanup を覆っています。
- [x] 1 時間の Telegram video は `${MEDIAGENT_DATA_DIR}/library/telegram/video/2025/08/20250806__telegram__1002602480644-4097-6098041214500608152__v0.mp4` に download 済みです。
- [x] 同じ Telegram sync の再実行で completed long video は skipped になりました。
- [x] `library.file.verify` は 627 files を checked し、627 valid でした。

## 現時点の明示的 Non-Goals

- [ ] Headless Workflow V1 が有用になる前に visual workflow editor を作らない。
- [ ] Bottom/platform tool contracts が安定する前に LLM Agent Core を作らない。
- [ ] Cron-compatible execution が reliable になる前に built-in scheduler を作らない。
- [ ] media browsing、library management、sharing、forwarding、reposting、chat-management features は作らない。
