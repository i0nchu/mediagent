# Mediagent Focused TODO

## Comic source follow-up

- [ ] 新しい nhentai browser cookie を再 export し、complete favorites collect/sync live test を再実行する。以前 user 検証済みの cookie は現在 HTTP 401。
- [ ] Expired cookie recovery と browser re-import を live test する。Provider は refresh を HTTP 403 で拒否したため、password／CAPTCHA automation を追加せず、自動 renewal も前提にしない。
- [x] JM credential login/session reuse、3-page favorite pagination、42 albums／1,081 chapters full dry-run、108-page bounded real favorite sync と CBZ/dedupe を live 検証した。
- [x] Shared run lock と summary-only journal output を使う system-level comic favorite timer example を追加した。
- [x] Remote で expired になった JMComic session を run ごとに最大 1 回 credential login で recovery し、collection／album resolve 後に rotated cookie を checkpoint、initial full sync timeout を 18 時間にした。
- [x] Valid JMComic 1-12 px spacer strips を ignored non-content に分類し、CBZ／page count から除外して repair の再試行対象外にした。
- [x] JMComic remote name/FID/URL multi-folder selection、local alias fallback、atomic union membership、selection-change follow semantics を追加し、named folder 7 items と aggregate All 49 items を live verify した。
- [ ] JMComic folder-selection を server に deploy し、最初は numeric FID で target folder を選択、service snapshot を確認してから timer に戻す。

このファイルは、今後の実装・検証作業だけを追跡します。完了済みの状態、live-test 履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新するときは、同じ変更で英語版と繁体字中国語版も更新してください:

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Current Focus: systemd Timer Hardening

Goal: 別の long-running source または scheduler layer を追加する前に、既存の Agent-mode timer deployment を強化します。

- [ ] Enabled Telegram inbox、Pixiv bookmark、optional Instagram saved-media sources 用の deployment-oriented environment-check profile を追加します。
- [ ] Overlapping timer runs が collection/download の開始前に clean failure するよう、run lock または lease guard を追加します。
- [ ] Systemd journal 向け Agent Core summary-only output を追加し、full artifact と nested resolution payloads は default で省略します。
- [ ] Pixiv `stop_on_known` を source-aware にし、別 source から download された explicit Pixiv link が bookmark sync を早期停止しないようにします。
- [ ] 一貫した timer-safe failure policy を適用します:
  - auth/session と checkpoint failures は current platform run を停止します
  - rate limits は tight retry loop を行わず停止します
  - partial downloads は durable source state を進めません
  - successful recurring runs は DB/file state で dedupe されます
- [ ] Hourly Telegram/Pixiv tasks と optional conservative Instagram saved-media task の system-level deployment examples を追加または更新します。

## Acceptance Criteria

- [ ] Clean checkout は platform に接続せず、enabled timer settings をすべて検証できます。
- [ ] 同じ source の 2 つの overlapping runs は concurrent download できません。
- [ ] Journal output は run ごとに 1 つの concise redacted final summary を出します。
- [ ] Existing Telegram/Pixiv recurring commands は intended source state から続行し、duplicate download を行いません。
- [ ] Instagram saved-media recurring sync は `stop_on_known:true` と bounded page cap を使い、item limit を捏造しません。
- [ ] Auth、checkpoint、rate-limit、partial-download、lock-contention paths に focused offline tests があります。
- [ ] `uv run --locked python -m unittest discover -s tests`、`uv lock --check`、`git diff --check` が通ります。

## Deferred To V2 Or Later

- Long-running daemon process。
- Built-in または agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory と multi-turn conversation state。
- Workspace-scoped command execution と broad library-management workflows。
- X explicit post-link support。Tweet reads には現在 paid credits が必要です。
- Normalized `metadata.comic` contract を使う additional authorized comic-source adapters。Provider access、page ordering、series identity、policy boundaries は adapter-specific のままにします。
