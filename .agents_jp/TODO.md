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
- Conservative cleanup/recovery foundation は `core.cleanup.media_state` で実装済みです。dry-run planning、manifest output、explicit apply confirmation、quarantine-before-DB-reset behavior、credential path protection に対応します
- Reddit foundation は実装済みです。OAuth config/auth tools、saved-listing collector、image/gallery/video/direct media shapes の media parser、CLI examples、credential path safety、cursor path safety、fake-client tests を含みます

完了済み phase の詳細をここに展開しないでください。今後の作業に直接影響する場合だけ短い baseline note を追加します。

## 現在の焦点：Phase 18 Link Resolver Hardening And Multi-File Readiness

Goal: 最初の Reddit live test が成功した後、link-first resolver path を harden します。ただし unrestricted crawler にはしません。

現在の resolver path は次の形を実証済みです。

```text
explicit user-provided link
-> URL normalization and uniqueness check
-> resolver registry
-> normalized media item
-> existing sync/download/storage pipeline
```

### 18A. Reddit Single-Media Coverage を完了する

- [ ] `redd.it/<post_id>` short URLs が post pages へ安全に redirect する fake-client coverage を追加する。
- [ ] Direct `old.reddit.com` input links の fake-client coverage を追加する。
- [ ] `shreddit-screenview-data` JSON extraction の fake-client coverage を追加する。
- [ ] Clear original `i.redd.it` image が存在する場合、Reddit preview/thumbnail URLs を無視することを tests で証明する。
- [ ] No-media pages、blocked pages、deleted/removed pages、login-required pages、quarantined pages、ambiguous multi-image pages の structured skip tests を追加する。
- [x] Reddit links が `library/reddit/...` に download され、Telegram は `ingested_from` としてのみ残ることを確認する Telegram inbox sync fake-client coverage を追加しました。
- [x] Generic direct-media fallback の前に direct `v.redd.it` MP4 support を追加しました。
- [x] Reddit post/legacy-page から explicit `v.redd.it/...DASH_*.mp4` candidates を抽出できるようにしました。
- [x] Reddit MP4 links が `video`、`v0`、`library/reddit/video/...` に map されることを tests で確認しました。

### 18B. Multi-File Resolver Contract を準備する

- [ ] 現在の public result shape は one resolved media item と互換のまま維持する。
- [ ] One input link から multiple files を返せる internal resolver result shape を draft する。
- [ ] Future multi-file result を既存 media item `metadata.files` format に map する。
- [ ] Multi-file shape が unit tests で覆われるまで Reddit galleries や multi-stream video muxing は enable しない。
- [ ] Storage layout は変更しない：`<platform>/<media_type>/<yyyy>/<mm>/<filename>`。

### 18C. 次の Provider Link Resolvers

- [ ] Bookmark access に依存せず既存 Pixiv auth と artwork parsing を再利用する explicit Pixiv artwork-link resolver を計画する。
- [ ] Explicit X post-link resolver は X bookmark APIs と分けて計画し、login walls と anti-bot limits の扱いを明確にする。
- [ ] Generic HTML resolver は conservative に保つ：single clear public media file のみ、JavaScript 実行なし、credential scraping なし、page dumps 保存なし。

### 18D. Reddit Deferred Scope

- [ ] Reddit OAuth live verification は credentials が利用できない間 pending のままにする。
- [ ] Explicit-link behavior と collector output shape が安定してから `reddit.saved.sync` を検討する。
- [ ] Resolver contract が one link -> multiple files をきれいに扱えるようになってから Reddit galleries を実装する。
- [ ] ffmpeg/dependency strategy と multi-file resolver contract が安定してから Reddit audio muxing、DASH/HLS manifest handling、full multi-file `v.redd.it` support を実装する。
- [ ] Reddit posting、commenting、voting、save/unsave、moderation、chat-management features は追加しない。

### 18E. Reddit Video Mux と Managed FFmpeg 計画

- [ ] Mediagent が project-local ffmpeg binary を管理するか、明示的な `MEDIAGENT_FFMPEG_PATH` を受け付けるか、または両方に対応するか決める。
- [ ] PATH を変更せず、version と supported codecs を報告する tool-safe ffmpeg capability check を追加する。
- [ ] Reddit video/audio tracks を別々に download して 1 つの final file に mux できるよう、one media item with multiple source files を計画する。
- [ ] Muxing が未実装の間も direct single MP4 video-only downloads support は維持する。
- [ ] Audio-only MP4 candidates が user-facing video files として保存されないことを tests で確認する。

## Side Decisions

- [ ] X live OAuth verification は未実施。API access に paid credits が必要な可能性がある。
- [ ] Phase 18 hardening の完了後に X と Pixiv の explicit-link resolvers を計画し、inbox automation が bookmark/feed access に依存せず explicit post/artwork links から download できるようにする。
- [ ] Pixiv に bookmarks 以外の source tools が必要か議論する。例：following-user works、explicit artwork IDs。
- [ ] User が明示的に promote を決めるまで Telegram link resolver behavior は undocumented のままにする。
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
- `link_queue.normalized_url` を uniqueness key にし、同じ link の重複投入で duplicate work が発生しないようにします。
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
platform collector
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
