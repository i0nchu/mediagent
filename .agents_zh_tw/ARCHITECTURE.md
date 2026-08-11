# Mediagent 架構

## 產品邊界

Mediagent 只負責蒐集與下載媒體。它不管理媒體庫、不瀏覽媒體、不 repost、不分享，也不提供 gallery UI。

## Package Layout

```text
src/mediagent/
  cli.py
  agent/
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

## Agent Core V1 Layer

`src/mediagent/agent/` 包含目前的本機 Agent Core V1 preview：

- `skills/`：Markdown SKILL loading 與內建英文 SKILL files
- `llm/`：Ollama client boundary
- `prompts.py`：strict JSON skill/action prompt builders
- `actions.py` 與 `schema.py`：action parsing 與 structured agent run contracts
- `core.py`：SKILL-scoped run loop

Agent Core V1 不是 scheduler，也不是廣泛 autonomous planner。它必須選擇一個 SKILL、只透過 `ToolRegistry` 呼叫該 SKILL allowlist 中的工具、在沒有 SKILL 明確符合時於 tool call 前拒絕 unsupported tasks、把模型的 dry-run 選擇正規化到全域 runtime mode，並移除使用者沒有明確提供的 destination paths。

## Platform Layer

`src/mediagent/platforms/` 放平台特定 client、auth、parser。

目前：

- `platforms/x/` 已實作
- `platforms/pixiv/`
- `platforms/telegram/`
- `platforms/reddit/`
- `platforms/instagram/`

`platforms/x/` 目前包含：

- `auth.py`：OAuth 2.0 PKCE、token refresh、credential file support、auth status checks
- `client.py`：X API `/2/users/me` 與 authenticated-user bookmarks
- `parser.py`：把 X tweet/media expansions 轉成 normalized media items

`platforms/pixiv/` 目前包含：

- `auth.py`：local OAuth/PKCE setup、explicit refresh-token auth、token refresh、credential file support、auth session model
- `client.py`：Pixiv App API user detail、bookmarked illustrations、artwork detail、ugoira metadata calls
- `links.py`：explicit artwork URL/id normalization，以及給共享 link-first resolver pipeline 使用的 Pixiv artwork-detail resolution
- `parser.py`：把 Pixiv works 轉成 normalized media items，包含多頁作品與 ugoira metadata

`platforms/telegram/` 目前包含：

- `auth.py`：Telethon-compatible user-session configuration、session-path safety、safe auth-session modeling
- `client.py`：Telegram client boundary，包含 fake-client hooks 與 lazy Telethon usage
- `parser.py`：把 Telegram message/media shapes 轉成 normalized media items，包含 grouped media/albums

`platforms/reddit/` 目前包含：

- `auth.py`：Reddit OAuth config、token exchange/refresh/status、credential file helpers 與 Reddit rate-limit metadata parsing
- `client.py`：Reddit OAuth API `/api/v1/me` 與 authenticated-user saved listings calls
- `parser.py`：把 saved listing entries 轉成 first-version image/gallery/video/direct-media shapes 的 normalized media items

`platforms/instagram/` 目前包含：

- `auth.py`：saved-session status、明確本機 username/password login、bounded ensure-session behavior、credential path safety，以及可供 agent 判斷的 auth/session error mapping
- `links.py`：Instagram post/Reel/tv URL parsing、canonical identity、whole-post resource normalization，以及 runtime-only signed CDN download URL handling
- `client.py` 與 `parser.py`：單頁 saved-feed access、opaque cursor，以及 whole-post saved-media normalization

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

## 目前 Link-First Flow

```text
explicit URL source
-> link.queue.upsert
-> URL safety and canonicalization
-> resolver chain
-> media candidates
-> deterministic candidate selection
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

這是目前主要產品方向。URL source 可以是 CLI JSON、queued DB rows、Telegram inbox links、未來 workflow steps 或未來 Agent/SKILL calls。

`link_queue.normalized_url` 只作為第一層 intake dedupe。Resolver 還必須在可行時輸出 canonical aliases 與 source/media identity，避免 short links、canonical post links、old site links、provider watch URLs、direct media URLs 指向同一內容時產生重複下載。

`link_queue` 已具備 schema v7 lifecycle foundation，可作為 cron 或 daemon 使用前的基底。它是 URL resolution queue，不是檔案下載 lifecycle：

```text
queued
-> resolving
-> resolved
```

Permanent skips 與 retryable failures 必須分開：

```text
skipped
failed
deferred
```

Schema 目前已保存 retry counts、last error、retryable flag、next attempt time、source provenance merge fields，tool layer 也支援 batch limit，並有 lease columns。`link.media.sync` 會在 queued runs 使用 active claim/lease behavior，並把 retryable failures 以 bounded `next_attempt_at` backoff 排程。Explicit URL 與 explicit `link_id` runs 則刻意不走 queue claiming。

成功的 `link.media.sync` run 可以在同一個 tool call 內完成 resolve 與 download，但 link row 在 URL resolution 完成後會停在 `resolved`。下載進度與最後檔案狀態以 `media_items` 與 `media_files` 為準，包含 `downloaded`、`partial` 與 `failed` 等狀態。

`MediaCandidate` 不得持久化帶有 credentials 的 request headers。可保存的 download hints 必須是 allowlisted 且 non-secret，例如必要時使用的 public `Referer`。`Authorization`、`Cookie`、signed URL tokens、session headers、CSRF headers 等 runtime-only headers 必須透過 download context reference 保存在記憶體中，不得寫入 SQLite、sidecar metadata、logs 或 snapshots。

Multi-candidate resolution 目前已支援簡單 static file groups，例如 Reddit galleries。現行 contract 會針對這些 static groups 記錄 group id、required files、optional files、partial-success status 與 `metadata.files` mapping。Muxed video/audio tracks 與更複雜的 multi-file posts 仍維持 deferred。

Instagram 使用同一套 link-first contract，但多了一層 platform session boundary。一個 Instagram `/p/<shortcode>/`、`/reel/<shortcode>/` 或 `/tv/<shortcode>/` URL 代表整個 post。Carousel resources 會被 normalize 成同一個 media item 底下的多個 files；signed Instagram CDN URLs 只保留在 runtime，不會作為 canonical media identity 持久化。

## 既有 Collector Flow

```text
platform collector output
-> media.item.upsert
-> status filtering
-> storage.path.plan
-> download.http or platform-specific downloader
-> metadata.write
-> media.file.upsert
-> media.item.set_status
-> core.run.record
```

`pixiv.bookmarks.collect`、`pixiv.bookmarks.sync`、`telegram.messages.collect` 與 `telegram.messages.sync` 仍是有用且已實作的流程。X bookmark collection 與 Reddit saved collection 已有 fixture/fake-client coverage，但除非使用者明確恢復 auth-assisted account collection，否則它們不是目前擴展路徑。

## Future Policy Layer

RuleSpec 是預留的 policy layer，不是已實作 runtime feature。

未來預期形狀：

```text
explicit URL source or collector
-> candidate media items
-> deterministic RuleSpec policy
-> sync/download pipeline
```

LLM 或 Agent Core integration 可以協助使用者把自然語言意圖轉成 explicit tool calls 或未來 RuleSpec files，但 daemon 與 cron execution 應執行已儲存的 deterministic rules。Platform adapters 不應包含 quality scoring 或 content-preference logic。

## Deferred Workflow Layer

`src/mediagent/workflows/` 目前是 placeholder。除非使用者明確選擇，或下一個平台 foundation 證明 shared sync contract 已足夠穩定，否則不要實作 Workflow V1。
