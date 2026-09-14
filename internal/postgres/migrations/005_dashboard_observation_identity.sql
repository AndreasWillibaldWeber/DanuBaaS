-- Append identity without changing existing reporting columns or grants.
-- Dashboard baseline selection can now break timestamp ties deterministically.
CREATE OR REPLACE VIEW sensor.dashboard_values AS
SELECT observed_at AS time, sensor_id, gateway_id, sensor_type, value, unit, metadata,
       longitude, latitude, location_id, id AS observation_id
FROM sensor.measurements;
