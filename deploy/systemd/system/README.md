# Mediagent system-level comic favorite timers

These units run deterministic favorite sync tools from the production checkout at
`/data/services/mediagent` as the production account `server`. They set `HOME`
and `PATH` explicitly so a system service can locate the account's user-installed
`uv`. They do not turn direct links into follow targets.

- `nhentai.favorites.sync` treats every currently favorited gallery as exact and
  discovers newly favorited galleries on later runs.
- `jmcomic.favorites.sync` treats the union of the selected favorite folders as
  `series_and_follow` and discovers new chapters while an album remains in at
  least one selected folder. With no folder configuration it keeps the backward-
  compatible aggregate `all` view (remote folder ID `0`).

Both services use the same non-blocking `/run/lock/mediagent-sync.lock`. This
prevents comic timers from writing the shared SQLite database concurrently. A
lock conflict exits successfully and the persistent timer tries again at its next
scheduled run. SQLite connections also wait up to 30 seconds for unrelated short
transactions.

Before installing either timer, run its `*.favorites.collect` diagnostic and one
manual full sync from the production checkout. The collect command validates the
complete remote snapshot without downloading or changing collection membership:

```bash
sudo -u server bash -lc 'cd /data/services/mediagent && set -a && source .env && set +a && uv run --locked mediagent tools run jmcomic.favorites.collect --summary-json'
sudo -u server bash -lc 'cd /data/services/mediagent && set -a && source .env && set +a && uv run --locked mediagent tools run nhentai.favorites.collect --summary-json'
```

JMComic resolves current folder names from the authenticated API and also accepts
numeric folder IDs or trusted folder URLs. Inspect remote names, or register a
stable local fallback when the provider returns a stale/empty folder index (this
does not modify the remote account):

```bash
sudo -u server bash -lc 'cd /data/services/mediagent && set -a && source .env && set +a && uv run --locked mediagent tools run jmcomic.favorites.folders.register --input examples/tools/jmcomic.favorites.folders.register.json --json'
sudo -u server bash -lc 'cd /data/services/mediagent && set -a && source .env && set +a && uv run --locked mediagent tools run jmcomic.favorites.folders.list --input examples/tools/jmcomic.favorites.folders.list.json --json'
sudo -u server bash -lc 'cd /data/services/mediagent && set -a && source .env && set +a && uv run --locked mediagent tools run jmcomic.favorites.folders.collect --input examples/tools/jmcomic.favorites.folders.collect.json --json'
```

The system service reads `MEDIAGENT_JMCOMIC_FAVORITE_FOLDERS` when its input JSON
does not contain `folders`. Use a JSON array so names may contain punctuation:

```dotenv
MEDIAGENT_JMCOMIC_FAVORITE_FOLDERS='["read-later","long-series"]'
```

For the first deployment, numeric IDs may be selected before aliases are
registered. Tool input always overrides the environment value. Every selected
folder must complete before the union snapshot is committed; one incomplete
folder preserves the previous follow membership. Changing the selected set on a
later successful run stops follow only for albums absent from the new union and
never deletes already downloaded files.

Install and verify the system units:

```bash
sudo cp /data/services/mediagent/deploy/systemd/system/mediagent-*-favorites.service /etc/systemd/system/
sudo cp /data/services/mediagent/deploy/systemd/system/mediagent-*-favorites.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl start mediagent-jmcomic-favorites.service
sudo systemctl status mediagent-jmcomic-favorites.service --no-pager
sudo journalctl -u mediagent-jmcomic-favorites.service -n 80 --no-pager

sudo systemctl enable --now mediagent-jmcomic-favorites.timer
sudo systemctl list-timers 'mediagent-*-favorites.timer'
```

Enable the nhentai timer only while its imported browser cookie remains valid:

```bash
sudo systemctl start mediagent-nhentai-favorites.service
sudo systemctl enable --now mediagent-nhentai-favorites.timer
```

Do not delete SQLite `-wal` or `-shm` files. Stop all relevant services before a
manual repair or overwrite run.
