#!/bin/sh
set -eu
DB_GRAFANA_PASSWORD=$(cat /run/secrets/db_grafana_password)
export DB_GRAFANA_PASSWORD
exec /run.sh
