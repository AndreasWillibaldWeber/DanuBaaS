// Package alerts defines the versioned water-level alert configuration contract.
package alerts

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"regexp"
	"time"
)

var ErrInvalid = errors.New("invalid rule change")
var ErrConflict = errors.New("rule version conflict")
var ErrNotFound = errors.New("alert resource not found")
var identifier = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

// Rates are metres per RisePeriodSeconds, measured over RiseWindowSeconds.
// Pointer thresholds distinguish an explicit zero from an omitted parameter.
type Rule struct {
	ID                string    `json:"id"`
	SensorID          string    `json:"sensor_id"`
	Enabled           bool      `json:"enabled"`
	LevelWarning      *float64  `json:"level_warning"`
	LevelCritical     *float64  `json:"level_critical"`
	RiseWarning       *float64  `json:"rise_warning"`
	RiseCritical      *float64  `json:"rise_critical"`
	RisePeriodSeconds int       `json:"rise_period_seconds"`
	RiseWindowSeconds int       `json:"rise_window_seconds"`
	RiseMinSeconds    int       `json:"rise_min_seconds"`
	LevelHysteresis   float64   `json:"level_hysteresis"`
	RiseHysteresis    float64   `json:"rise_hysteresis"`
	HoldSeconds       int       `json:"hold_seconds"`
	StaleSeconds      int       `json:"stale_seconds"`
	Version           int       `json:"version"`
	UpdatedAt         time.Time `json:"updated_at,omitempty"`
}

// Reject missing/null configuration values rather than accidentally disabling a
// rule or replacing a parameter with Go's zero value during a full replacement.
func (r *Rule) UnmarshalJSON(data []byte) error {
	type plain Rule
	fields := map[string]json.RawMessage{}
	scan := json.NewDecoder(bytes.NewReader(data))
	token, err := scan.Token()
	if err != nil || token != json.Delim('{') {
		return errors.New("expected a rule object")
	}
	for scan.More() {
		token, err = scan.Token()
		if err != nil {
			return err
		}
		key, ok := token.(string)
		if !ok {
			return errors.New("invalid rule field")
		}
		if _, exists := fields[key]; exists {
			return errors.New("duplicate rule field")
		}
		var value json.RawMessage
		if err = scan.Decode(&value); err != nil {
			return err
		}
		fields[key] = value
	}
	if _, err = scan.Token(); err != nil {
		return err
	}
	for _, name := range []string{"id", "sensor_id", "enabled", "level_warning", "level_critical", "rise_warning", "rise_critical", "rise_period_seconds", "rise_window_seconds", "rise_min_seconds", "level_hysteresis", "rise_hysteresis", "hold_seconds", "stale_seconds", "version"} {
		value, ok := fields[name]
		if !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return errors.New("all rule parameters must be explicitly supplied and non-null")
		}
	}
	var value plain
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		return err
	}
	*r = Rule(value)
	return nil
}

func (r Rule) Validate() error {
	if !identifier.MatchString(r.ID) || !identifier.MatchString(r.SensorID) {
		return errors.New("invalid rule or sensor identifier")
	}
	for _, v := range []*float64{r.LevelWarning, r.LevelCritical, r.RiseWarning, r.RiseCritical} {
		if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
			return errors.New("all four finite thresholds are required")
		}
	}
	if *r.LevelWarning >= *r.LevelCritical || *r.RiseWarning <= 0 || *r.RiseWarning >= *r.RiseCritical {
		return errors.New("warning must be below critical; rise thresholds must be positive")
	}
	if math.IsNaN(r.LevelHysteresis) || math.IsInf(r.LevelHysteresis, 0) || math.IsNaN(r.RiseHysteresis) || math.IsInf(r.RiseHysteresis, 0) || r.LevelHysteresis < 0 || r.LevelHysteresis >= *r.LevelCritical-*r.LevelWarning || r.RiseHysteresis < 0 || r.RiseHysteresis >= *r.RiseCritical-*r.RiseWarning {
		return errors.New("hysteresis must be nonnegative and smaller than the severity threshold gap")
	}
	if r.RisePeriodSeconds < 1 || r.RisePeriodSeconds > 86400 || r.RiseWindowSeconds < 2 || r.RiseWindowSeconds > 86400 || r.RiseMinSeconds < 1 || r.RiseMinSeconds >= r.RiseWindowSeconds || r.StaleSeconds < r.RiseMinSeconds || r.StaleSeconds > 604800 || r.HoldSeconds < 0 || r.HoldSeconds > r.StaleSeconds || r.Version < 0 {
		return errors.New("invalid evaluation durations or version")
	}
	return nil
}
func Decode(reader io.Reader, target any) error {
	dec := json.NewDecoder(reader)
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		return errors.New("invalid JSON or unknown fields")
	}
	if err := dec.Decode(new(any)); err != io.EOF {
		return errors.New("expected one JSON document")
	}
	return nil
}
