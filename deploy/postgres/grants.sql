GRANT USAGE ON SCHEMA sensor TO sensor_api, grafana_reader;
GRANT SELECT, INSERT ON sensor.events, sensor.measurements TO sensor_api;
GRANT USAGE, SELECT ON SEQUENCE sensor.events_sequence_seq TO sensor_api;
GRANT SELECT ON sensor.dashboard_values TO grafana_reader;

-- Runs for fresh installs and upgrades: initdb scripts do not run on old volumes.
\getenv telegraf_password DB_TELEGRAF_PASSWORD
SELECT format('CREATE ROLE telegraf_writer LOGIN PASSWORD %L', :'telegraf_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='telegraf_writer')
\gexec
GRANT USAGE ON SCHEMA sensor TO telegraf_writer;
GRANT INSERT ON sensor.mqtt_ingest TO telegraf_writer;
