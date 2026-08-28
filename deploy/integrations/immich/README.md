# Immich delete-candidate integration

This integration keeps Immich as the selection UI while making Mediagent the
only writer of new trash state. The script reads one configured Immich album,
maps each external-library path to the NAS path, and invokes
`mediagent library remove`. It never calls `shutil.move` or writes directly
under `.trash`.

Install the script over the existing private cleanup script and install
`mediagent.conf` as:

```text
/etc/systemd/system/immich-private-cleanup.service.d/mediagent.conf
```

Install `timer.conf` under the matching timer drop-in directory. Its empty
`OnCalendar=` line correctly resets the base daily schedule before adding the
hourly schedule; this replaces the historical `OnClaendar=` typo.

The base service keeps its existing `EnvironmentFile`. The service account
must be able to read that file and execute the script. Pre-create
`<library-root>/.trash/mediagent` for the service account, then verify with:

```bash
mediagent library trash status --library-root /data/nas/mediagent --json
```

The drop-in uses the same advisory lock as the Mediagent sync services, so a
timer collision exits with status 75 and performs no cleanup. Removed files
remain indefinitely until explicitly restored or a separately reviewed
retention policy is added.
