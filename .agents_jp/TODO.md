# Mediagent Focused TODO

このファイルは、今後の実装・検証作業だけを追跡します。完了済みの状態、live-test 履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新するときは、同じ変更で英語版と繁体字中国語版も更新してください:

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Current Focus: Instagram 保存済みメディア Foundation

Goal: 実証済みの Pixiv bookmark architecture を参考に、Instagram saved session と private API の制約を守る deterministic な保存済みメディア収集・同期 tools を追加します。

通常の source workflow:

`saved feed -> posts/resources の normalize -> items の upsert -> dedupe/status filter -> storage plan -> download -> file/item state の記録`

保存済みメディアの logic は Instagram platform layer と tool layer に置きます。共通 downloader、storage planner、DB state、repair behavior、session boundary を再利用し、別の download pipeline は作りません。

### 1. Platform Client と Normalization

- [ ] Configured saved session を使い、1 回に 1 page を読む bounded Instagram saved-feed client operation を追加します。
- [ ] Cookies、authorization headers、signed media URLs、raw session settings を公開せず、page items と opaque next-page cursor を返します。
- [ ] Photo、Reel/video、carousel posts を既存の media item/file model に normalize します。
- [ ] 1 saved post を 1 media item とし、carousel の全 downloadable resources を file candidates に含めます。
- [ ] Stable source identity、shortcode/media ID、author、source timestamp、canonical post URL、安全な caption metadata、resource index、media type を保持します。
- [ ] Runtime download URLs と credential-bearing request context は memory 内だけに保持します。
- [ ] Login expiry、checkpoint/challenge、rate limit、private/unavailable media、temporary request failures を既存の structured Instagram error codes に map します。

### 2. `instagram.saved.collect`

- [ ] 保存済み Instagram posts 用の stable deterministic collector を追加します。
- [ ] Operator tests と controlled runs 向けに bounded `limit` と `max_pages` inputs をサポートします。
- [ ] Arbitrary item limit なしで explicit full collection が要求された場合、feed exhaustion まで paginate します。
- [ ] Pages fetched、raw posts、normalized items、resource counts、next cursor、stop reason を含む collection summary を返します。
- [ ] Files を download せず、media item/file state を変更しません。
- [ ] Dry-run は Instagram を呼び出したり state を書き込んだりせず、configuration validation と request plan の説明だけを行います。
- [ ] 既存の saved-session boundary を使い、unbounded login loop を自動実行せず actionable auth errors を返します。

### 3. `instagram.saved.sync`

- [ ] Collection と既存 DB、storage、download、status helpers を組み合わせる stable sync tool を追加します。
- [ ] 実用的な範囲で Pixiv-compatible semantics を使い、`full_sync`、`stop_on_known`、`limit`、`max_pages`、`store_cursor`、`retry_failed`、`repair_missing_files`、`write_sidecar_metadata` をサポートします。
- [ ] Recurring sync は newest saved posts から scan し、known terminal item に到達したら停止します。古い pagination cursor だけを source of truth にしません。
- [ ] Explicit full sync は feed exhaustion まで続け、tool-layer item/file dedupe が healthy completed media を skip します。
- [ ] Durable cursor/source state は successful、untruncated boundary の後だけ保存し、partial/failed runs では進めません。
- [ ] Scanner-friendly storage `<library_root>/instagram/<media_type>/<yyyy>/<mm>/...` を再利用します。
- [ ] Complete-post behavior を維持し、carousel の全 resources が download されるまで parent item を downloaded にしません。
- [ ] Partial/failed file/item state を記録し、後続の `retry_failed` と `repair_missing_files` で recovery できるようにします。
- [ ] Collected、known、queued、downloaded、partial、failed、repaired、skipped、files、bytes の concise summary を返します。

### 4. Agent と CLI Integration

- [ ] 両 tools を default tool registry に登録し、machine-readable inspect schemas を公開します。
- [ ] Bounded collect、recurring sync、explicit full sync 用の stable JSON examples を追加します。
- [ ] Agent Core が saved-media sync と explicit-link download を区別できるように、英語の `instagram_saved_sync` SKILL を追加します。
- [ ] 「すべての Instagram 保存済みメディア」という natural-language request に、捏造した `limit` / `max_pages` を付けないことを保証します。
- [ ] Explicit post/Reel URL request は既存 Instagram link-download SKILL に route します。

### 5. Safety と Rate Limits

- [ ] V1 は conservative sequential page requests を使い、Instagram feed を concurrent crawl しません。
- [ ] Rate limit、checkpoint/challenge、invalid session では current run を停止し、tight retry は行いません。
- [ ] Account passwords、session cookies、signed CDN query parameters、raw private-API payloads を永続化しません。
- [ ] Default tests は完全 offline とし、private saved content や identifiable account data を含まない fake clients と minimized fixtures を使います。

## Automated Verification

- [ ] Unit tests は empty saved feed、1 photo、1 Reel/video、1 multi-resource carousel、pagination をカバーします。
- [ ] Collector tests は bounded limits、feed exhaustion、dry-run no-network、structured auth/rate-limit failures をカバーします。
- [ ] Sync tests は first download、second-run dedupe、stop-on-known recurring sync、full sync、partial carousel failure、retry、missing-file repair、安全な storage paths、failure/truncation 時の cursor non-advancement をカバーします。
- [ ] Agent tests は bounded requests、recurring update requests、unbounded "all saved media" requests をカバーします。
- [ ] `uv run --locked python -m unittest discover -s tests` が通ります。
- [ ] `uv lock --check` が通ります。
- [ ] `git diff --check` が通ります。

## Local Live-Test Gate

- [ ] `/home/ion/projects/mediagent` の configuration、DB、temporary library、saved Instagram session だけを使います。Development verification 中に `/data/services` または `/data/nas` へアクセスしません。
- [ ] Saved session を 1 回 check し、private URLs や account details を log せず bounded 1 page だけ collect します。
- [ ] 少数の bounded saved posts を dedicated local live-test library に sync します。
- [ ] Bounded sample に carousel と Reel/video が含まれる場合、carousel の全 resources と有効な Reel/video file を確認します。
- [ ] 同じ bounded sync を再実行し、healthy files が dedupe され duplicate download が 0 であることを確認します。
- [ ] Dedicated live-test scope に対して `library.file.verify` を実行します。
- [ ] Redacted summary を記録後、local live-test media、DB、temporary output を削除します。
- [ ] Automated verification と bounded live test が通った後だけ feature branch を `main` に merge します。

## After This Focus

- Systemd deployment MVP environment-check profile を完成します。
- Overlapping timer runs を防ぐ run lock または lease guard を追加します。
- Systemd journal 用 Agent Core summary-only output を追加します。
- Pixiv `stop_on_known` を source-aware にします。
- 文書化済みの timer-safe auth、rate-limit、cursor failure policy を追加します。

## Deferred To V2 Or Later

- Long-running daemon process。
- Built-in または agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory と multi-turn conversation state。
- Workspace-scoped command execution と broad library-management workflows。
- X explicit post-link support。Tweet reads には現在 paid credits が必要です。
