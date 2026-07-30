# Mediagent Focused TODO

このファイルは次に実装する作業だけを追跡します。完了済み status、live-test history、resolved issues は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新する時は、同じ変更で英語版と繁体字中国語版も同期してください。

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Current Focus: Phase 21 Pixiv Explicit Link Resolver

Goal: user-provided Pixiv artwork URLs を、Instagram、Reddit、Redgifs、generic public links と同じ link-first pipeline で扱えるようにします。

Primary flow:

```text
Pixiv artwork URL または artwork id
-> Pixiv URL/id normalization
-> 既存 Pixiv auth/session handling
-> Pixiv artwork detail fetch
-> normalized media candidates
-> link.media.sync
-> scanner-friendly storage
```

この phase は explicit links が対象であり、bookmark/feed discovery ではありません。既存の Pixiv bookmark sync は維持しますが、新規機能はできるだけ shared resolver/download/storage pipeline を再利用してください。

## Product Scope

- [ ] `https://www.pixiv.net/artworks/<illust_id>` を support する。
- [ ] `https://www.pixiv.net/en/artworks/<illust_id>` のような localized artwork paths を support する。
- [ ] Pixiv-specific tool では direct `illust_id` input を support する。
- [ ] 1 つの artwork URL は artwork 全体を表すものとして扱う。
- [ ] Multi-page illustration/manga works は default ですべての original pages を resolve する。
- [ ] Page selection hints があっても、future explicit option が追加されるまでは metadata としてのみ保存する。
- [ ] 既存 Pixiv item identity を再利用する: `platform = "pixiv"`、`remote_id = <illust_id>`。
- [ ] Explicit-link downloads は `pixiv.bookmarks.sync` が既に download した items/files と dedupe できるようにする。

## Non-Goals

- [ ] この phase では Pixiv feed、following-user、ranking、search、tag、recommendation、user-profile collection を追加しない。
- [ ] Media browsing、gallery UI、reposting、commenting、bookmarking、liking、account mutation を追加しない。
- [ ] この phase では ugoira frames を video に変換しない。
- [ ] App API detail path が使える場合、広範な Pixiv HTML scraping を実装しない。
- [ ] この phase から Workflow V1、built-in scheduling、RuleSpec、Agent Core work を開始しない。

## 21A. Resolver Contract

- [ ] Core link resolver registry に `pixiv_artwork_link` という Pixiv resolver を追加する。
- [ ] Accepted URLs を `https://www.pixiv.net/artworks/<illust_id>` に normalize する。
- [ ] Equivalent localized Pixiv artwork URLs の aliases を emit する。
- [ ] `origin_source: "pixiv"`、canonical URL、remote id、resolver name、source timestamp、author metadata、media candidates を含む `LinkResolution` を返す。
- [ ] Resolved artwork files を、stable `file_index`、`part`、`media_type`、`mime_type`、`extension`、`content_identity`、source timestamp を持つ `MediaCandidate` に変換する。
- [ ] Multi-page artwork は 1 つの Pixiv media item として扱い、複数 file candidates を持たせる。
- [ ] 取得できる場合は title、caption、tags、author id/name、create date、dimensions、Pixiv type、page count、`x_restrict`、visibility、bookmark/view counts を保持する。
- [ ] Unsupported URLs、missing artwork id、private/deleted/unavailable artwork、rate limits、auth failure、unsupported media type、resolver failure は structured skips を返す。

## 21B. Pixiv API And Auth Boundary

- [ ] Pixiv App API artwork detail behavior を使う `pixiv_client.get_illust_detail` を追加する。
- [ ] `pixiv.auth.status` / `pixiv.auth.refresh` の既存 credential loading と refresh behavior を再利用する。
- [ ] `pixiv_artwork_link` は configured Pixiv session を使ってよいが、自分で browser login を開始してはいけない。
- [ ] Credentials が missing/invalid の場合は、必要に応じて `pixiv.auth.login` または `pixiv.auth.refresh` を recommend する agent-decidable errors を返す。
- [ ] Credential files は allowed write roots 内に留める。
- [ ] Access tokens、refresh tokens、authorization codes、client secrets、raw upstream auth payloads を outputs、logs、metadata、tests に出さない。
- [ ] Pixiv rate-limit または temporary block responses は structured rate-limit errors にし、tight retry loops を避ける。

