---
name: comic_favorites_sync
description: Sync comics from the configured nhentai or JMComic favorites collection.
allowed_tools:
  - nhentai.auth.status
  - nhentai.auth.refresh
  - nhentai.favorites.sync
  - jmcomic.auth.status
  - jmcomic.auth.login
  - jmcomic.favorites.sync
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - sync nhentai favorites
  - sync JMComic or 18comic favorites
  - retry failed or repair missing favorite comic files
unsupported_intents:
  - download a directly provided comic URL
  - search either provider
  - manage or delete favorites
---

## When To Use

Use this skill only when the user asks to synchronize the configured account favorites. The user does not choose a mode:

- `nhentai.favorites.sync` treats each favorite gallery as exact.
- `jmcomic.favorites.sync` treats each favorite album as `series_and_follow`, downloading its current chapters and discovering new chapters on later runs while it remains favorited.

Direct links belong to `explicit_link_download` and never enable follow behavior.

Do not pass `download_limit` unless the user explicitly requests a bounded download. Collection enumeration must still complete before favorite removals are committed. Both sync tools retry failed pages, repair missing files, and package complete chapters as CBZ by default.

If nhentai reports a missing session, explain that a browser cookie session must be imported once; username/password automation is intentionally unsupported because the site requires interactive browser challenges. If JMComic has configured credentials but no usable session, `jmcomic.favorites.sync` may log in once and persist the session.

Report favorites seen, added/retained/removed membership, followed albums, downloaded chapters/pages, and CBZ package counts.
