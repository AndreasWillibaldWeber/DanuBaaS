#!/bin/sh
set -eu
# psql's quoted variables escape passwords as SQL literals, including punctuation.
DB_API_PASSWORD=$(cat /run/secrets/db_api_password)
DB_GRAFANA_PASSWORD=$(cat /run/secrets/db_grafana_password)
export DB_API_PASSWORD DB_GRAFANA_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv api_password DB_API_PASSWORD
\getenv grafana_password DB_GRAFANA_PASSWORD
CREATE ROLE sensor_api LOGIN PASSWORD :'api_password';
CREATE ROLE grafana_reader LOGIN PASSWORD :'grafana_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
