package sensor

import (
	"encoding/json"
	"testing"
	"time"
)

func TestMeasurementRange(t *testing.T) {
	for _, tc := range []struct {
		metadata string
		valid    bool
	}{
		{`{}`, true}, {`{"deviation":0.1}`, true},
		{`{"minimum":1.1,"maximum":1.9}`, true}, {`{"minimum":1.5,"maximum":1.5}`, true},
		{`{"minimum":1}`, false}, {`{"maximum":2}`, false},
		{`{"minimum":null,"maximum":2}`, false}, {`{"minimum":"1","maximum":2}`, false},
		{`{"minimum":2,"maximum":1}`, false}, {`{"minimum":1.6,"maximum":2}`, false},
		{`{"minimum":1,"maximum":1.4}`, false}, {`{"minimum":1,"maximum":1e400}`, false},
	} {
		t.Run(tc.metadata, func(t *testing.T) {
			value := 1.5
			v := Value{ID: "00000000-0000-4000-8000-000000000001", SensorID: "range", SensorType: "water-level", Timestamp: time.Date(2026, 9, 15, 0, 0, 0, 0, time.UTC), Value: &value, Unit: "m"}
			if err := json.Unmarshal([]byte(tc.metadata), &v.Metadata); err != nil {
				t.Fatal(err)
			}
			if err := v.Normalize(); (err == nil) != tc.valid {
				t.Fatalf("valid=%v, error=%v", tc.valid, err)
			}
		})
	}
}
