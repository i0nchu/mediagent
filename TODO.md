# Mediagent TODO

## Features

- [x] Phase 2 Stage 0: consistent operational logging and CLI result semantics
- [x] Phase 2 Stage 1: stable Asset identity and migration foundation
- [x] Phase 2 Stage 2: universal add for URLs and local media
- [x] Phase 2 Stage 3: Asset removal, restore, retention purge, and permanent content tombstones
- [x] Phase 2 Stage 4: Asset tags and library search
- [x] Phase 2 Stage 5: unified add orchestration and durable metadata-only AI tagging
- [x] OpenAI-compatible Agent Core LLM provider
- [x] Instagram multi-session fallback for authenticated exact links
- [x] Unified Telegram inbox intake for links, direct uploads, and forwarded media
- [x] Pornhub exact-video support through a dedicated yt-dlp-backed adapter

## Bugs

- [x] Telegram Inbox durable external-link delivery, regional Pornhub routing, bounded retries, and intake cursor/exit-code isolation (#3).
- [ ] JMComic Favorites incrementally resolve only new, failed, repairable or audited chapters with durable progress (#4).
- [x] Consistent `.env` loading across CLI subcommands, including `library`, `tools`, `link`, and `experimental`, preserving `MEDIAGENT_ENV_FILE` and environment precedence.
- [ ] Close SQLite connections explicitly after transactions; running the offline suite on Python 3.13 emits `ResourceWarning: unclosed database` from connections returned by `core.db.connect`.
