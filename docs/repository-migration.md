# Integration into DanuBaaS

The former `demo-api/` project now lives directly at the root of
`github.com/AndreasWillibaldWeber/DanuBaaS`. The existing DanuBaaS Git history and
origin remote are retained. No nested Go repository or extra `demo-api/` directory
is required.

## Working from the checkout

```sh
cd DanuBaaS
make dev
```

The Go module and internal imports use the GitHub repository path. The executable
and its command directory remain `sensor-api` and `cmd/sensor-api`. API URLs,
database schema, migration versions, secret names, and MQTT topics are unchanged
by this repository integration.

The development Compose project remains `dbe-sensors`; production remains
`dbe-sensors-production`. These names are retained intentionally because they
determine the existing named volumes. Renaming them would select different volumes
and make an existing installation appear empty.

## Existing local installations

Local `deploy/.env`, secret files, certificates, backups, and installed Node-RED
dependencies move with the deployment directory and remain ignored by Git. They
are not included in a fresh clone; `make dev` initializes a fresh development
environment. Production continues to require operator-provided configuration.

Compose bind-mount source paths change when the checkout moves. Recreate existing
development containers from the new root to use those paths:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d --force-recreate
```

For production, first run `make prod-check`, then select the existing production
project and configuration explicitly:

```sh
docker compose -p dbe-sensors-production --env-file deploy/.env.production \
  -f deploy/compose.yaml up --build -d --force-recreate
```

Do not delete volumes as part of this move. Saved Node-RED flows remain in their
volume; image rebuilds do not overwrite edits made through the editor. The
[deployment guide](../deploy/README.md#upgrade-an-existing-deployment) explains
upgrading older flow definitions.

The workshop report and original images remain outside this repository. The
[alignment review](report-alignment.md) refers to the separate workshop material
that was present during development.
