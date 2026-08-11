# Mediagent Focused TODO

このファイルは、今後の実装・検証作業だけを追跡します。完了済みの状態、live-test 履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新するときは、同じ変更で英語版と繁体字中国語版も更新してください:

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Remaining Focus: Instagram 保存済みメディア Live Verification

Offline foundation は実装済みで `STATE.md` に記録されています。残作業は以下の operator-controlled live-test gate のみです。

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
