# Quick start

Install Docker Engine with Compose v2.20+, Node.js 22.9+ with npm, Python 3.9+,
OpenSSL, and Make. Docker must be running and accessible to your user.

From `DanuBaaS/` (the directory containing `go.mod`), run:

```sh
make dev
```

This installs dependencies, generates local credentials and certificates, builds
and starts all services, and verifies a sample Node-RED write and database read.
It also loads the development alert parameters and verifies the PostgreSQL evaluator.
Rerunning preserves credentials, saved flows, and database volumes.

Trust `deploy/certs/caddy-root.crt` in your browser/OS. Read your passwords locally
from `deploy/secrets/operator-credentials.json`.

| Open | Login names |
| --- | --- |
| https://grafana.localhost | `viewer`, then `admin` |
| https://nodered.localhost/dashboard | `viewer`, then `operator` |
| http://127.0.0.1:1880/admin | `admin` |

The API-key-protected REST endpoint is `https://flows.localhost/api/v1/values`.
To use Telegraf, set `INGESTION_BACKEND=mqtt` in `deploy/.env` and rerun `make dev`.
Stop services with `docker compose -f deploy/compose.yaml down`; data is retained.

**Production:** copy `deploy/.env.production.example` to `deploy/.env.production`
and [supply the required configuration, secrets, and MQTT certificates](deploy/PRODUCTION.md).
Run `make prod-check` to validate, then `make prod` to start. Neither command
generates credentials; missing or invalid configuration prevents startup.

## Alert parameters

`make dev` creates `deploy/alerts.json` with demonstration warning/critical water
levels and rise rates for `quickstart`. Later runs preserve API edits. View active
alerts in Grafana and configure rules through `https://api.localhost/api/v1/alert-rules`
using the separate key in `deploy/secrets/alert_admin_key`. The ingestion key cannot
change thresholds. See [alerting.md](docs/alerting.md) for parameters, API operations,
health checks and optional webhook delivery. Demo thresholds require site calibration
before operational use.
