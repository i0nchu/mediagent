# Mediagent Agent 接手指南

## 漫畫來源更新（2026-08-13）

nhentai 與 JMComic/18comic 已具備只處理漫畫的 adapter 與工具。直接連結一律為 `exact`：nhentai gallery 是單本、JM photo 是單章、JM album／可信封面是當下所有章節且不追蹤。收藏列表才是帳號 inbox：nhentai 收藏仍為單本 exact，指定 JM 收藏資料夾的聯集則為 `series_and_follow`；資料夾名稱可從遠端解析並以本機名稱/FID fallback。完整頁面會存入 `comic-pages`，並原子封裝成含 `ComicInfo.xml`、適合 Kavita 的 CBZ；影片、音訊與文字內容不會進入漫畫候選。

JMComic album manifest 是章號權威來源，provider 重號使用 deterministic collision suffix。`jmcomic.library.reconcile` 提供全 library 唯讀 plan 與明確確認 apply，可用健康本機頁修復既有 DB／CBZ identity，不重新下載媒體，也不還原 `.trash`。

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
- Pixiv 作品類型分類、舊 library reconciliation，以及官方漫畫的 deterministic CBZ 封裝
- Telegram user-session media source tools，包含 explicit local login、auth status、dialog listing、message/link-inbox collection、Telegram-specific media download 與 deterministic message sync
- Reddit OAuth config/auth tools 與 saved-media collection tools，但目前保留為 deferred legacy/advanced capability
- first-class link-first tools：`link.queue.upsert` 與 `link.media.sync`，以及 direct media、bounded single-media HTML、Imgur、Pixiv artwork links、anonymous Reddit explicit links/static galleries、Redgifs direct/watch links 的 resolver foundations
- Instagram explicit-link foundation tools：saved-session status/login/ensure-session，以及透過 `link.media.sync` 解析並下載整個 post/Reel
- Agent Core V1 local preview，包含英文 SKILL files、Ollama-backed strict JSON action generation、tool allowlist enforcement、unsupported-task handling、execute/dry-run boundaries 與 destination path sanitization
- unit tests、CLI smoke tests、fake HTTP clients 與 fixture responses

尚未實作：

- Workflow V1
- 內建 scheduler

Agent Core V1 已存在，但定位是 local preview，不是廣泛 autonomous planner 或 scheduler。X auth/bookmark collection 與 Reddit auth/saved collection 已有 fake HTTP / fixture 測試，但它們不再是主要擴展路徑。Pixiv auth、collection、deterministic bookmark sync 與 universal storage layout 已有 fake HTTP / fixture 測試；Pixiv 也已完成使用者協助的 live storage verification，包含一次 100 個 items / 624 個 files 的 bounded `scanner-friendly-v2` layout run。Telegram foundation 已包含 explicit login、curated link-inbox support、stream-safe real downloads、layout placement、重跑去重，以及小型媒體與一小時影片 live verification。Instagram explicit-link foundation 已支援使用者提供的公開 post、carousel 與 Reel links，搭配本機 saved session，並已完成 live verification。

目前產品方向是 link-first：使用者、cron jobs、workflows、Telegram inboxes 與未來 agents 提供 explicit URLs；Mediagent 解析安全且可下載的 media candidates，然後重用既有 storage/download pipeline。除非使用者明確重啟，auth-assisted account collection 應視為 optional legacy/advanced behavior。Pixiv bookmarks 因為已能穩定運作，所以保留為實用例外。

## 常用命令

```bash
uv run --locked mediagent tools list --json
uv run --locked mediagent tools inspect pixiv.auth.login --json
uv run --locked mediagent tools inspect pixiv.bookmarks.collect --json
uv run --locked mediagent tools inspect pixiv.bookmarks.sync --json
uv run --locked mediagent tools inspect pixiv.library.reconcile --json
uv run --locked mediagent tools inspect pixiv.comics.package --json
uv run --locked mediagent tools inspect jmcomic.library.reconcile --json
uv run --locked mediagent tools inspect core.cleanup.media_state --json
uv run --locked mediagent tools inspect library.content.deduplicate --json
uv run --locked mediagent tools inspect library.entry.remove --json
uv run --locked mediagent library deduplicate --dry-run --json
uv run --locked mediagent tools inspect telegram.auth.login --json
uv run --locked mediagent tools inspect telegram.messages.sync --json
uv run --locked mediagent tools inspect link.queue.upsert --json
uv run --locked mediagent tools inspect link.media.sync --json
uv run --locked mediagent tools inspect instagram.auth.status --json
uv run --locked mediagent tools inspect instagram.link.resolve --json
uv run --locked mediagent tools list --json --include-experimental
uv run --locked mediagent tools inspect link.resolve.preview --json --allow-experimental
uv run --locked mediagent agent skills list --json
uv run --locked mediagent agent skills inspect telegram_inbox_download --json
uv run --locked mediagent agent run "download https://example.com/media.jpg" --dry-run --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 重要方向

除非使用者明確改變方向，下一步不要先做 Workflow V1、內建 scheduler、Reddit saved sync 或 X live auth verification。Agent Core V1 可以繼續 harden，但必須維持 SKILL-scoped 且透過 tool registry 呼叫工具。Link-first baseline 現在是主要產品路徑：stable `link.queue.upsert`、stable `link.media.sync`、public `mediagent link sync <url>`、queue claim/retry scheduling、canonical/media identity dedupe、Reddit external-provider delegation、Redgifs downloads、Instagram whole-post downloads，以及簡單 multi-candidate partial-success handling。Resolver behavior 預設應維持 bounded；未來平台工作應先擴充 explicit-link provider adapters，再考慮 account/bookmark collectors。
