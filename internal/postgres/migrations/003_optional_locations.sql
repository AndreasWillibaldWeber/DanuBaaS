-- Add nullable location attributes without rewriting immutable event payloads.
ALTER TABLE sensor.measurements
    ADD COLUMN longitude double precision,
    ADD COLUMN latitude double precision,
    ADD COLUMN location_id bigint,
    ADD CONSTRAINT measurements_coordinates_check CHECK (
        (longitude IS NULL AND latitude IS NULL) OR
        (longitude IS NOT NULL AND latitude IS NOT NULL AND
         longitude BETWEEN -180 AND 180 AND latitude BETWEEN -90 AND 90)),
    ADD CONSTRAINT measurements_location_id_check CHECK (location_id BETWEEN 0 AND 9007199254740991);

-- Append columns to preserve existing view consumers and grants.
CREATE OR REPLACE VIEW sensor.dashboard_values AS
SELECT observed_at AS time, sensor_id, gateway_id, sensor_type, value, unit, metadata,
       longitude, latitude, location_id
FROM sensor.measurements;

CREATE OR REPLACE FUNCTION sensor.ingest_mqtt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    batch jsonb;
    item jsonb;
    canonical jsonb;
    existing jsonb;
    observed timestamptz;
    event_id uuid;
    measurement double precision;
    longitude double precision;
    latitude double precision;
    location_id bigint;
    location_number numeric;
    normalized_time text;
    inserted integer;
    failure_code text;
    failure_message text;
