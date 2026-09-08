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
	"github.com/jackc/pgx/v5"
)

// MQTT documents enter through COPY, just as they do with Telegraf's PostgreSQL
// output. Each test uses fresh IDs and leaves unrelated database data untouched.
func TestMQTTIngestion(t *testing.T) {
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
	if err := s.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	prefix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()))
	next := 0
	value := func() sensor.Value {
		next++
		n := 0.0
		return sensor.Value{ID: fmt.Sprintf("%s-0000-4000-8000-%012d", prefix, next), SensorID: "mqtt-quality", SensorType: "water-level", Timestamp: time.Date(2026, 9, 14, 10, 0, 0, 123456000, time.UTC), Value: &n, Unit: "m", Metadata: map[string]json.RawMessage{"raw": json.RawMessage(`{"rssi":-70,"nested":[1,null,"x"]}`)}}
	}
	encode := func(v any) string {
		t.Helper()
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		return string(b)
	}
	copyDocument := func(body string) error {
		_, err := s.pool.CopyFrom(ctx, pgx.Identifier{"sensor", "mqtt_ingest"}, []string{"time", "value"}, pgx.CopyFromRows([][]any{{time.Now(), body}}))
		return err
	}
	ingest := func(body string) {
		t.Helper()
		if err := copyDocument(body); err != nil {
			t.Fatal(err)
		}
	}
	assertRejected := func(body string) {
		t.Helper()
		ingest(body)
		var count int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.rejected_messages WHERE payload=$1`, body).Scan(&count); err != nil || count < 1 {
			t.Fatalf("missing quarantine record: %d %v", count, err)
		}
	}
	t.Run("MQTT batch then Go retry preserves identity and metadata", func(t *testing.T) {
		a, b := value(), value()
		b.Timestamp = time.Date(2026, 9, 14, 10, 0, 10, 0, time.UTC)
		b.Metadata = nil
		ingest(encode([]sensor.Value{a, b}))
		got, err := s.Get(ctx, a.ID)
		if err != nil || !got.Timestamp.Equal(a.Timestamp) || *got.Value.Value != 0 {
			t.Fatalf("round trip: %+v %v", got, err)
		}
		var metadata any
		if err := json.Unmarshal(got.Metadata["raw"], &metadata); err != nil {
			t.Fatal(err)
		}
		if metadata.(map[string]any)["nested"].([]any)[1] != nil {
			t.Fatal("nested JSON null lost")
		}
		replayed, n, err := s.Put(ctx, []sensor.Value{a, b})
		if err != nil || n != 0 || replayed[0].Sequence != got.Sequence {
			t.Fatalf("cross-path retry: %d %v", n, err)
		}
		ingest(encode([]sensor.Value{a, b}))
		var count int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.measurements WHERE id=$1`, a.ID).Scan(&count); err != nil || count != 1 {
			t.Fatalf("duplicate measurement: %d %v", count, err)
		}
	})
	t.Run("Go then MQTT normalizes timezones and optional nulls", func(t *testing.T) {
		a := value()
		a.Metadata = nil
		if _, _, err := s.Put(ctx, []sensor.Value{a}); err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		if err := json.Unmarshal([]byte(encode(a)), &body); err != nil {
			t.Fatal(err)
		}
		body["timestamp"] = "2026-09-14T12:00:00.123456+02:00"
		body["metadata"] = nil
		body["gateway_id"] = nil
		document := encode(body)
		ingest(document)
		var count int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.rejected_messages WHERE payload=$1`, document).Scan(&count); err != nil || count != 0 {
			t.Fatalf("valid retry quarantined: %d %v", count, err)
		}
	})
	t.Run("invalid final item and conflict roll back the entire document", func(t *testing.T) {
		a, existing := value(), value()
		if _, _, err := s.Put(ctx, []sensor.Value{existing}); err != nil {
			t.Fatal(err)
		}
		changed := existing
		different := 1.0
		changed.Value = &different
		for _, body := range []string{encode([]any{a, map[string]any{"id": value().ID}}), encode([]sensor.Value{a, changed}), encode([]sensor.Value{a, a}), `{"bad":"` + prefix + `"}`} {
			assertRejected(body)
			if _, err := s.Get(ctx, a.ID); !errors.Is(err, sensor.ErrNotFound) {
				t.Fatalf("partial commit: %v", err)
			}
		}
	})
	t.Run("concurrent Go and MQTT retries commit once", func(t *testing.T) {
		a := value()
		body := encode(a)
		var wg sync.WaitGroup
		for i := 0; i < 16; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				if i%2 == 0 {
					if err := copyDocument(body); err != nil {
						t.Error(err)
					}
				} else {
					if _, _, err := s.Put(ctx, []sensor.Value{a}); err != nil {
						t.Error(err)
					}
				}
			}(i)
		}
		wg.Wait()
		var count int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.measurements WHERE id=$1`, a.ID).Scan(&count); err != nil || count != 1 {
			t.Fatalf("concurrent duplicate: %d %v", count, err)
		}
	})
	t.Run("insert-only role can COPY without access to stored observations", func(t *testing.T) {
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(context.Background())
		// The role and grants are transaction-local and disappear on rollback.
		role := "mqtt_test_" + prefix
		if _, err := tx.Exec(ctx, `CREATE ROLE `+role+`; GRANT USAGE ON SCHEMA sensor TO `+role+`; GRANT INSERT ON sensor.mqtt_ingest TO `+role+`; SET LOCAL ROLE `+role); err != nil {
			t.Fatal(err)
		}
		a := value()
		if _, err := tx.CopyFrom(ctx, pgx.Identifier{"sensor", "mqtt_ingest"}, []string{"time", "value"}, pgx.CopyFromRows([][]any{{time.Now(), encode(a)}})); err != nil {
			t.Fatal(err)
		}
		var canRead, canWrite bool
		if err := tx.QueryRow(ctx, `SELECT has_table_privilege(current_user,'sensor.events','SELECT'),has_table_privilege(current_user,'sensor.measurements','INSERT')`).Scan(&canRead, &canWrite); err != nil || canRead || canWrite {
			t.Fatalf("excess privileges: %v %v %v", canRead, canWrite, err)
		}
		if _, err := tx.Exec(ctx, `RESET ROLE`); err != nil {
			t.Fatal(err)
		}
		var count int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM sensor.measurements WHERE id=$1`, a.ID).Scan(&count); err != nil || count != 1 {
			t.Fatalf("restricted COPY failed: %d %v", count, err)
		}
	})
	var staged int
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.mqtt_ingest`).Scan(&staged); err != nil || staged != 0 {
		t.Fatalf("adapter accumulated messages: %d %v", staged, err)
	}
}
