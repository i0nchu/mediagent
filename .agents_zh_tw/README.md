# Mediagent Agent 接手指南

這個目錄是繁體中文交接文件。英文預設文件在 `.agents/`，日文文件在 `.agents_jp/`。

## 建議閱讀順序

1. `STATE.md`：了解目前狀態與下一個建議任務。
2. `ARCHITECTURE.md`：了解 package layout 與資料流。
3. `TOOL_CATALOG.md`：查看目前有哪些工具。
4. `RUNBOOK.md`：安全地跑測試與 smoke check。
5. `ISSUES.md`：查看目前 caveats。
6. `TODO.md`：了解後續 roadmap 摘要。
7. `RULES.md`：改檔前先讀開發規範。

## 目前狀態

Mediagent 目前是 agentic-ready 的工具底座，還不是完整 workflow agent。

已支援：

- `src/mediagent/` Python package
- `mediagent tools ...` CLI bridge
- tool contract 與 registry
- env、DB、path、run record、sync cursor、media item、media file、HTTP download、metadata 等底層工具
- credential/auth primitives 與 redacted session status
- X OAuth PKCE、token exchange/refresh/status、bookmark collection tools
- Pixiv local OAuth/PKCE setup、explicit refresh-token auth、token refresh/status、bookmark collection tools
- Telegram user-session media source tools，包含 explicit local login、auth status、dialog listing、message/link-inbox collection、Telegram-specific media download 與 deterministic message sync
- Reddit OAuth config/auth tools 與 saved-media collection tools
- unit tests、CLI smoke tests、fake HTTP clients 與 fixture responses

尚未實作：

- Workflow V1
- 內建 scheduler
- Instagram support
- LLM Agent Core

X 與 Reddit 工具有 fake HTTP / fixture 測試，但尚未用真實帳號做現場驗證。Pixiv auth、collection、deterministic bookmark sync 與 universal storage layout 已有 fake HTTP / fixture 測試；Pixiv 也已完成使用者協助的 live storage verification，包含一次 100 個 items / 624 個 files 的 bounded `scanner-friendly-v2` layout run。Telegram foundation 已包含 explicit login、curated link-inbox support、stream-safe real downloads、layout placement、重跑去重，以及小型媒體與一小時影片 live verification。

## 常用命令

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

## 重要方向

下一步不要先做 Agent Core 或 Workflow V1，除非使用者明確改變方向。接下來較可能的優先項目是使用者提供 credentials 後做 Reddit live verification、實作 `reddit.saved.sync`、Pixiv 額外 source 討論，或在 credentials/API access 可用時做 X live verification。
