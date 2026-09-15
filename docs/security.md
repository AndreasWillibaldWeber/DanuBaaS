# Security model

DanuBaaS uses independent credentials for machine ingestion, alert administration,
dashboard viewing, the Node-RED editor, MQTT, and database roles. Caddy provides
HTTPS and an outer Basic Auth gate for browser dashboards; Grafana and the
Node-RED Dashboard then require their own application sign-in. The direct API
uses `X-API-Key`, and MQTT uses TLS credentials and topic ACLs.

| Surface | Control |
| --- | --- |
| Direct Go API | API key for observations and alert reads; separate admin key for alert writes |
| Node-RED demonstration REST route | API key for observations; no alert admin route |
| Grafana and Node-RED Dashboard | Caddy gate plus separate application login |
| Node-RED editor | Own admin login; development binds port 1880 to loopback |
| MQTT broker | TLS listener, per-publisher credentials and topic ACLs |
| PostgreSQL | Separate migration, API, Grafana, and Telegraf roles; restricted grants |

Use HTTPS for remote HTTP clients and verify the MQTT server certificate. Never
send API keys in URLs. Keep secret files and backups outside commits. Production
requires externally supplied secret and MQTT certificate directories; the
[production guide](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/deploy/PRODUCTION.md)
describes their permissions and validation.

The API key is shared across this demonstration dataset: it is not scoped by
device. The alert-admin key is logged as a shared `alert-admin` principal, not an
individual operator identity. The project does not provide an identity-provider
integration, per-sensor permissions, or high availability. Treat those as separate
deployment requirements when relevant.

The [Debian host-security baseline](https://github.com/AndreasWillibaldWeber/DanuBaaS/blob/main/security/debian/README.md)
contains nftables, SSH, fail2ban, update, audit, and timed firewall rollback
guidance. `make test-security` validates the scripts; it does not apply a host
policy. Operators apply and verify host changes explicitly.
