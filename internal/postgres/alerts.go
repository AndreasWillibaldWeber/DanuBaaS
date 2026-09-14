package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/alerts"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const ruleColumns = `id,sensor_id,enabled,level_warning,level_critical,rise_warning,rise_critical,rise_period_seconds,rise_window_seconds,rise_min_seconds,level_hysteresis,rise_hysteresis,hold_seconds,stale_seconds`

func (s *Store) SaveRule(ctx context.Context, r alerts.Rule, actor string, seed bool) (json.RawMessage, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	payload, _ := json.Marshal(r)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7349203)`); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `SELECT set_config('app.actor',$1,true)`, actor); err != nil {
		return nil, err
	}
	var version int
	err = tx.QueryRow(ctx, `SELECT version FROM configuration.alert_rules WHERE id=$1`, r.ID).Scan(&version)
	exists := err == nil
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if seed && exists {
		var out json.RawMessage
		err = tx.QueryRow(ctx, `SELECT to_jsonb(r) FROM configuration.alert_rules r WHERE id=$1`, r.ID).Scan(&out)
		return out, err
	}
	if (exists && version != r.Version) || (!exists && r.Version != 0) {
		return nil, alerts.ErrConflict
	}
	query := `INSERT INTO configuration.alert_rules (` + ruleColumns + `) SELECT ` + ruleColumns + ` FROM jsonb_populate_record(NULL::configuration.alert_rules,$1) RETURNING to_jsonb(alert_rules)`
	if exists {
		query = `UPDATE configuration.alert_rules SET (sensor_id,enabled,level_warning,level_critical,rise_warning,rise_critical,rise_period_seconds,rise_window_seconds,rise_min_seconds,level_hysteresis,rise_hysteresis,hold_seconds,stale_seconds) =
  (SELECT sensor_id,enabled,level_warning,level_critical,rise_warning,rise_critical,rise_period_seconds,rise_window_seconds,rise_min_seconds,level_hysteresis,rise_hysteresis,hold_seconds,stale_seconds FROM jsonb_populate_record(NULL::configuration.alert_rules,$1)) WHERE id=$2 RETURNING to_jsonb(alert_rules)`
	}
	var out json.RawMessage
	args := []any{payload}
	if exists {
		args = append(args, r.ID)
	}
	if err = tx.QueryRow(ctx, query, args...).Scan(&out); err != nil {
		var pg *pgconn.PgError
		if errors.As(err, &pg) && pg.Code == "23514" {
			return nil, alerts.ErrInvalid
		}
		if errors.As(err, &pg) && pg.Code == "23505" {
			return nil, alerts.ErrConflict
		}
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return out, nil
}
func (s *Store) SeedRules(ctx context.Context, reader io.Reader) error {
	var rules []alerts.Rule
	data, err := io.ReadAll(io.LimitReader(reader, (1<<20)+1))
	if err != nil {
		return err
	}
	if len(data) > 1<<20 {
		return errors.New("initial rule file exceeds 1 MiB")
	}
	if err := alerts.Decode(bytes.NewReader(data), &rules); err != nil {
		return err
	}
	if rules == nil || len(rules) > 1000 {
		return errors.New("initial rules must be an array with at most 1000 entries")
	}
	ids, sensors := map[string]bool{}, map[string]bool{}
	for _, r := range rules {
		if err := r.Validate(); err != nil {
			return fmt.Errorf("initial rule %s: %w", r.ID, err)
		}
		if r.Version != 0 || ids[r.ID] || sensors[r.SensorID] {
			return errors.New("initial rules need unique IDs/sensors and version 0")
		}
		ids[r.ID] = true
		sensors[r.SensorID] = true
	}
	for _, r := range rules {
		if _, err := s.SaveRule(ctx, r, "setup", true); err != nil {
			return err
		}
	}
	return nil
}
func (s *Store) AlertQuery(ctx context.Context, resource, id string, after int64, limit int, active bool) (json.RawMessage, error) {
	var query string
	var args []any
	switch resource {
	case "rules":
		query = `SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id),'[]'::jsonb) FROM (SELECT * FROM configuration.alert_rules WHERE id>$1 ORDER BY id LIMIT $2) r`
		args = []any{id, limit}
	case "rule":
		query = `SELECT to_jsonb(r) FROM configuration.alert_rules r WHERE id=$1`
		args = []any{id}
	case "versions":
		query = `SELECT coalesce(jsonb_agg(to_jsonb(v) ORDER BY version),'[]'::jsonb) FROM (SELECT * FROM configuration.alert_rule_versions WHERE rule_id=$1 AND version>$2 ORDER BY version LIMIT $3) v`
		args = []any{id, after, limit}
	case "alerts":
		query = `SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY id),'[]'::jsonb) FROM (SELECT * FROM alerting.alerts WHERE id>$1 AND (NOT $3 OR resolved_at IS NULL) ORDER BY id LIMIT $2) a`
		args = []any{after, limit, active}
	case "alert":
		query = `SELECT to_jsonb(a) FROM alerting.alerts a WHERE id=$1::bigint`
		args = []any{id}
	case "events":
		query = `SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY id),'[]'::jsonb) FROM (SELECT * FROM alerting.events WHERE alert_id=$1::bigint AND id>$2 ORDER BY id LIMIT $3) e`
		args = []any{id, after, limit}
	case "status":
		query = `SELECT jsonb_build_object('healthy',last_success_at IS NOT NULL AND last_success_at>clock_timestamp()-make_interval(secs=>schedule_seconds*3),
  'last_success_at',last_success_at,'schedule_seconds',schedule_seconds,
  'enabled_rules',(SELECT count(*) FROM configuration.alert_rules WHERE enabled),
  'pending_notifications',(SELECT count(*) FROM alerting.notification_outbox WHERE delivered_at IS NULL),
  'oldest_pending_notification_at',(SELECT min(e.created_at) FROM alerting.notification_outbox o JOIN alerting.events e ON e.id=o.event_id WHERE o.delivered_at IS NULL),
  'states',(SELECT coalesce(jsonb_agg(to_jsonb(s)),'[]'::jsonb) FROM (SELECT * FROM alerting.rule_state ORDER BY rule_id LIMIT 100) s)) FROM alerting.evaluator_status`
	default:
		return nil, alerts.ErrNotFound
	}
	var out json.RawMessage
	err := s.pool.QueryRow(ctx, query, args...).Scan(&out)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, alerts.ErrNotFound
	}
	return out, err
}
func (s *Store) Acknowledge(ctx context.Context, id int64, actor, note string) error {
	var found bool
	err := s.pool.QueryRow(ctx, `SELECT alerting.acknowledge($1,$2,$3)`, id, actor, note).Scan(&found)
	if err == nil && !found {
		return alerts.ErrNotFound
	}
	return err
}

// ConfigureSchedule is administrative and preserves the single job registered by migration.
func (s *Store) ConfigureSchedule(ctx context.Context, seconds int) error {
	if seconds < 1 || seconds > 3600 {
		return errors.New("ALERT_EVALUATION_SECONDS must be between 1 and 3600")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(7349203)`); err != nil {
		return err
	}
	var job int
	if err = tx.QueryRow(ctx, `SELECT job_id FROM timescaledb_information.jobs WHERE proc_schema='alerting' AND proc_name='evaluate_rules'`).Scan(&job); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `SELECT public.alter_job($1,schedule_interval=>make_interval(secs=>$2),max_runtime=>interval '20 seconds',retry_period=>interval '10 seconds')`, job, seconds); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE alerting.evaluator_status SET schedule_seconds=$1`, seconds); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type Notification struct {
	ID       int64
	Event    json.RawMessage
	Attempts int
}

func (s *Store) ClaimNotification(ctx context.Context) (Notification, error) {
	var n Notification
	err := s.pool.QueryRow(ctx, `WITH candidate AS (
 SELECT id FROM alerting.notification_outbox WHERE delivered_at IS NULL AND next_attempt_at<=clock_timestamp() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
 ), claimed AS (
 UPDATE alerting.notification_outbox o SET attempts=attempts+1,next_attempt_at=clock_timestamp()+interval '60 seconds'
 FROM candidate c WHERE o.id=c.id RETURNING o.*)
 SELECT c.id,c.attempts,jsonb_build_object('event',to_jsonb(e),'alert',to_jsonb(a),'rule',v.parameters)
 FROM claimed c JOIN alerting.events e ON e.id=c.event_id JOIN alerting.alerts a ON a.id=e.alert_id
 JOIN configuration.alert_rule_versions v ON v.rule_id=a.rule_id AND v.version=a.rule_version`).Scan(&n.ID, &n.Attempts, &n.Event)
	if errors.Is(err, pgx.ErrNoRows) {
		return n, alerts.ErrNotFound
	}
	return n, err
}
func (s *Store) FinishNotification(ctx context.Context, n Notification, delivered bool) error {
	delay := time.Duration(1<<min(n.Attempts, 10)) * time.Second
	_, err := s.pool.Exec(ctx, `UPDATE alerting.notification_outbox SET delivered_at=CASE WHEN $3 THEN clock_timestamp() ELSE NULL END,
 next_attempt_at=clock_timestamp()+make_interval(secs=>$4),last_error=CASE WHEN $3 THEN NULL ELSE 'webhook delivery failed' END
 WHERE id=$1 AND attempts=$2 AND delivered_at IS NULL`, n.ID, n.Attempts, delivered, int(delay.Seconds()))
	return err
}
