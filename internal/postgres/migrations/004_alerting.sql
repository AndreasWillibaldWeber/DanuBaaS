-- Alert rules operate on canonical water-level observations in metres.
CREATE INDEX events_by_sensor_sequence ON sensor.events ((payload->>'sensor_id'),sequence);
CREATE SCHEMA configuration;
CREATE SCHEMA alerting;
CREATE TABLE configuration.alert_rules (
 id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
 sensor_id text NOT NULL UNIQUE CHECK (sensor_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
 enabled boolean NOT NULL,
 level_warning float8 NOT NULL CHECK (level_warning > '-Infinity'::float8 AND level_warning < 'Infinity'::float8),
 level_critical float8 NOT NULL CHECK (level_critical < 'Infinity'::float8 AND level_warning < level_critical),
 rise_warning float8 NOT NULL CHECK (rise_warning > 0),
 rise_critical float8 NOT NULL CHECK (rise_critical < 'Infinity'::float8 AND rise_warning < rise_critical),
 rise_period_seconds integer NOT NULL CHECK (rise_period_seconds BETWEEN 1 AND 86400),
 rise_window_seconds integer NOT NULL CHECK (rise_window_seconds BETWEEN 2 AND 86400),
 rise_min_seconds integer NOT NULL CHECK (rise_min_seconds >= 1 AND rise_min_seconds < rise_window_seconds),
 level_hysteresis float8 NOT NULL CHECK (level_hysteresis >= 0 AND level_hysteresis::numeric < level_critical::numeric-level_warning::numeric),
 rise_hysteresis float8 NOT NULL CHECK (rise_hysteresis >= 0 AND rise_hysteresis::numeric < rise_critical::numeric-rise_warning::numeric),
 hold_seconds integer NOT NULL CHECK (hold_seconds >= 0 AND hold_seconds <= stale_seconds),
 stale_seconds integer NOT NULL CHECK (stale_seconds BETWEEN rise_min_seconds AND 604800),
 version integer NOT NULL DEFAULT 1 CHECK (version > 0),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE configuration.alert_rule_versions (
 rule_id text NOT NULL REFERENCES configuration.alert_rules(id), version integer NOT NULL,
 parameters jsonb NOT NULL, changed_by text NOT NULL, changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (rule_id, version)
);
CREATE TABLE alerting.rule_state (
 rule_id text PRIMARY KEY REFERENCES configuration.alert_rules(id),
 last_sequence bigint NOT NULL DEFAULT 0, last_observed_at timestamptz,
 last_observation_id uuid, last_value float8
);
CREATE TABLE alerting.condition_state (
 rule_id text NOT NULL REFERENCES configuration.alert_rules(id),
 kind text NOT NULL CHECK (kind IN ('level','rise','stale')),
 severity text NOT NULL DEFAULT 'normal' CHECK (severity IN ('normal','warning','critical')),
 pending_severity text, pending_since timestamptz,
 PRIMARY KEY (rule_id,kind)
);
CREATE TABLE alerting.alerts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 rule_id text NOT NULL, rule_version integer NOT NULL,
 kind text NOT NULL CHECK (kind IN ('level','rise','stale')),
 severity text NOT NULL CHECK (severity IN ('warning','critical')),
 opened_at timestamptz NOT NULL, resolved_at timestamptz,
 acknowledged_at timestamptz, acknowledged_by text, acknowledgment_note text,
 FOREIGN KEY (rule_id,rule_version) REFERENCES configuration.alert_rule_versions(rule_id,version)
);
CREATE UNIQUE INDEX one_active_alert ON alerting.alerts(rule_id,kind) WHERE resolved_at IS NULL;
CREATE TABLE alerting.events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 alert_id bigint NOT NULL REFERENCES alerting.alerts(id),
 event_type text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 details jsonb NOT NULL
);
CREATE INDEX ON alerting.events(alert_id,id);
CREATE TABLE alerting.notification_outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 event_id bigint NOT NULL UNIQUE REFERENCES alerting.events(id),
 attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 delivered_at timestamptz, last_error text
);
CREATE TABLE alerting.evaluator_status (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 last_success_at timestamptz, schedule_seconds integer NOT NULL DEFAULT 30
);
INSERT INTO alerting.evaluator_status(singleton) VALUES(true);

