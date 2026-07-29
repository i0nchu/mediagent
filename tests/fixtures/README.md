# Test Fixtures

Default tests must not call real platforms or require real credentials.

Use fixtures for:

- fake HTTP clients
- recorded platform API responses with secrets removed
- platform-agnostic collector output examples

Recorded responses must be minimized to fields needed by parsers and tests. Never commit real tokens, cookies, private bookmarks, user IDs that identify a real person, or downloaded media.
