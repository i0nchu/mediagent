---
name: comic_favorites_sync
description: Sync comics from the configured nhentai or JMComic favorites collection.
allowed_tools:
  - nhentai.auth.status
  - nhentai.auth.refresh
  - nhentai.favorites.collect
  - nhentai.favorites.sync
  - jmcomic.auth.status
  - jmcomic.auth.login
  - jmcomic.favorites.folders.register
  - jmcomic.favorites.folders.list
  - jmcomic.favorites.folders.collect
  - jmcomic.favorites.collect
  - jmcomic.favorites.sync
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - sync nhentai favorites
  - sync JMComic or 18comic favorites
  - retry failed or repair missing favorite comic files
  - validate or count configured comic favorites without downloading
  - register, list, or select JMComic favorite folders by name
unsupported_intents:
  - download a directly provided comic URL
  - search either provider
  - manage or delete favorites
---

## When To Use

Use this skill only when the user asks to synchronize the configured account favorites. The user does not choose a mode:

- `nhentai.favorites.sync` treats each favorite gallery as exact.
- `jmcomic.favorites.sync` treats each favorite album as `series_and_follow`, downloading its current chapters and discovering new chapters on later runs while it remains favorited.

JMComic folder selection is an inbox boundary. Pass `folders` only when the user names one or more folders. Registered names, numeric folder IDs, and trusted `favorite/albums?folder=...` URLs are accepted; unknown names must fail rather than silently falling back to `default`. Multiple folders are collected completely and unioned by album ID before one atomic membership snapshot is committed. If any selected folder is incomplete, preserve the previous snapshot. Removing a selected folder stops follow only for albums no longer present in any retained selected folder, without deleting downloaded files.

Resolve unregistered names from the authenticated remote `folder_list`; use `jmcomic.favorites.folders.collect` when the user only wants to inspect that remote index. Use `jmcomic.favorites.folders.register` when the user provides a folder name plus its numeric ID or trusted URL, or when a stale provider session does not return its folder index. Registration is local and does not modify the remote account. Use `jmcomic.favorites.folders.list` to inspect the local fallback registry. The reserved `all` name maps to folder ID `0`, which is the provider's aggregate All view; `default`, `全部`, and `所有` are accepted aliases.

Direct links belong to `explicit_link_download` and never enable follow behavior.

Use `*.favorites.collect` when the user asks only to validate authentication,
pagination, or favorite counts without downloading. It never changes collection
membership. Normal synchronization and follow runs use `*.favorites.sync`.

Do not pass `download_limit` unless the user explicitly requests a bounded download. Collection enumeration must still complete before favorite removals are committed. Both sync tools retry failed pages, repair missing files, and package complete chapters as CBZ by default.

If nhentai reports a missing session, explain that a browser cookie session must be imported once; username/password automation is intentionally unsupported because the site requires interactive browser challenges. If JMComic has configured credentials but no usable session, `jmcomic.favorites.sync` may log in once and persist the session.

Report favorites seen, added/retained/removed membership, followed albums, downloaded chapters/pages, and CBZ package counts.
