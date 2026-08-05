---
name: instagram_link_download
description: Resolve and download explicit Instagram post or Reel links.
allowed_tools:
  - instagram.auth.status
  - instagram.auth.ensure_session
  - link.media.sync
default_dry_run: false
risk_level: write_files_network_credentials
requires_initial_tool_call: false
supports_unbounded: true
supported_intents:
  - resolve explicit Instagram post URLs
  - download explicit Instagram post URLs
  - resolve explicit Instagram Reel URLs
  - download explicit Instagram Reel URLs
unsupported_intents:
  - Instagram feed crawling
  - hashtag crawling
  - account scraping
  - sending messages or account actions
---

## When To Use

Use this skill when the user provides explicit Instagram post, carousel, or Reel URLs and asks to resolve or download them.

Do not use this skill for Instagram feed crawling, hashtag crawling, account scraping, sending messages, liking, commenting, or following.

## Inputs The User May Provide

The user may provide one or more Instagram URLs, sidecar metadata preference, retry behavior, or execute intent.

## Tool Calling Strategy

Use `link.media.sync` for the normal explicit-link workflow because the link resolver can route Instagram URLs to the Instagram foundation.

For carousel posts, one post URL represents the whole post. Download every media resource in that post unless the user explicitly asks for a narrower item.

Use `instagram.auth.ensure_session` only when a tool result reports `instagram_login_required`, `instagram_session_expired`, missing credentials, or invalid session state.

Use `instagram.auth.status` only when the user asks to check Instagram session status.

## Example Dry-Run Action

```json
{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://www.instagram.com/reel/example/","write_sidecar_metadata":false,"retry_failed":false},"dry_run":true,"reason":"Preview the Instagram explicit-link download plan."}
```

## Common Errors

- `instagram_login_required`: call `instagram.auth.ensure_session` or ask the user to configure credentials.
- `requires_auth`: explain that the post cannot be resolved without a valid session.
- `unsupported_media_type`: explain that no supported media was found.
- `rate_limited`: suggest waiting before retrying.

## Final Summary

Report whether the post or Reel resolved, how many files were planned or downloaded, skipped reasons, and artifact paths when available.
