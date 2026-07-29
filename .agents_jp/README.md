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
- Reddit OAuth config/auth tools と saved-media collection tools
- unit tests、CLI smoke tests、fake HTTP clients、fixture responses

未実装:

- Workflow V1
- built-in scheduler
- Instagram support
- LLM Agent Core

X と Reddit tools は fake HTTP / fixture tests で検証済みですが、実 account による live verification はまだです。Pixiv auth、collection、deterministic bookmark sync、universal storage layout は fake HTTP / fixture tests で検証済みです。Pixiv は user-assisted live storage verification も完了しており、100 items / 624 files の bounded `scanner-friendly-v2` layout run も確認済みです。Telegram foundation は explicit login、curated link-inbox support、stream-safe real downloads、layout placement、rerun dedupe、小さな media と 1 時間 video の live verification を含みます。

## よく使うコマンド

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.sync --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect reddit.saved.collect --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 重要な方針

ユーザーが明示的に方向転換しない限り、次に Agent Core や Workflow V1 を作らないでください。次の優先候補は user-provided credentials による Reddit live verification、`reddit.saved.sync`、Pixiv additional source discussion、または credentials/API access がある場合の X live verification です。
