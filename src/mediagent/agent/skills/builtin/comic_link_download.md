---
name: comic_link_download
description: Download explicit nhentai or JMComic/18comic comic links exactly as linked.
allowed_tools:
  - comic.link.sync
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - download an explicit nhentai gallery URL
  - download an explicit JMComic or 18comic album URL
  - download an explicit JMComic or 18comic photo URL
  - repair a directly linked comic
unsupported_intents:
  - synchronize account favorites
  - search either provider
  - follow a directly linked series
---

## When To Use

Use this skill when the user provides one or more explicit nhentai or JMComic/18comic URLs. Call `comic.link.sync` and do not ask the user to choose a strategy.

Direct links are always exact:

- nhentai gallery: that one gallery
- JMComic album or trusted cover: every chapter currently in that album, with no follow state
- JMComic photo: that one chapter only

The tool retries recorded failures, repairs missing pages, and creates a CBZ only when every declared page is healthy. A later direct sync of the same album may discover its then-current chapters, but the direct link itself is never stored as a recurring follow source. Favorites belong to `comic_favorites_sync`.

Report resolved chapters, downloaded/partial/failed pages, and packaged/existing/incomplete CBZ files.
