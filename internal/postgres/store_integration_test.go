//go:build integration

package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
)

// This suite intentionally refuses to run without an explicitly disposable database.
func TestPostgresContract(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Fatal("TEST_DATABASE_URL must name a disposable TimescaleDB database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	s, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if err = s.Migrate(ctx); err != nil {
		t.Fatalf("migration not idempotent: %v", err)
	}
	// Do not truncate shared tables: this suite uses a fresh set of UUIDs per run.
	prefix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()))
	value := func(n int) sensor.Value {
		v := 0.0
		return sensor.Value{ID: fmt.Sprintf("%s-0000-4000-8000-%012d", prefix, n), SensorID: "quality-test", SensorType: "water-level", Timestamp: time.Date(2026, 9, 14, 10, 0, 0, 0, time.UTC), Value: &v, Unit: "m", Metadata: map[string]json.RawMessage{"raw": json.RawMessage(`{"rssi":-70,"nested":[1,null,"x"]}`)}}
	}
	a, b := value(1), value(2)
	records, n, err := s.Put(ctx, []sensor.Value{a, b})
	if err != nil || n != 2 {
		t.Fatalf("create: %d %v", n, err)
	}
	again, n, err := s.Put(ctx, []sensor.Value{a, b})
	if err != nil || n != 0 || again[0].Sequence != records[0].Sequence {
		t.Fatalf("replay: %d %v", n, err)
	}
	got, err := s.Get(ctx, a.ID)
	if err != nil || string(got.Metadata["raw"]) == "" || *got.Value.Value != 0 {
		t.Fatalf("roundtrip: %+v %v", got, err)
	}
	changed := a
	different := 1.0
	changed.Value = &different
	c := value(3)
	if _, _, err = s.Put(ctx, []sensor.Value{c, changed}); !errors.Is(err, sensor.ErrConflict) {
		t.Fatalf("expected conflict, got %v", err)
	}
	if _, err = s.Get(ctx, c.ID); !errors.Is(err, sensor.ErrNotFound) {
		t.Fatalf("batch was partially committed: %v", err)
	}
	var count int
	if err = s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.measurements WHERE id=$1`, a.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("hypertable duplicate: %d %v", count, err)
	}
	concurrent := value(4)
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, _, err := s.Put(ctx, []sensor.Value{concurrent}); err != nil {
				t.Errorf("concurrent retry: %v", err)
			}
		}()
	}
	wg.Wait()
	if err = s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.events WHERE id=$1`, concurrent.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("registry duplicate: %d %v", count, err)
	}
	page, err := s.List(ctx, records[0].Sequence, 1)
	if err != nil || len(page) != 1 || page[0].ID != b.ID {
		t.Fatalf("pagination: %+v %v", page, err)
	}
	canceled, cancelNow := context.WithCancel(ctx)
	cancelNow()
	if _, _, err = s.Put(canceled, []sensor.Value{value(5)}); err == nil {
		t.Fatal("canceled write succeeded")
	}
}