CREATE FUNCTION alerting.record_event(aid bigint, event_name text, data jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE eid bigint;
BEGIN
 INSERT INTO alerting.events(alert_id,event_type,details) VALUES(aid,event_name,data) RETURNING id INTO eid;
 IF event_name <> 'acknowledged' THEN
  INSERT INTO alerting.notification_outbox(event_id) VALUES(eid);
 END IF;
END $$;

-- Rule edits and evaluations take the same transaction lock. A version change
-- retires old conditions; it never rewrites the historical rule snapshot.
CREATE FUNCTION configuration.audit_rule() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE a record;
BEGIN
 PERFORM pg_advisory_xact_lock(7349203);
 IF TG_OP = 'UPDATE' THEN
  IF NEW.id <> OLD.id OR NEW.sensor_id <> OLD.sensor_id THEN
   RAISE EXCEPTION 'rule identity is immutable' USING ERRCODE='23514';
  END IF;
  NEW.version := OLD.version + 1;
 END IF;
 NEW.updated_at := clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER alert_rule_before BEFORE INSERT OR UPDATE ON configuration.alert_rules
 FOR EACH ROW EXECUTE FUNCTION configuration.audit_rule();
CREATE FUNCTION configuration.rule_changed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE a record;
BEGIN
 INSERT INTO configuration.alert_rule_versions(rule_id,version,parameters,changed_by)
 VALUES(NEW.id,NEW.version,to_jsonb(NEW),coalesce(nullif(current_setting('app.actor',true),''),session_user));
 FOR a IN UPDATE alerting.alerts SET resolved_at=NEW.updated_at WHERE rule_id=NEW.id AND resolved_at IS NULL RETURNING id LOOP
  PERFORM alerting.record_event(a.id,'configuration_changed',jsonb_build_object('new_version',NEW.version));
 END LOOP;
 INSERT INTO alerting.rule_state(rule_id,last_sequence)
 VALUES(NEW.id,coalesce((SELECT max(sequence) FROM sensor.events),0))
 ON CONFLICT(rule_id) DO UPDATE SET last_sequence=excluded.last_sequence,last_observed_at=NULL,last_observation_id=NULL,last_value=NULL;
 DELETE FROM alerting.condition_state WHERE rule_id=NEW.id;
 INSERT INTO alerting.condition_state(rule_id,kind) VALUES(NEW.id,'level'),(NEW.id,'rise'),(NEW.id,'stale');
 RETURN NEW;
END $$;
CREATE TRIGGER alert_rule_after AFTER INSERT OR UPDATE ON configuration.alert_rules
 FOR EACH ROW EXECUTE FUNCTION configuration.rule_changed();

CREATE FUNCTION alerting.classify(value numeric, warning numeric, critical numeric, hysteresis numeric, previous text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE WHEN value >= critical OR (previous='critical' AND value >= critical-hysteresis) THEN 'critical'
 WHEN value >= warning OR (previous IN ('warning','critical') AND value >= warning-hysteresis) THEN 'warning'
 ELSE 'normal' END
$$;

CREATE FUNCTION alerting.transition(r configuration.alert_rules, condition text, target text,
 at_time timestamptz, evaluated_at timestamptz, data jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE s alerting.condition_state; aid bigint; action text;
BEGIN
 SELECT * INTO STRICT s FROM alerting.condition_state WHERE rule_id=r.id AND kind=condition FOR UPDATE;
 IF target=s.severity THEN
  UPDATE alerting.condition_state SET pending_severity=NULL,pending_since=NULL WHERE rule_id=r.id AND kind=condition;
  RETURN;
 END IF;
 -- Hold applies to entering/escalating abnormal conditions. Recovery/downgrade
 -- uses hysteresis; missing data has already waited for stale_seconds.
 IF target <> 'normal' AND condition <> 'stale' AND NOT (s.severity='critical' AND target='warning') THEN
  IF s.pending_severity IS DISTINCT FROM target THEN s.pending_since:=at_time; END IF;
  UPDATE alerting.condition_state SET pending_severity=target,pending_since=s.pending_since WHERE rule_id=r.id AND kind=condition;
  IF at_time-s.pending_since < make_interval(secs=>r.hold_seconds) THEN RETURN; END IF;
 END IF;
 SELECT id INTO aid FROM alerting.alerts WHERE rule_id=r.id AND kind=condition AND resolved_at IS NULL;
 IF target='normal' THEN
  UPDATE alerting.alerts SET resolved_at=evaluated_at WHERE id=aid;
  action:='resolved';
 ELSIF aid IS NULL THEN
  INSERT INTO alerting.alerts(rule_id,rule_version,kind,severity,opened_at)
  VALUES(r.id,r.version,condition,target,evaluated_at) RETURNING id INTO aid;
  action:='opened';
 ELSE
  UPDATE alerting.alerts SET severity=target WHERE id=aid;
  action:=CASE WHEN target='critical' THEN 'escalated' ELSE 'downgraded' END;
 END IF;
 IF aid IS NOT NULL THEN
  PERFORM alerting.record_event(aid,action,data || jsonb_build_object('severity',target,'condition_at',at_time,'rule_version',r.version));
 END IF;
 UPDATE alerting.condition_state SET severity=target,pending_since=NULL,pending_severity=NULL WHERE rule_id=r.id AND kind=condition;
END $$;

-- Explicit evaluation time allows deterministic integration tests. The scheduler
-- wrapper supplies wall time. No network I/O happens in this transaction.
CREATE FUNCTION alerting.evaluate(evaluation_time timestamptz) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE r configuration.alert_rules; s alerting.rule_state; m record; baseline record;
 high_sequence bigint; previous text; target text; rate numeric; evidence jsonb;
BEGIN
 IF NOT pg_try_advisory_xact_lock(7349203) THEN RETURN; END IF;
 SELECT coalesce(max(sequence),0) INTO high_sequence FROM sensor.events;
 FOR r IN SELECT * FROM configuration.alert_rules WHERE enabled ORDER BY id LOOP
  SELECT * INTO STRICT s FROM alerting.rule_state WHERE rule_id=r.id;
  -- Process a bounded arrival batch in observation-time order. Older/equal
  -- timestamps never rewind live state. Future and stale arrivals are ignored.
  FOR m IN SELECT batch.* FROM (
   SELECT e.sequence,obs.observed_at,obs.id,obs.value,obs.unit,obs.sensor_type FROM sensor.events e
   JOIN sensor.measurements obs ON obs.id=e.id AND obs.observed_at=e.observed_at
   WHERE e.sequence>s.last_sequence AND e.sequence<=high_sequence AND obs.sensor_id=r.sensor_id AND e.payload->>'sensor_id'=r.sensor_id
   ORDER BY e.sequence LIMIT 1000
  ) batch ORDER BY observed_at,id LOOP
   s.last_sequence:=greatest(s.last_sequence,m.sequence);
   IF m.sensor_type <> 'water-level' OR m.unit <> 'm' OR m.observed_at > evaluation_time
    OR m.observed_at < r.updated_at OR m.observed_at < evaluation_time-make_interval(secs=>r.stale_seconds)
    OR (s.last_observed_at IS NOT NULL AND m.observed_at<=s.last_observed_at) THEN CONTINUE; END IF;
   IF s.last_observed_at IS NULL OR m.observed_at-s.last_observed_at > make_interval(secs=>r.stale_seconds) THEN
    UPDATE alerting.condition_state SET pending_since=NULL,pending_severity=NULL WHERE rule_id=r.id;
   END IF;
   evidence:=jsonb_build_object('observation_id',m.id,'observed_at',m.observed_at,'value',m.value,'unit','m');
   SELECT severity INTO previous FROM alerting.condition_state WHERE rule_id=r.id AND kind='level';
   target:=alerting.classify(m.value::numeric,r.level_warning::numeric,r.level_critical::numeric,r.level_hysteresis::numeric,previous);
   PERFORM alerting.transition(r,'level',target,m.observed_at,evaluation_time,evidence);
   SELECT b.id,b.value,b.observed_at INTO baseline FROM sensor.measurements b
    WHERE b.sensor_id=r.sensor_id AND b.sensor_type='water-level' AND b.unit='m'
    AND b.observed_at>=m.observed_at-make_interval(secs=>r.rise_window_seconds)
    AND b.observed_at<=m.observed_at-make_interval(secs=>r.rise_min_seconds)
    ORDER BY b.observed_at,b.id LIMIT 1;
   IF FOUND THEN
    rate:=(m.value::numeric-baseline.value::numeric)/extract(epoch FROM m.observed_at-baseline.observed_at)*r.rise_period_seconds;
    SELECT severity INTO previous FROM alerting.condition_state WHERE rule_id=r.id AND kind='rise';
    target:=alerting.classify(rate,r.rise_warning::numeric,r.rise_critical::numeric,r.rise_hysteresis::numeric,previous);
    PERFORM alerting.transition(r,'rise',target,m.observed_at,evaluation_time,evidence || jsonb_build_object('rate',rate,'period_seconds',r.rise_period_seconds,'baseline_id',baseline.id));
   ELSE
    UPDATE alerting.condition_state SET pending_since=NULL,pending_severity=NULL WHERE rule_id=r.id AND kind='rise';
   END IF;
   s.last_observed_at:=m.observed_at; s.last_observation_id:=m.id; s.last_value:=m.value;
  END LOOP;
  UPDATE alerting.rule_state SET last_sequence=s.last_sequence,last_observed_at=s.last_observed_at,
   last_observation_id=s.last_observation_id,last_value=s.last_value WHERE rule_id=r.id;
  target:=CASE WHEN evaluation_time-coalesce(s.last_observed_at,r.updated_at)>=make_interval(secs=>r.stale_seconds) THEN 'warning' ELSE 'normal' END;
  PERFORM alerting.transition(r,'stale',target,evaluation_time,evaluation_time,jsonb_build_object('last_observed_at',s.last_observed_at));
  IF target='warning' THEN
   UPDATE alerting.condition_state SET pending_since=NULL,pending_severity=NULL WHERE rule_id=r.id;
  END IF;
 END LOOP;
 UPDATE alerting.evaluator_status SET last_success_at=evaluation_time WHERE singleton;
END $$;
CREATE PROCEDURE alerting.evaluate_rules(job_id integer, config jsonb)
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN PERFORM alerting.evaluate(clock_timestamp()); END $$;

CREATE FUNCTION alerting.acknowledge(aid bigint, actor text, note text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
 IF length(actor) NOT BETWEEN 1 AND 128 OR length(note)>2000 THEN RAISE EXCEPTION 'invalid acknowledgment' USING ERRCODE='23514'; END IF;
 UPDATE alerting.alerts SET acknowledged_at=clock_timestamp(),acknowledged_by=actor,acknowledgment_note=note
 WHERE id=aid AND acknowledged_at IS NULL;
 IF FOUND THEN PERFORM alerting.record_event(aid,'acknowledged',jsonb_build_object('actor',actor,'note',note)); END IF;
 RETURN EXISTS(SELECT 1 FROM alerting.alerts WHERE id=aid);
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA alerting,configuration FROM PUBLIC;
REVOKE ALL ON PROCEDURE alerting.evaluate_rules(integer,jsonb) FROM PUBLIC;
-- TimescaleDB requires a LOGIN job owner. No password is assigned; deployed
-- password-authenticated network connections cannot use this restricted role.
DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='alert_evaluator') THEN CREATE ROLE alert_evaluator LOGIN; END IF; END $$;
GRANT USAGE ON SCHEMA sensor,configuration,alerting TO alert_evaluator;
GRANT SELECT ON sensor.events,sensor.measurements,configuration.alert_rules,configuration.alert_rule_versions TO alert_evaluator;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA alerting TO alert_evaluator;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA alerting TO alert_evaluator;
GRANT EXECUTE ON FUNCTION alerting.evaluate(timestamptz),alerting.classify(numeric,numeric,numeric,numeric,text),
 alerting.transition(configuration.alert_rules,text,text,timestamptz,timestamptz,jsonb),alerting.record_event(bigint,text,jsonb) TO alert_evaluator;
GRANT EXECUTE ON PROCEDURE alerting.evaluate_rules(integer,jsonb) TO alert_evaluator;
SET LOCAL ROLE alert_evaluator;
SELECT public.add_job('alerting.evaluate_rules',INTERVAL '30 seconds');
RESET ROLE;