BEGIN
    -- The exception block is a subtransaction: a bad final item or conflict rolls
    -- back every observation from this MQTT document before quarantine is written.
    BEGIN
        IF NEW.value IS NULL OR octet_length(NEW.value) > 1048576 THEN
            RAISE EXCEPTION 'document exceeds 1 MiB or is null' USING ERRCODE = '22023';
        END IF;
        batch := NEW.value::jsonb;
        IF jsonb_typeof(batch) = 'object' THEN batch := jsonb_build_array(batch); END IF;
        IF jsonb_typeof(batch) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'expected an object or array' USING ERRCODE = '22023';
        END IF;
        IF jsonb_array_length(batch) NOT BETWEEN 1 AND 1000 THEN
            RAISE EXCEPTION 'batch size must be 1..1000' USING ERRCODE = '22023';
        END IF;
        IF (SELECT count(DISTINCT v->>'id') FROM jsonb_array_elements(batch) v) <> jsonb_array_length(batch) THEN
            RAISE EXCEPTION 'missing or repeated observation ID' USING ERRCODE = '22023';
        END IF;
        PERFORM pg_advisory_xact_lock(7349202);
        FOR item IN SELECT v FROM jsonb_array_elements(batch) v LOOP
            IF jsonb_typeof(item) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'observation must be an object' USING ERRCODE = '22023';
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_object_keys(item) k WHERE k NOT IN
                ('id','sensor_id','gateway_id','sensor_type','timestamp','value','unit','metadata','lon_lat','location_id')) THEN
                RAISE EXCEPTION 'unknown observation field' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(item->'id') IS DISTINCT FROM 'string' OR
               (item->>'id') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' OR
               jsonb_typeof(item->'sensor_id') IS DISTINCT FROM 'string' OR
               (item->>'sensor_id') !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' OR
               jsonb_typeof(item->'sensor_type') IS DISTINCT FROM 'string' OR
               (item->>'sensor_type') !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$' THEN
                RAISE EXCEPTION 'invalid observation identity' USING ERRCODE = '22023';
            END IF;
            IF item->>'gateway_id' IS NOT NULL AND
               (jsonb_typeof(item->'gateway_id') <> 'string' OR
                (item->>'gateway_id') !~ '^$|^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$') THEN
                RAISE EXCEPTION 'invalid gateway ID' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(item->'timestamp') IS DISTINCT FROM 'string' OR
               (item->>'timestamp') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
                RAISE EXCEPTION 'timestamp must be RFC3339 with at most microsecond precision' USING ERRCODE = '22023';
            END IF;
            observed := (item->>'timestamp')::timestamptz;
            IF extract(year FROM observed AT TIME ZONE 'UTC') NOT BETWEEN 1970 AND 9999 THEN
                RAISE EXCEPTION 'timestamp year out of range' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(item->'value') IS DISTINCT FROM 'number' OR
               jsonb_typeof(item->'unit') IS DISTINCT FROM 'string' OR
               octet_length(item->>'unit') NOT BETWEEN 1 AND 32 OR
               (item->>'unit') ~ '^\s|\s$' THEN
                RAISE EXCEPTION 'invalid value or unit' USING ERRCODE = '22023';
            END IF;
            measurement := (item->>'value')::double precision;
            IF measurement IN ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8) THEN
                RAISE EXCEPTION 'value must be finite' USING ERRCODE = '22023';
            END IF;
            -- Reset optional fields for every item; batches may mix located and unlocated values.
            longitude := NULL;
            latitude := NULL;
            location_id := NULL;
            IF item->>'lon_lat' IS NOT NULL THEN
                IF jsonb_typeof(item->'lon_lat') <> 'array' THEN
                    RAISE EXCEPTION 'lon_lat must be an array or null' USING ERRCODE = '22023';
                END IF;
                IF jsonb_array_length(item->'lon_lat') <> 2 OR
                   jsonb_typeof(item->'lon_lat'->0) IS DISTINCT FROM 'number' OR
                   jsonb_typeof(item->'lon_lat'->1) IS DISTINCT FROM 'number' THEN
                    RAISE EXCEPTION 'lon_lat must contain exactly two numbers' USING ERRCODE = '22023';
                END IF;
                longitude := (item->'lon_lat'->>0)::double precision;
                latitude := (item->'lon_lat'->>1)::double precision;
                IF longitude NOT BETWEEN -180 AND 180 OR latitude NOT BETWEEN -90 AND 90 THEN
                    RAISE EXCEPTION 'longitude or latitude out of range' USING ERRCODE = '22023';
                END IF;
            END IF;
            IF item->>'location_id' IS NOT NULL THEN
                IF jsonb_typeof(item->'location_id') <> 'number' THEN
                    RAISE EXCEPTION 'location_id must be an integer or null' USING ERRCODE = '22023';
                END IF;
                location_number := (item->>'location_id')::numeric;
                IF location_number <> trunc(location_number) OR location_number NOT BETWEEN 0 AND 9007199254740991 THEN
                    RAISE EXCEPTION 'location_id must be an integer between 0 and 9007199254740991' USING ERRCODE = '22023';
                END IF;
                location_id := location_number::bigint;
            END IF;
            IF item ? 'metadata' AND jsonb_typeof(item->'metadata') NOT IN ('object', 'null') THEN
                RAISE EXCEPTION 'metadata must be an object or null' USING ERRCODE = '22023';
            END IF;
            IF jsonb_typeof(item->'metadata') = 'object' AND EXISTS (
                SELECT 1 FROM jsonb_object_keys(item->'metadata') k WHERE octet_length(k) NOT BETWEEN 1 AND 128
            ) THEN
                RAISE EXCEPTION 'invalid metadata key' USING ERRCODE = '22023';
            END IF;
            normalized_time := regexp_replace(to_char(observed AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US'), '\.?0+$', '') || 'Z';
            canonical := jsonb_build_object('id', item->>'id', 'sensor_id', item->>'sensor_id',
                'sensor_type', item->>'sensor_type', 'timestamp', normalized_time,
                'value', measurement, 'unit', item->>'unit');
            IF coalesce(item->>'gateway_id','') <> '' THEN
                canonical := canonical || jsonb_build_object('gateway_id',item->>'gateway_id');
            END IF;
            IF jsonb_typeof(item->'metadata') = 'object' AND item->'metadata' <> '{}'::jsonb THEN
                canonical := canonical || jsonb_build_object('metadata',item->'metadata');
            END IF;
            IF longitude IS NOT NULL THEN
                canonical := canonical || jsonb_build_object('lon_lat', jsonb_build_array(longitude, latitude));
            END IF;
            IF location_id IS NOT NULL THEN
                canonical := canonical || jsonb_build_object('location_id', location_id);
            END IF;
            event_id := (item->>'id')::uuid;
            INSERT INTO sensor.events(id,observed_at,payload) VALUES(event_id,observed,canonical)
                ON CONFLICT(id) DO NOTHING;
            GET DIAGNOSTICS inserted = ROW_COUNT;
            IF inserted = 0 THEN
                SELECT payload INTO existing FROM sensor.events WHERE id=event_id;
                IF existing <> canonical THEN
                    RAISE EXCEPTION 'observation ID conflicts with existing content' USING ERRCODE = '23505';
                END IF;
            ELSE
                INSERT INTO sensor.measurements(observed_at,id,sensor_id,gateway_id,sensor_type,value,unit,metadata,longitude,latitude,location_id)
                VALUES(observed,event_id,item->>'sensor_id',nullif(item->>'gateway_id',''),
                       item->>'sensor_type',measurement,item->>'unit',
                       coalesce(nullif(item->'metadata','null'::jsonb),'{}'::jsonb),longitude,latitude,location_id);
            END IF;
        END LOOP;
    EXCEPTION WHEN data_exception OR integrity_constraint_violation THEN
        GET STACKED DIAGNOSTICS failure_code = RETURNED_SQLSTATE, failure_message = MESSAGE_TEXT;
        INSERT INTO sensor.rejected_messages(payload,error_code,reason)
        VALUES(coalesce(left(NEW.value,1048576),'null'),failure_code,failure_message);
    END;
    -- Successfully stored or quarantined documents are acknowledged by Telegraf.
    -- Do not accumulate a second copy in the adapter table.
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION sensor.ingest_mqtt() FROM PUBLIC;
