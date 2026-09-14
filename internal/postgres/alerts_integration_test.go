//go:build integration

package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/alerts"
	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
)

func TestAlertLifecycle(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Fatal("TEST_DATABASE_URL must name a disposable TimescaleDB database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	s, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	// Manual evaluation uses a virtual clock; keep the wall-clock scheduler from
	// interfering with these deterministic cases, and restore it on exit.
	var job int
	if err = s.pool.QueryRow(ctx, `SELECT job_id FROM timescaledb_information.jobs WHERE proc_schema='alerting' AND proc_name='evaluate_rules'`).Scan(&job); err != nil {
		t.Fatal(err)
	}
	if _, err = s.pool.Exec(ctx, `SELECT alter_job($1,scheduled=>false)`, job); err != nil {
		t.Fatal(err)
	}
	defer s.pool.Exec(context.Background(), `SELECT alter_job($1,scheduled=>true)`, job)
	prefix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()))
	number := 0
	ptr := func(v float64) *float64 { return &v }
	newRule := func(name string) alerts.Rule {
		r := alerts.Rule{ID: prefix + "-" + name, SensorID: prefix + "-" + name, Enabled: true, LevelWarning: ptr(2), LevelCritical: ptr(3), RiseWarning: ptr(10), RiseCritical: ptr(20), RisePeriodSeconds: 300, RiseWindowSeconds: 600, RiseMinSeconds: 30, LevelHysteresis: .1, RiseHysteresis: .1, StaleSeconds: 900}
		return r
	}
	save := func(t *testing.T, r alerts.Rule) alerts.Rule {
		t.Helper()
		raw, e := s.SaveRule(ctx, r, "test-admin", false)
		if e != nil {
			t.Fatal(e)
		}
		if e = json.Unmarshal(raw, &r); e != nil {
			t.Fatal(e)
		}
		return r
	}
	put := func(t *testing.T, r alerts.Rule, at time.Time, v float64, mqtt bool) sensor.Value {
		t.Helper()
		number++
		value := sensor.Value{ID: fmt.Sprintf("%s-0000-4000-8000-%012d", prefix, number), SensorID: r.SensorID, SensorType: "water-level", Timestamp: at, Value: &v, Unit: "m"}
		if e := value.Normalize(); e != nil {
			t.Fatal(e)
		}
		if mqtt {
			raw, _ := json.Marshal(value)
			_, e := s.pool.Exec(ctx, `INSERT INTO sensor.mqtt_ingest(time,value) VALUES(clock_timestamp(),$1)`, string(raw))
			if e != nil {
				t.Fatal(e)
			}
		} else {
			if _, _, e := s.Put(ctx, []sensor.Value{value}); e != nil {
				t.Fatal(e)
			}
		}
		return value
	}
	evaluate := func(t *testing.T, at time.Time) {
		t.Helper()
		if _, e := s.pool.Exec(ctx, `SELECT alerting.evaluate($1)`, at); e != nil {
			t.Fatal(e)
		}
	}
	severity := func(t *testing.T, r alerts.Rule, kind, expected string) {
		t.Helper()
		var got string
		if e := s.pool.QueryRow(ctx, `SELECT severity FROM alerting.condition_state WHERE rule_id=$1 AND kind=$2`, r.ID, kind).Scan(&got); e != nil || got != expected {
			t.Fatalf("%s %s got %q want %q: %v", r.ID, kind, got, expected, e)
		}
	}
	for _, mqtt := range []bool{false, true} {
		t.Run(fmt.Sprintf("level-mqtt-%t", mqtt), func(t *testing.T) {
			r := save(t, newRule(fmt.Sprintf("level-%t", mqtt)))
			base := r.UpdatedAt.Add(time.Second).Truncate(time.Microsecond)
			value := put(t, r, base, 2.1, mqtt)
			evaluate(t, base)
			severity(t, r, "level", "warning")
			if _, _, e := s.Put(ctx, []sensor.Value{value}); e != nil {
				t.Fatal(e)
			}
			evaluate(t, base)
			put(t, r, base.Add(time.Minute), 3.1, mqtt)
			evaluate(t, base.Add(time.Minute))
			severity(t, r, "level", "critical")
			put(t, r, base.Add(2*time.Minute), 2.95, mqtt)
			evaluate(t, base.Add(2*time.Minute))
			severity(t, r, "level", "critical")
			put(t, r, base.Add(3*time.Minute), 2.8, mqtt)
			evaluate(t, base.Add(3*time.Minute))
			severity(t, r, "level", "warning")
			put(t, r, base.Add(4*time.Minute), 1.8, mqtt)
			evaluate(t, base.Add(4*time.Minute))
			severity(t, r, "level", "normal")
			var count int
			var aid int64
			if e := s.pool.QueryRow(ctx, `SELECT count(*),min(id) FROM alerting.alerts WHERE rule_id=$1 AND kind='level'`, r.ID).Scan(&count, &aid); e != nil || count != 1 {
				t.Fatalf("duplicate instance: %d %v", count, e)
			}
			var events string
			if e := s.pool.QueryRow(ctx, `SELECT string_agg(event_type,',' ORDER BY id) FROM alerting.events WHERE alert_id=$1`, aid).Scan(&events); e != nil || events != "opened,escalated,downgraded,resolved" {
				t.Fatalf("events %s: %v", events, e)
			}
			if e := s.Acknowledge(ctx, aid, "test-admin", "investigated"); e != nil {
				t.Fatal(e)
			}
			if e := s.Acknowledge(ctx, aid, "other", "retry"); e != nil {
				t.Fatal(e)
			}
			var actor string
			if e := s.pool.QueryRow(ctx, `SELECT acknowledged_by FROM alerting.alerts WHERE id=$1`, aid).Scan(&actor); e != nil || actor != "test-admin" {
				t.Fatal("acknowledgment changed", e)
			}
			// An old high reading cannot rewind live state.
			put(t, r, base.Add(30*time.Second), 9, mqtt)
			evaluate(t, base.Add(4*time.Minute))
			severity(t, r, "level", "normal")
		})
	}
	t.Run("rise warning critical and time normalization", func(t *testing.T) {
		r := newRule("rise")
		r.LevelWarning = ptr(100)
		r.LevelCritical = ptr(200)
		r.RiseWarning = ptr(.1)
		r.RiseCritical = ptr(.2)
		r.RiseHysteresis = .01
		r = save(t, r)
		base := r.UpdatedAt.Add(time.Second).Truncate(time.Microsecond)
		put(t, r, base, 1, false)
		evaluate(t, base)
		severity(t, r, "rise", "normal")
		put(t, r, base.Add(300*time.Second), 1.15, false)
		evaluate(t, base.Add(300*time.Second))
		severity(t, r, "rise", "warning")
		put(t, r, base.Add(600*time.Second), 1.5, false)
		evaluate(t, base.Add(600*time.Second))
		severity(t, r, "rise", "critical")
		put(t, r, base.Add(900*time.Second), 1.15, false)
		evaluate(t, base.Add(900*time.Second))
		severity(t, r, "rise", "normal")
	})
	t.Run("hold and missing sensor", func(t *testing.T) {
		r := newRule("hold")
		r.HoldSeconds = 60
		r = save(t, r)
		base := r.UpdatedAt.Add(time.Second).Truncate(time.Microsecond)
		evaluate(t, base.Add(901*time.Second))
		severity(t, r, "stale", "warning")
		put(t, r, base.Add(902*time.Second), 2.2, false)
		evaluate(t, base.Add(902*time.Second))
		severity(t, r, "level", "normal")
		severity(t, r, "stale", "normal")
		// Repeated polling of one sample must not satisfy the hold duration.
		evaluate(t, base.Add(970*time.Second))
		severity(t, r, "level", "normal")
		put(t, r, base.Add(971*time.Second), 2.3, false)
		evaluate(t, base.Add(971*time.Second))
		severity(t, r, "level", "warning")
		evaluate(t, base.Add(2000*time.Second))
		severity(t, r, "stale", "warning")
		severity(t, r, "level", "warning")
	})
	t.Run("batch excursions and unusable readings", func(t *testing.T) {
		r := save(t, newRule("batch"))
		base := r.UpdatedAt.Add(time.Second).Truncate(time.Microsecond)
		put(t, r, base, 1, false)
		put(t, r, base.Add(time.Second), 3.2, false)
		put(t, r, base.Add(2*time.Second), 1, false)
		evaluate(t, base.Add(2*time.Second))
		severity(t, r, "level", "normal")
		var count int
		if e := s.pool.QueryRow(ctx, `SELECT count(*) FROM alerting.alerts WHERE rule_id=$1 AND kind='level' AND resolved_at IS NOT NULL`, r.ID).Scan(&count); e != nil || count != 1 {
			t.Fatal("intermediate breach was missed", e)
		}
		number++
		v := 99.0
		invalid := sensor.Value{ID: fmt.Sprintf("%s-0000-4000-8000-%012d", prefix, number), SensorID: r.SensorID, SensorType: "water-level", Timestamp: base.Add(3 * time.Second), Value: &v, Unit: "cm"}
		if e := invalid.Normalize(); e != nil {
			t.Fatal(e)
		}
		if _, _, e := s.Put(ctx, []sensor.Value{invalid}); e != nil {
			t.Fatal(e)
		}
		evaluate(t, base.Add(3*time.Second))
		severity(t, r, "level", "normal")
		put(t, r, base.Add(4*time.Second), 99, false)
		evaluate(t, base.Add(3*time.Second))
		severity(t, r, "level", "normal")
		// A future-dated arrival skipped once is not replayed when its time arrives.
		evaluate(t, base.Add(4*time.Second))
		severity(t, r, "level", "normal")
	})

	t.Run("extreme finite observations cannot overflow evaluator", func(t *testing.T) {
		r := save(t, newRule("extreme"))
		base := r.UpdatedAt.Add(time.Second).Truncate(time.Microsecond)
		put(t, r, base, -1e308, false)
		evaluate(t, base)
		put(t, r, base.Add(time.Minute), 1e308, false)
		evaluate(t, base.Add(time.Minute))
		severity(t, r, "level", "critical")
		severity(t, r, "rise", "critical")
	})

	t.Run("versioning and setup preservation", func(t *testing.T) {
		r := save(t, newRule("version"))
		original := r
		r.LevelWarning = ptr(2.5)
		r = save(t, r)
		if r.Version != 2 {
			t.Fatal("version not incremented")
		}
		if _, e := s.SaveRule(ctx, original, "stale-editor", false); !errors.Is(e, alerts.ErrConflict) {
			t.Fatalf("stale version accepted: %v", e)
		}
		original.Version = 0
		original.UpdatedAt = time.Time{}
		seed, _ := json.Marshal([]alerts.Rule{original})
		if e := s.SeedRules(ctx, strings.NewReader(string(seed))); e != nil {
			t.Fatal(e)
		}
		raw, e := s.AlertQuery(ctx, "rule", r.ID, 0, 100, false)
		if e != nil {
			t.Fatal(e)
		}
		var got alerts.Rule
		_ = json.Unmarshal(raw, &got)
		if got.Version != 2 || *got.LevelWarning != 2.5 {
			t.Fatal("setup overwrote operator configuration")
		}
		if _, e = s.pool.Exec(ctx, `UPDATE configuration.alert_rules SET rise_critical=rise_warning WHERE id=$1`, r.ID); e == nil {
			t.Fatal("database accepted invalid thresholds")
		}
	})
	t.Run("outbox retry and claim", func(t *testing.T) {
		n, e := s.ClaimNotification(ctx)
		if e != nil {
			t.Fatal(e)
		}
		next, e := s.ClaimNotification(ctx)
		if e != nil {
			t.Fatal(e)
		}
		if n.ID == next.ID {
			t.Fatal("leased notification claimed twice")
		}
		if e = s.FinishNotification(ctx, n, false); e != nil {
			t.Fatal(e)
		}
		var delivered bool
		if e = s.pool.QueryRow(ctx, `SELECT delivered_at IS NOT NULL FROM alerting.notification_outbox WHERE id=$1`, n.ID).Scan(&delivered); e != nil || delivered {
			t.Fatal("failed delivery marked delivered", e)
		}
		if e = s.FinishNotification(ctx, next, true); e != nil {
			t.Fatal(e)
		}
	})
	// Execute as the actual restricted scheduler owner to catch missing grants.
	t.Run("scheduler owner permissions", func(t *testing.T) {
		tx, e := s.pool.Begin(ctx)
		if e != nil {
			t.Fatal(e)
		}
		defer tx.Rollback(ctx)
		if _, e = tx.Exec(ctx, `SET LOCAL ROLE alert_evaluator; CALL alerting.evaluate_rules(0,'{}')`); e != nil {
			t.Fatal(e)
		}
	})
	if err = s.ConfigureSchedule(ctx, 5); err != nil {
		t.Fatal(err)
	}
	if err = s.Migrate(ctx); err != nil {
		t.Fatal("migration rerun", err)
	}
}
