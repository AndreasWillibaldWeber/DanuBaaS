CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS sensor;

-- The ordinary table enforces UUID uniqueness globally. The hypertable's primary
-- key must include event time. Registry entries deliberately outlive retention.
CREATE TABLE sensor.events (
    id uuid PRIMARY KEY,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE NOT NULL,
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload jsonb NOT NULL
);
CREATE TABLE sensor.measurements (
    observed_at timestamptz NOT NULL,
    id uuid NOT NULL REFERENCES sensor.events(id),
    sensor_id text NOT NULL,
    gateway_id text,
    sensor_type text NOT NULL,
    value double precision NOT NULL,
    unit text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (observed_at, id)
);
SELECT create_hypertable('sensor.measurements', 'observed_at',
                         chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON sensor.measurements (sensor_id, observed_at DESC);

CREATE VIEW sensor.dashboard_values AS
SELECT observed_at AS time, sensor_id, gateway_id, sensor_type, value, unit, metadata
FROM sensor.measurements;
