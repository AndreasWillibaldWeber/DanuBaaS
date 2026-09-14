# Production configuration

Use `make prod` from `DanuBaaS/`. It validates all configuration before contacting
Docker, then builds and starts the `dbe-sensors-production` Compose project.
`make prod-check` performs only the read-only preflight and does not require a
Docker daemon. Both require Python 3.9+ and OpenSSL; startup additionally requires
Docker Engine and Compose v2.20+. Production never invokes the development setup
or generates default credentials, certificates, or sample observations.

## Operator-supplied configuration

Copy `.env.production.example` to `.env.production` in this directory and fill in
every field. Values are literal `KEY=value` lines without quotes, comments after
values, or variable interpolation. Shell environment variables cannot override
these validated settings. The file contains paths, hostnames, and usernames;
credentials belong exclusively in secret files.

- Set four distinct public DNS names for the HTTP services and `MQTT_HOST` to the
  hostname covered by the MQTT certificate. Point them to the host before startup.
- Set all four login usernames and `INGESTION_BACKEND` to `api` or `mqtt`.
- Set `SECRETS_DIR` and `MQTT_CERTS_DIR` to absolute directories outside the repository.
  Do not reuse the development credential directory or its generated credentials.

Caddy obtains public HTTPS certificates for the configured web hostnames. Arrange
DNS and access to ports 80/443 for certificate issuance. MQTT uses port 8883 and
the certificate supplied below. The editor remains bound to loopback on port 1880.
Development and production use separate project names and volumes, but publish
the same ports; stop development before running production on the same host.

Set `ALERT_EVALUATION_SECONDS` (1–3600) and `ALERT_RULES_FILE` to an absolute
readable JSON file outside the repository. Start with
`alerts.production.example.json` (an empty array), or supply calibrated rules in
the format of `alerts.dev.example.json`. Startup loads missing rules only;
existing API-managed rules are preserved. See [alerting](../docs/alerting.md).

## Secret files

Provision these files in `SECRETS_DIR` through your normal secret-management process:

| File | Required content |
| --- | --- |
| `db_admin_password` | Independent random secret, at least 32 bytes |
| `db_api_password` | Independent random secret, at least 32 bytes |
| `db_grafana_password` | Independent random secret, at least 32 bytes |
| `db_telegraf_password` | Independent random secret, at least 32 bytes |
| `api_key` | Independent random secret, at least 32 ASCII bytes |
| `alert_admin_key` | Separate random secret, at least 32 ASCII bytes; administrative alert operations |
| `alert_webhook_url` | HTTPS receiver URL, or an empty file to disable external delivery |
| `mqtt_node_red_password` | Independent random secret, at least 32 bytes |
| `mqtt_demo_password` | Independent random secret, at least 32 bytes; the supplied gateway account remains configured |
| `mqtt_telegraf_password` | Independent random secret, at least 32 bytes |
| `node_red_credential_secret` | Independent random secret, at least 32 bytes |
| `grafana_admin_password` | Independent random secret, at least 32 bytes |
| `node_red_admin_password_hash` | bcrypt hash, cost 12 or higher |
| `dashboard_password_hash` | bcrypt hash, cost 12 or higher |
| `caddy_password_hash` | bcrypt hash, cost 12 or higher |

Files contain one UTF-8 value, optionally followed by one newline. Empty, short,
obvious placeholder, and reused credential values are rejected. Only the optional
`alert_webhook_url` file may be empty. Hash syntax and cost are
checked; the checker cannot assess the strength of the underlying password.
Store those passwords in your password manager. No password values are provided
in the production template or printed by the checker.

Use mode `0700` for the secrets directory. Local Compose secrets are file bind
mounts shared with different container UIDs; use `0444` for individual secret
files inside that private directory. The checker rejects files unreadable by
the service UIDs. These files are not an encrypted secret store.

## MQTT certificates

`MQTT_CERTS_DIR` must contain `server.crt` (PEM certificate, followed by any needed
intermediate certificates) and `server.key` (matching, unencrypted PEM private key).
Use mode `0755` for this mounted directory and `0444` for both files, inside a
private parent directory. Mosquitto runs as UID 1883 and must be able to read them.

Preflight verifies parsing, hostname coverage, current validity, at least 24 hours
remaining validity, and key matching. It does not establish external CA trust or
test DNS reachability. Distribute the appropriate CA to MQTT clients and manage
certificate renewal separately. Do not use the development CA in production.

## Start and operate

From `DanuBaaS/`:

```sh
make prod-check
make prod
```

The production wrapper is the supported entry point for these checks. Running
Compose directly bypasses the Python preflight. For subsequent inspection, always
select the same production project and environment file:

```sh
docker compose -p dbe-sensors-production --env-file deploy/.env.production \
  -f deploy/compose.yaml ps
```

Startup waits for Compose's running/healthy conditions; it is not a release
acceptance test. Follow [quality.md](../docs/quality.md) for deployment verification
and the [operations guide](README.md#persistence-and-operations) for backups and
rotation. Updating password files alone does not rotate existing database roles
or Grafana's initial administrator password. Never delete volumes to rotate secrets.

## Debian host security

Use the [Debian host-security baseline](../security/debian/README.md) for nftables
(including Docker forwarding), SSH, fail2ban, security updates, and an operations
checklist. Host policies are applied explicitly on the server and are never run
by `make dev` or `make prod`. Firewall changes support a timed trial and rollback.
