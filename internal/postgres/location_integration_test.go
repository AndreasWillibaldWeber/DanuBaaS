//go:build integration

package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
	"github.com/jackc/pgx/v5"
)

// Exercise actual COPY/trigger and Go writers against the same registry and view.
func TestLocationContract(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Fatal("TEST_DATABASE_URL must name a disposable database")
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
		t.Fatal(err)
	}
	prefix := fmt.Sprintf("%08x", uint32(time.Now().UnixNano()))
	serial := 0
	value := func(fields string) string {
		serial++
		if fields != "" {
			fields = "," + fields
		}
		return fmt.Sprintf(`{"id":"%s-0000-4000-8000-%012d","sensor_id":"location-test","sensor_type":"water-level","timestamp":"2026-09-14T10:00:00Z","value":0,"unit":"m"%s}`, prefix, serial, fields)
	}
	decode := func(t *testing.T, raw string) sensor.Value {
		t.Helper()
		var v sensor.Value
		if err := json.Unmarshal([]byte(raw), &v); err != nil {
			t.Fatal(err)
		}
		if err := v.Normalize(); err != nil {
			t.Fatal(err)
		}
		return v
	}
	copyDoc := func(t *testing.T, raw string, rejected bool) {
		t.Helper()
		var before, after int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.rejected_messages WHERE payload=$1`, raw).Scan(&before); err != nil {
			t.Fatal(err)
		}
		if _, err := s.pool.CopyFrom(ctx, pgx.Identifier{"sensor", "mqtt_ingest"}, []string{"time", "value"}, pgx.CopyFromRows([][]any{{time.Now(), raw}})); err != nil {
			t.Fatal(err)
		}
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM sensor.rejected_messages WHERE payload=$1`, raw).Scan(&after); err != nil {
			t.Fatal(err)
		}
		want := before
		if rejected {
			want++
		}
		if after != want {
			t.Fatalf("quarantine=%d want %d for %s", after, want, raw)
		}
	}
	check := func(t *testing.T, v sensor.Value) {
		t.Helper()
		got, err := s.Get(ctx, v.ID)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got.LonLat, v.LonLat) || !reflect.DeepEqual(got.LocationID, v.LocationID) {
			t.Fatalf("GET location changed: %+v", got)
		}
		page, err := s.List(ctx, got.Sequence-1, 1)
		if err != nil || len(page) != 1 || !reflect.DeepEqual(page[0], got) {
			t.Fatalf("list: %+v %v", page, err)
		}
		var lon, lat *float64
		var id *int64
		if err := s.pool.QueryRow(ctx, `SELECT longitude,latitude,location_id FROM sensor.measurements WHERE id=$1`, v.ID).Scan(&lon, &lat, &id); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(id, v.LocationID) {
			t.Fatalf("stored location ID=%v want %v", id, v.LocationID)
		}
		if v.LonLat == nil {
			if lon != nil || lat != nil {
				t.Fatal("coordinates leaked from another batch item")
			}
		} else if lon == nil || lat == nil || *lon != v.LonLat[0] || *lat != v.LonLat[1] {
			t.Fatal("stored coordinate order/value changed")
		}
		var visible bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM sensor.dashboard_values WHERE sensor_id=$1 AND time=$2 AND longitude IS NOT DISTINCT FROM $3::float8 AND latitude IS NOT DISTINCT FROM $4::float8 AND location_id IS NOT DISTINCT FROM $5::bigint)`, v.SensorID, v.Timestamp, lon, lat, id).Scan(&visible); err != nil || !visible {
			t.Fatalf("reporting view: %t %v", visible, err)
		}
	}
	for _, route := range []string{"api", "mqtt"} {
		t.Run(route, func(t *testing.T) {
			fields := []string{`"lon_lat":[16.3738,48.2082],"location_id":7`, `"lon_lat":null,"location_id":null`, "", `"lon_lat":[0,0],"location_id":0`, `"location_id":7`, `"lon_lat":[180,90]`, `"lon_lat":[-180,-90],"location_id":9007199254740991`}
			raw := make([]json.RawMessage, len(fields))
			values := make([]sensor.Value, len(fields))
			for i, f := range fields {
				body := value(f)
				raw[i] = json.RawMessage(body)
				values[i] = decode(t, body)
			}
			batch, err := json.Marshal(raw)
			if err != nil {
				t.Fatal(err)
			}
			if route == "mqtt" {
				copyDoc(t, string(batch), false)
			} else {
				if _, n, err := s.Put(ctx, values); err != nil || n != len(values) {
					t.Fatalf("Go write: %d %v", n, err)
				}
			}
			for _, v := range values {
				check(t, v)
			}
			// Replays cross routes; canonical Go JSON omits the explicit null fields.
			if _, n, err := s.Put(ctx, values); err != nil || n != 0 {
				t.Fatalf("cross-route retry: %d %v", n, err)
			}
			copyDoc(t, string(batch), false)
			canonical, err := json.Marshal(values)
			if err != nil {
				t.Fatal(err)
			}
			copyDoc(t, string(canonical), false)
			for _, change := range []string{"coordinate", "id", "remove"} {
				changed := values[0]
				switch change {
				case "coordinate":
					changed.LonLat = &sensor.Coordinates{0, 0}
				case "id":
					id := int64(8)
					changed.LocationID = &id
				case "remove":
					changed.LonLat = nil
					changed.LocationID = nil
				}
				fresh := decode(t, value(""))
				conflict := []sensor.Value{fresh, changed}
				if _, _, err := s.Put(ctx, conflict); !errors.Is(err, sensor.ErrConflict) {
					t.Fatalf("Go location conflict: %v", err)
				}
				doc, err := json.Marshal(conflict)
				if err != nil {
					t.Fatal(err)
				}
				copyDoc(t, string(doc), true)
				if _, err := s.Get(ctx, fresh.ID); !errors.Is(err, sensor.ErrNotFound) {
					t.Fatal("conflicting batch partially committed")
				}
			}
		})
	}
	t.Run("invalid final location quarantines whole document", func(t *testing.T) {
		for _, f := range []string{`"lon_lat":[]`, `"lon_lat":[1]`, `"lon_lat":[1,2,3]`, `"lon_lat":[null,1]`, `"lon_lat":[1,null]`, `"lon_lat":["1",2]`, `"lon_lat":{}`, `"lon_lat":false`, `"lon_lat":[181,0]`, `"lon_lat":[-181,0]`, `"lon_lat":[0,91]`, `"lon_lat":[0,-91]`, `"lon_lat":[1e309,0]`, `"location_id":-1`, `"location_id":9007199254740992`, `"location_id":1.5`, `"location_id":"7"`, `"location_id":true`, `"location_id":[]`} {
			a, b := value(""), value(f)
			copyDoc(t, "["+a+","+b+"]", true)
			if _, err := s.Get(ctx, decode(t, a).ID); !errors.Is(err, sensor.ErrNotFound) {
				t.Fatalf("invalid location partially committed: %s", f)
			}
		}
	})
	t.Run("database constraints protect direct writes", func(t *testing.T) {
		for _, set := range []string{"longitude=1,latitude=NULL", "longitude=NULL,latitude=1", "longitude=181,latitude=0", "longitude=0,latitude=91", "longitude='NaN',latitude=0", "location_id=-1", "location_id=9007199254740992"} {
			v := decode(t, value(""))
			if _, _, err := s.Put(ctx, []sensor.Value{v}); err != nil {
				t.Fatal(err)
			}
			_, err := s.pool.Exec(ctx, `UPDATE sensor.measurements SET `+set+` WHERE id=$1`, v.ID)
			if err == nil {
				t.Fatalf("constraint allowed %s", set)
			}
		}
	})
}
