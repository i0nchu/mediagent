# Mediagent Focused TODO

このファイルは、次に行う実装・検証作業だけを追跡します。完了済みの状態、live-test 履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新するときは、同じ変更で英語版と繁体字中国語版も更新してください:

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Current Focus: Remaining Missing-File Policy Decision

Goal: bounded repair run 後も missing local files を指している 6 件の historical Reddit file records をどう扱うか決めます。

Explicit repair path は実装済みで live-tested です。Resolve 可能だった missing files は修復済みです。Remaining records は通常の downloader failure ではありません。Source URLs が現在 Reddit login wall に当たり、resolver は `requires_auth:login_required` を返します。

## Decision Tasks

- 残り 6 件の Reddit rows を known historical missing records として残すか決めます。
- `core.cleanup.media_state` でこれらの records を reset または quarantine するか決めます。
- Reddit login-wall repair に新しい resolver/auth work を投資する価値があるか、Reddit OAuth/saved collection と一緒に deferred のままにするか決めます。
- Fresh dry-run と user の明示的 approval なしに、full live DB へ broad repair を実行しません。

## Acceptance Notes

- User が cleanup または新しい Reddit auth/resolver work を選ばない限り、current live verification は 669 valid files と 6 missing files のままです。
- Reddit login-wall limitation を解決するまでは、どの agent も remaining 6 rows を newly discovered downloadable media として扱ってはいけません。
- Repair feature 自体は complete とみなします。Future work は product policy または provider capability であり、original DB-state bug ではありません。

## Deferred Candidates

- X explicit post-link feasibility。
- Instagram session-status TTL と long-running cron verification。
- Telegram inbox を experimental wrapper から documented URL input source へ promote する作業。
- Reddit/Redgifs follow-up は、新しい explicit-link examples が必要とする場合だけ行います。
- Workflow V1 は、link-first provider adapters が repeated runs でも安定してから開始します。
- Agent Core / SKILL integration は、deterministic tools と workflow boundaries が安定してから開始します。
