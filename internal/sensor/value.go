// Package sensor defines the ingestion contract independently of HTTP and storage.
package sensor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strings"
	"time"
)

// MaxObservationBytes bounds memory when a list contains the maximum number of records.
const MaxObservationBytes = 16 << 10

var (
	ErrNotFound = errors.New("value not found")
	ErrConflict = errors.New("event ID already exists with different content")
	uuid        = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	identifier  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)
)

// Value is one immutable observation. ID is a client-generated UUID retained on retry.
// Metadata preserves source-specific fields, such as RSSI, deviation, and reference height.
type Value struct {
	ID         string                     `json:"id"`
	SensorID   string                     `json:"sensor_id"`
	GatewayID  string                     `json:"gateway_id,omitempty"`
	SensorType string                     `json:"sensor_type"`
	Timestamp  time.Time                  `json:"timestamp"`
	Value      *float64                   `json:"value"`
	Unit       string                     `json:"unit"`
	Metadata   map[string]json.RawMessage `json:"metadata,omitempty"`
}

func ValidID(id string) bool { return uuid.MatchString(id) }

// Normalize validates without inventing timestamps or replacing missing values with zero.
func (v *Value) Normalize() error {
	if !ValidID(v.ID) {
		return errors.New("id must be a lowercase UUID")
	}
	if !identifier.MatchString(v.SensorID) {
		return errors.New("invalid sensor_id")
	}
	if v.GatewayID != "" && !identifier.MatchString(v.GatewayID) {
		return errors.New("invalid gateway_id")
	}
	if !identifier.MatchString(v.SensorType) {
		return errors.New("invalid sensor_type")
	}
	if v.Timestamp.IsZero() || v.Timestamp.Year() < 1970 || v.Timestamp.Year() > 9999 {
		return errors.New("timestamp must be RFC3339 with a year between 1970 and 9999")
	}
	// PostgreSQL stores microseconds. Reject loss of precision rather than changing identity on retry.
	if v.Timestamp.Nanosecond()%1000 != 0 {
		return errors.New("timestamp supports at most microsecond precision")
	}
	v.Timestamp = v.Timestamp.UTC()
	if v.Value == nil || math.IsNaN(*v.Value) || math.IsInf(*v.Value, 0) {
		return errors.New("value must be a finite number")
	}
	if len(v.Unit) == 0 || len(v.Unit) > 32 || strings.TrimSpace(v.Unit) != v.Unit {
		return errors.New("unit must contain 1 to 32 bytes without surrounding whitespace")
	}
	for k, raw := range v.Metadata {
		if len(k) == 0 || len(k) > 128 || !json.Valid(raw) {
			return fmt.Errorf("invalid metadata entry")
		}
	}
	encoded, err := json.Marshal(v)
	if err != nil || len(encoded) > MaxObservationBytes {
		return errors.New("normalized observation exceeds 16 KiB")
	}
	return nil
}

type Record struct {
	Value
	Sequence   int64     `json:"sequence"`
	ReceivedAt time.Time `json:"received_at"`
}

type Store interface {
	Put(context.Context, []Value) ([]Record, int, error)
	Get(context.Context, string) (Record, error)
	List(context.Context, int64, int) ([]Record, error)
	Ping(context.Context) error
}
