-- Optional measured bounds share the observation's unit. Preserve legacy metadata;
-- reject malformed ranges on new writes through either ingestion backend.
CREATE FUNCTION sensor.valid_measurement_range(meta jsonb, measured float8)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE low float8; high float8;
BEGIN
 IF NOT (meta ? 'minimum' OR meta ? 'maximum') THEN RETURN true; END IF;
 IF jsonb_typeof(meta->'minimum') IS DISTINCT FROM 'number'
 OR jsonb_typeof(meta->'maximum') IS DISTINCT FROM 'number' THEN RETURN false; END IF;
 low := (meta->>'minimum')::float8; high := (meta->>'maximum')::float8;
 RETURN low > '-Infinity'::float8 AND high < 'Infinity'::float8
        AND low <= measured AND measured <= high;
EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN RETURN false;
END $$;
ALTER TABLE sensor.measurements ADD CONSTRAINT measurements_range_check
 CHECK (sensor.valid_measurement_range(metadata,value)) NOT VALID;

CREATE OR REPLACE VIEW sensor.dashboard_values AS
SELECT observed_at AS time, sensor_id, gateway_id, sensor_type, value, unit, metadata,
       longitude, latitude, location_id, id AS observation_id,
       CASE WHEN sensor.valid_measurement_range(metadata,value) THEN (metadata->>'minimum')::float8 END AS minimum,
       CASE WHEN sensor.valid_measurement_range(metadata,value) THEN (metadata->>'maximum')::float8 END AS maximum
FROM sensor.measurements;

-- Copy bounds into event evidence while the immutable source observation exists.
-- Event snapshots survive measurement retention and later sensor readings.
CREATE OR REPLACE FUNCTION alerting.record_event(aid bigint, event_name text, data jsonb)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE eid bigint; source jsonb;
BEGIN
 IF data ? 'observation_id' THEN
  SELECT payload INTO source FROM sensor.events WHERE id=(data->>'observation_id')::uuid;
  IF source IS NOT NULL AND sensor.valid_measurement_range(coalesce(source->'metadata','{}'::jsonb),(source->>'value')::float8) THEN
   data := data || jsonb_build_object('minimum',source->'metadata'->'minimum','maximum',source->'metadata'->'maximum');
  END IF;
 END IF;
 INSERT INTO alerting.events(alert_id,event_type,details) VALUES(aid,event_name,data) RETURNING id INTO eid;
 IF event_name <> 'acknowledged' THEN
  INSERT INTO alerting.notification_outbox(event_id) VALUES(eid);
 END IF;
END $$;
