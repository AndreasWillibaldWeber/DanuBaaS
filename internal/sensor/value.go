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

// MaxLocationID is the largest integer represented exactly by Node-RED's JSON numbers.
const MaxLocationID int64 = 9007199254740991

// Coordinates stores WGS84 decimal degrees in [longitude, latitude] order.
// Decode explicitly: encoding/json otherwise pads or truncates fixed-size arrays.
type Coordinates [2]float64

func (c *Coordinates) UnmarshalJSON(data []byte) error {
	var values []*float64
	if err := json.Unmarshal(data, &values); err != nil {
		return err
	}
	if len(values) != 2 || values[0] == nil || values[1] == nil {
		return errors.New("lon_lat must contain exactly two numbers")
	}
	*c = Coordinates{*values[0], *values[1]}
	return nil
}

// Value is one immutable observation. ID is a client-generated UUID retained on retry.
// Metadata preserves source-specific fields, such as RSSI, deviation, and reference height.
type Value struct {
	ID         string    `json:"id"`
	SensorID   string    `json:"sensor_id"`
	GatewayID  string    `json:"gateway_id,omitempty"`
	SensorType string    `json:"sensor_type"`
	Timestamp  time.Time `json:"timestamp"`
	Value      *float64  `json:"value"`
	Unit       string    `json:"unit"`
	// Optional location fields are omitted from canonical JSON when absent or null.
	LonLat     *Coordinates               `json:"lon_lat,omitempty"`
	LocationID *int64                     `json:"location_id,omitempty"`
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
	if v.LonLat != nil {
		lon, lat := v.LonLat[0], v.LonLat[1]
		if math.IsNaN(lon) || math.IsNaN(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90 {
			return errors.New("lon_lat must contain finite longitude [-180,180] and latitude [-90,90]")
		}
	}
	if v.LocationID != nil && (*v.LocationID < 0 || *v.LocationID > MaxLocationID) {
		return errors.New("location_id must be an integer between 0 and 9007199254740991")
	}
	for k, raw := range v.Metadata {
		if len(k) == 0 || len(k) > 128 || !json.Valid(raw) {
			return fmt.Errorf("invalid metadata entry")
		}
	}
	minimum, hasMinimum := v.Metadata["minimum"]
	maximum, hasMaximum := v.Metadata["maximum"]
	if hasMinimum || hasMaximum {
		var low, high *float64
		if !hasMinimum || !hasMaximum || json.Unmarshal(minimum, &low) != nil || json.Unmarshal(maximum, &high) != nil || low == nil || high == nil || math.IsInf(*low, 0) || math.IsInf(*high, 0) || *low > *v.Value || *high < *v.Value {
			return errors.New("metadata minimum and maximum must be finite numbers with minimum <= value <= maximum")
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
