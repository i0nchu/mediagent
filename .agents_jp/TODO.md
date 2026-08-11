# Mediagent Focused TODO

このファイルは、次に行う実装・検証作業だけを追跡します。完了済みの状態、live-test 履歴、解決済み issue は `STATE.md`、`ISSUES.md`、`RUNBOOK.md` に置きます。

この TODO を更新するときは、同じ変更で英語版と繁体字中国語版も更新してください:

- `.agents/TODO.md`
- `.agents_zh_tw/TODO.md`

## Recently Completed Gate: Clean-State Agent Full-Source Verification

Goal: Agent Core が deployment-style natural-language tasks を解釈し、"all" を arbitrary limits に downgrade しないことを証明します。

Timer hardening に戻る前に完了しました:

- [x] Active SQLite DB と `mediagent-data/library` を、旧 live-test state を保持せず rebuild します。
- [x] Execute mode で `mediagent agent run "下載所有 telegram inbox 內所有可下載的媒體來源"` を実行します。
- [x] Selected tool が `telegram.inbox.sync_links` で、`full_sync:true` を含み、`limit` / `max_messages` を捏造していないことを確認します。
- [x] Execute mode で `mediagent agent run "下載 pixiv bookmark 所有可下載媒體來源"` を実行します。
- [x] Selected tool が `pixiv.bookmarks.sync` で、`full_sync:true`、`stop_on_known:false` を含み、`limit` / `max_pages` を捏造していないことを確認します。
- [x] 同じ 2 tasks を再実行し、tool-layer dedupe が duplicate downloads を防ぐことを確認します。
- [x] `library.file.verify` で downloaded file health を確認します。

## Current Focus: Agent-Mode systemd Timer Deploy MVP

Goal: Long-running daemon を作る前に、Mediagent を Agent Core を呼ぶ保守的な timer-driven background service として deploy できる状態にします。

Production timer entry は direct deterministic tools ではなく、`mediagent agent run "<task>"` を呼ぶべきです。Deterministic tools は Agent Core、regression tests、debugging、explicit operator verification の安全な bottom layer として残します。

最初の agent-mode service target は Telegram inbox sync です。これは recurring content intake を表します: configured inbox を scan し、新しい links を resolve し、supported media を download し、DB/file state を保存し、次回 run では stored cursor の後から続行します。

2 つ目の timer-safe source は Pixiv bookmark sync です。Pixiv は Telegram のような単純な "messages after cursor" model を持たないため、service path は newest bookmarks から scan し、known terminal item に到達したら停止し、bounded `max_pages` safety cap を使います。

## P0 Gate: Telegram Inbox Message-Link Bridge

- [x] Inbox 内の public `t.me/<channel>/<message_id>` と private `t.me/c/<chat>/<message_id>` links を Telegram message sync に渡し、external URLs は引き続き link resolver pipeline で処理します。
- [x] Telegram native media に inbox chat/message/date/run provenance を保持し、protected または inaccessible な linked messages は structured skips として返します。
- [x] `telegram.inbox.sync_links` と `link.media.sync` に `retry_auth_skipped` を追加し、platform session が usable になった後で旧 `requires_auth` / `login_wall` queue rows を再試行できるようにします。
- [x] Public、private、inaccessible、protected、external と Telegram の mixed case、auth retry paths を fake-client tests でカバーします。
- [ ] Public link、accessible private link、inaccessible link、session が復旧した downstream platform を含む bounded live inbox check を 1 回実施します。Production DB を手動 reset しないでください。

## Remaining Deployment MVP Tasks

- [ ] Deployment-oriented environment check profile を追加します:
  - `MEDIAGENT_DATA_DIR`
  - `MEDIAGENT_DB_PATH`
  - `MEDIAGENT_LIBRARY_DIR`
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`
  - `TELEGRAM_SESSION_FILE`
  - `MEDIAGENT_TELEGRAM_INBOX_KEY`
  - `MEDIAGENT_TELEGRAM_INBOX_CHAT_ID`、`MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME`、または `MEDIAGENT_TELEGRAM_INBOX_CHAT` のいずれか
- [ ] 同じ inbox を同時処理しないように run-lock または lease guard を追加します。
- [ ] `systemd` Agent Core runs 用の summary-only service output を追加します。現在の full JSON output は full artifact lists と nested resolution payloads を含むため、journal には大きすぎます。
- [ ] Pixiv `stop_on_known` を source-aware にし、他 source から download された explicit Pixiv links が clean-state rebuild 中の bookmark sync を早く止めないようにします。
- [ ] Timer-safe failure policy を追加します:
  - auth/session failures は current run を停止します
  - rate limits は current run を停止し、tight retry loops は行いません
  - partial downloads は Telegram cursor を進めません

## Acceptance Criteria

- [x] Clean checkout は `.env.example` から設定できます。
- [ ] `core.env.check` または equivalent CLI path が missing Telegram inbox deployment settings を検出できます。
- [ ] Dry-run agent-mode timer command は user が tool input に `chat` を渡さなくても configured inbox を resolve できます。
- [x] Execute agent-mode timer command は新しい inbox media を download し、`links:<inbox_key>` cursor state を保存できます。
- [x] 2 回目の run は stored cursor の後から始まり、同じ inbox links を再 download しません。
- [x] Pixiv bookmark timer runs は newest bookmarks を scan し、known terminal items で停止し、`MEDIAGENT_LIBRARY_DIR` が変わっても downloaded artworks を再 download しません。
- [ ] Overlapping timer runs は防止されるか、download 前に clean failure します。
- [x] Runbook は downloaded files の保存場所を説明します。

## Deferred To V2 Or Later

- Long-running daemon process。
- Built-in scheduler。
- Agentic scheduler。
- RuleSpec generation。
- Visual workflow editor。
- Long-term memory。
- Multi-turn conversation state。
- Selected SKILL を超える broad autonomous planning。
- Workspace-scoped command execution。
- Library rebuild / management workflows。
- Long-running progress または structured streaming。
- X explicit post-link support。X API tweet reads は現在 paid credits が必要なため。
