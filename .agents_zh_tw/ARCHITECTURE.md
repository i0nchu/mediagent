# Mediagent 架構

## 產品邊界

Mediagent 只負責蒐集與下載媒體。它不管理媒體庫、不瀏覽媒體、不 repost、不分享，也不提供 gallery UI。

## Package Layout

```text
src/mediagent/
  cli.py
  core/
  tools/
  platforms/
  workflows/
```

## Core Layer

`src/mediagent/core/` 放共用 primitives：

- `tooling.py`：`ToolSpec`、`ToolContext`、`ToolResult`、permissions、registry errors、`ToolRegistry`
- `db.py`：SQLite schema 與 persistence helpers
- `filesystem.py`：path placeholder expansion、normalization、write-boundary checks
- `auth.py`：credential refs、credential JSON helpers、redacted auth session model
- `http.py`：可測試的 HTTP client abstraction
- `rate_limit.py`：shared rate-limit metadata extraction
- `redaction.py`：secret redaction helpers
- `schema.py`：輕量 JSON-schema-compatible input validation

Core code 不得包含平台特定 API 行為。

## Tool Layer

`src/mediagent/tools/` 放 agent-callable tools。

每個 tool 必須：

- 宣告穩定的 `ToolSpec`
- 宣告 permissions
- 宣告 dry-run support
- 提供 JSON-compatible input/output schemas
- 回傳 `ToolResult`
- 避免洩漏 secrets

CLI、未來 workflows、未來 Agent Core 都必須透過同一個 registry 呼叫工具。

## Platform Layer

`src/mediagent/platforms/` 放平台特定 client、auth、parser。

目前：

- `platforms/x/` 已實作
- `platforms/pixiv/`
- `platforms/telegram/`
- `platforms/reddit/`

`platforms/x/` 目前包含：

- `auth.py`：OAuth 2.0 PKCE、token refresh、credential file support、auth status checks
- `client.py`：X API `/2/users/me` 與 authenticated-user bookmarks
- `parser.py`：把 X tweet/media expansions 轉成 normalized media items

`platforms/pixiv/` 目前包含：

- `auth.py`：local OAuth/PKCE setup、explicit refresh-token auth、token refresh、credential file support、auth session model
- `client.py`：Pixiv App API user detail、bookmarked illustrations、ugoira metadata calls
- `parser.py`：把 Pixiv works 轉成 normalized media items，包含多頁作品與 ugoira metadata

`platforms/telegram/` 目前包含：

- `auth.py`：Telethon-compatible user-session configuration、session-path safety、safe auth-session modeling
- `client.py`：Telegram client boundary，包含 fake-client hooks 與 lazy Telethon usage
- `parser.py`：把 Telegram message/media shapes 轉成 normalized media items，包含 grouped media/albums

`platforms/reddit/` 目前包含：

- `auth.py`：Reddit OAuth config、token exchange/refresh/status、credential file helpers 與 Reddit rate-limit metadata parsing
- `client.py`：Reddit OAuth API `/api/v1/me` 與 authenticated-user saved listings calls
- `parser.py`：把 saved listing entries 轉成 first-version image/gallery/video/direct-media shapes 的 normalized media items

## CLI Flow

```text
CLI args
-> read JSON input
-> create ToolContext
-> find tool in ToolRegistry
-> validate input
-> run tool
-> print JSON or human-readable output
-> return stable exit code
```

Exit codes：

- `0`：success
- `1`：runtime/network/rate-limit failure
- `2`：validation、auth、permission、filesystem、database 或 user input error

## 目前工具執行流

```text
platform collector output
-> media.item.upsert
-> status filtering
-> download.http
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

`x.bookmarks.collect`、`pixiv.bookmarks.collect`、`telegram.messages.collect` 與 `reddit.saved.collect` 負責 collection 與 normalization。Pixiv 與 Telegram 目前都有完整 collect-to-download path 的 deterministic wrappers：`pixiv.bookmarks.sync` 與 `telegram.messages.sync`。X 與 Reddit 在擁有自己的 sync helpers 或 Workflow V1 之前，仍使用手動 CLI/tool composition。

## Future Policy Layer

RuleSpec 是預留的 policy layer，不是已實作 runtime feature。

未來預期形狀：

```text
platform collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM 或 Agent Core integration 可以協助使用者把自然語言意圖轉成 RuleSpec files，但 daemon 與 cron execution 應執行已儲存的 deterministic rules。Platform adapters 不應包含 quality scoring 或 content-preference logic。

## Deferred Workflow Layer

`src/mediagent/workflows/` 目前是 placeholder。除非使用者明確選擇，或下一個平台 foundation 證明 shared sync contract 已足夠穩定，否則不要實作 Workflow V1。
