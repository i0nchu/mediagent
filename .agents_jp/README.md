# Mediagent Agent 引き継ぎガイド

このディレクトリは日本語の引き継ぎ資料です。英語の既定資料は `.agents/`、繁体字中国語版は `.agents_zh_tw/` にあります。

## 推奨読書順

1. `STATE.md`: 現在の状態と次の推奨作業を確認します。
2. `ARCHITECTURE.md`: package layout とデータフローを理解します。
3. `TOOL_CATALOG.md`: 現在あるツールを確認します。
4. `RUNBOOK.md`: テストと smoke check を安全に実行します。
5. `ISSUES.md`: 現在の caveats を確認します。
6. `TODO.md`: 今後の roadmap 概要を確認します。
7. `RULES.md`: ファイルを編集する前に開発規約を読みます。

## 現在の状態

Mediagent は現在、agentic-ready なツール基盤です。まだ完全な workflow agent ではありません。

対応済み:

- `src/mediagent/` Python package
- `mediagent tools ...` CLI bridge
- tool contract と registry
- env、DB、path、run record、sync cursor、media item、media file、HTTP download、metadata などの bottom tools
- credential/auth primitives と redacted session status
- X OAuth PKCE、token exchange/refresh/status、bookmark collection tools
- Pixiv local OAuth/PKCE setup、explicit refresh-token auth、token refresh/status、bookmark collection tools
- Telegram user-session media source tools。explicit local login、auth status、dialog listing、message/link-inbox collection、Telegram-specific media download、deterministic message sync を含みます
- Reddit OAuth config/auth tools と saved-media collection tools。ただし現在は deferred legacy/advanced capability として保持します
- `link.queue.upsert` と `link.media.sync` による first-class link-first tools、および direct media、bounded single-media HTML、Imgur、Pixiv artwork links、anonymous Reddit explicit links/static galleries、Redgifs direct/watch links の resolver foundations
- Instagram explicit-link foundation tools。Saved-session status/login/ensure-session と、`link.media.sync` 経由の post/Reel 全体 resolution/download を含みます
- unit tests、CLI smoke tests、fake HTTP clients、fixture responses

未実装:

- Workflow V1
- built-in scheduler
- LLM Agent Core

X auth/bookmark collection と Reddit auth/saved collection は fake HTTP / fixture tests で検証済みですが、現在の主要な拡張路線ではありません。Pixiv auth、collection、deterministic bookmark sync、universal storage layout は fake HTTP / fixture tests で検証済みです。Pixiv は user-assisted live storage verification も完了しており、100 items / 624 files の bounded `scanner-friendly-v2` layout run も確認済みです。Telegram foundation は explicit login、curated link-inbox support、stream-safe real downloads、layout placement、rerun dedupe、小さな media と 1 時間 video の live verification を含みます。Instagram explicit-link foundation は user-provided public post、carousel、Reel links を saved local session で扱い、live verification 済みです。

現在の product direction は link-first です。Users、cron jobs、workflows、Telegram inboxes、future agents が explicit URLs を提供し、Mediagent は安全に download 可能な media candidates を resolve してから既存 storage/download pipeline を再利用します。User が明示的に再開しない限り、auth-assisted account collection は optional legacy/advanced behavior として扱います。Pixiv bookmarks はすでに動作していて有用なため例外として維持します。

## よく使うコマンド

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.sync --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 重要な方針

ユーザーが明示的に方向転換しない限り、次に Agent Core、Workflow V1、Reddit saved sync、X live auth verification を作らないでください。Link-first baseline が現在の主要 product path です。Stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>`、queue claim/retry scheduling、canonical/media identity dedupe、Reddit external-provider delegation、Redgifs downloads、Instagram whole-post downloads、simple multi-candidate partial-success handling を含みます。Resolver behavior は default で bounded に保ち、今後の platform work は account/bookmark collectors より explicit-link provider adapters を先に拡張してください。