## 21C. Public Tool Surface

- [ ] Download せずに 1 件の Pixiv artwork URL/id を inspect する `pixiv.link.resolve` を追加する。
- [ ] Pixiv resolver を登録し、`link.media.sync` が Pixiv artwork URLs を直接 download できるようにする。
- [ ] `pixiv.link.resolve` は platform-bound に保つ。Non-Pixiv hosts はこの tool で resolve 成功してはいけない。
- [ ] `examples/tools/pixiv.link.resolve.json` を追加する。
- [ ] 3 言語の `TOOL_CATALOG.md` と `RUNBOOK.md` に Pixiv explicit-link commands を追加する。
- [ ] CLI path は単純に保つ。Credentials 設定後、user は `mediagent link sync <pixiv artwork url>` を実行できるべきです。

## 21D. Download And Storage Behavior

- [ ] Upsert、dedupe、path planning、download、sidecar metadata、file records、item status は既存 `link.media.sync` orchestration を使う。
- [ ] Pixiv image downloads は必要な safe Pixiv `Referer` behavior を使う。
- [ ] Credential-bearing headers や raw tokens を永続化しない。
- [ ] Files は現在の scanner-friendly layout に保存する。
- [ ] Shared root では path は `library/pixiv/photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>` のようになる。
- [ ] `MEDIAGENT_PIXIV_LIBRARY_DIR` では duplicate platform layer を省き、`photo/<yyyy>/<mm>/<yyyymmdd>__pixiv__<illust_id>__p0.<ext>` のようになる。
- [ ] 既存 media/file status rules を使う。Rerun は downloaded files を skip し、failed items は明示された場合だけ retry し、multi-page partial failure は item を `partial` にする。

## 21E. Ugoira Policy

- [ ] Detail flow で ugoira metadata が得られる場合は、既存 ugoira metadata parsing を再利用する。
- [ ] First-version ugoira output は、現在の Pixiv bookmark-sync capability と同じく source zip candidate として表現する。
- [ ] Future tooling が convert/index できるように ugoira metadata を明確に mark する。
- [ ] Detail-based ugoira resolution をこの phase で安全に実装できない場合、partial conversion behavior を作らず `unsupported_media_type` を返す。

## 21F. Tests

- [ ] Pixiv artwork URL/id parsing と canonicalization の unit tests。
- [ ] Localized URL alias handling の unit tests。
- [ ] `pixiv_client.get_illust_detail` request shape の fake HTTP unit tests。
- [ ] Single-page artwork resolution の unit tests。
- [ ] Multi-page artwork resolution と candidate ordering の unit tests。
- [ ] Ugoira zip candidate または structured skip behavior の unit tests。
- [ ] Auth missing、auth refresh failure、rate limit、deleted/private artwork、unsupported URL errors の unit tests。
- [ ] `pixiv.link.resolve` platform boundary と secret redaction の unit tests。
- [ ] Pixiv artwork URL を使う `link.media.sync` の unit tests。Pixiv `Referer`、既存 Pixiv bookmark records との dedupe、sidecar metadata、scanner-friendly layout を含める。
- [ ] DB/file writes が発生しない dry-run behavior の unit tests。

## 21G. Verification

- [ ] Full default test suite を実行する: `uv run --locked python -m unittest discover -s tests`。
- [ ] `uv lock --check` を実行する。
- [ ] CLI JSON で `pixiv.link.resolve` を inspect する。
- [ ] `pixiv.link.resolve` で Pixiv artwork URL を 1 件 dry-run する。
- [ ] `link.media.sync` で Pixiv artwork URL を 1 件 dry-run する。
- [ ] Live bulk verification は、後で他 platform と一緒に long-running verification するまで deferred にする。

## Later Candidates

以下は Phase 21 の範囲外です。

- [ ] X explicit post-link feasibility。
- [ ] Instagram session-status TTL と long-running cron verification。
- [ ] Telegram inbox を experimental wrapper から documented URL input source へ promote する。
- [ ] 新しい explicit-link examples が必要とする場合だけ Reddit/Redgifs follow-up を行う。
- [ ] Link-first provider adapters が repeated runs でも安定した後に Workflow V1 を開始する。
- [ ] Deterministic tools と workflow boundaries が安定した後に Agent Core / SKILL integration を開始する。
