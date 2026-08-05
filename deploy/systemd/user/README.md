# Mediagent user systemd timer examples

These units are local deployment examples for the current repository checkout.

They intentionally call deterministic tools instead of Agent Core. Timers should be predictable, bounded, restartable, and easy to inspect. Agent Core remains useful for manual natural-language tasks and future higher-level orchestration.

## Prerequisites

1. Keep a deployment `.env` file at:

   ```text
   /home/ion/projects/mediagent/.env
   ```

2. Ensure the `.env` file defines persistent paths under the intended user home:

   ```text
   MEDIAGENT_DATA_DIR=/home/ion/projects/mediagent/mediagent-data
   MEDIAGENT_DB_PATH=/home/ion/projects/mediagent/mediagent-data/mediagent.sqlite3
   MEDIAGENT_LIBRARY_DIR=/home/ion/projects/mediagent/mediagent-data/library
   ```

3. Ensure Telegram and Pixiv credentials are already usable from the CLI.

4. Run the tools manually once before enabling timers.

## Install user units

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/mediagent-*.service ~/.config/systemd/user/
cp deploy/systemd/user/mediagent-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

## Manual verification

```bash
systemctl --user start mediagent-telegram-inbox.service
systemctl --user status mediagent-telegram-inbox.service
journalctl --user -u mediagent-telegram-inbox.service -n 80 --no-pager

systemctl --user start mediagent-pixiv-bookmarks.service
systemctl --user status mediagent-pixiv-bookmarks.service
journalctl --user -u mediagent-pixiv-bookmarks.service -n 80 --no-pager
```

## Enable timers

```bash
systemctl --user enable --now mediagent-telegram-inbox.timer
systemctl --user enable --now mediagent-pixiv-bookmarks.timer
systemctl --user list-timers 'mediagent-*'
```

## Stop timers

```bash
systemctl --user disable --now mediagent-telegram-inbox.timer
systemctl --user disable --now mediagent-pixiv-bookmarks.timer
```

## State model

Treat the SQLite DB and library directory as one persistent state bundle.

Changing `MEDIAGENT_LIBRARY_DIR` only affects future target planning. Existing terminal media items in the same DB remain deduped and will not be repopulated into the new directory unless an explicit repair or rebuild flow is used.
