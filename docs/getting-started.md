# Quick start

This path starts the full development stack and performs a write/read smoke check.
Run commands from the DanuBaaS repository root, where `go.mod` and `Makefile` live.

## Prerequisites

- Docker Engine with Compose v2.20+ and permission to access the daemon
- Node.js 22.9+ with npm
- Python 3.9+, OpenSSL, and Make
- curl for `make dev-test-data` and end-to-end verification

Go is not needed to run the Compose demonstration. Standalone Go development needs
Go 1.25 or newer and a disposable TimescaleDB database.

## Start and sign in

```sh
git clone https://github.com/AndreasWillibaldWeber/DanuBaaS.git
cd DanuBaaS
make dev
```

The command installs pinned Node-RED dependencies, creates local credentials and
certificates when needed, starts Compose services, configures demonstration alert
rules, and verifies an observation through Node-RED and the Go read API. A rerun
preserves credentials and database volumes.

Trust the generated **public** CA certificate at `deploy/certs/caddy-root.crt` in
your browser or operating system. Local passwords are stored in
`deploy/secrets/operator-credentials.json`; read them on your own machine.

| URL | Sign in |
| --- | --- |
| `https://grafana.localhost` | Caddy `viewer`, then Grafana `admin` |
| `https://nodered.localhost/dashboard` | Caddy `viewer`, then dashboard `operator` |
| `http://127.0.0.1:1880/admin` | Node-RED editor `admin` |

The direct API is `https://api.localhost/api/v1/values`. The Node-RED demonstration
route is `https://flows.localhost/api/v1/values`. Both require `X-API-Key`; local
development stores it in `deploy/secrets/api_key`. Use
[the observation example](api.md#send-one-observation) to exercise the direct API.

## Populate the dashboards

```sh
make dev-test-data
make test-e2e
```

`make dev-test-data` loads synthetic history through both HTTP routes and verifies
four scheduled alert conditions. It replaces only reserved development fixtures;
review the [fixture IDs and reset policy](test-data.md) before using them for your
own data. `make test-e2e` writes additional synthetic observations, checks reads
and pagination, and leaves those records for inspection.

## Select the Node-RED write route

The default `INGESTION_BACKEND=api` gives Node-RED writes the Go API's committed
response. Set `INGESTION_BACKEND=mqtt` in `deploy/.env`, then rerun `make dev`.
In MQTT mode, Node-RED returns `202` when the broker accepts a message; the
observation is visible only after Telegraf and TimescaleDB commit it. The direct
Go API remains available in either mode. See [write route semantics](architecture.md#write-route-semantics).

To stop services while retaining data:

```sh
docker compose -f deploy/compose.yaml down
```

For a host deployment, follow [production preflight and secrets](operations.md#production-preflight)
before `make prod`. Existing installations should first read the
[repository migration notes](repository-migration.md).
