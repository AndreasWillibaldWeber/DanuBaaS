package alerts

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRuleThresholds(t *testing.T) {
	pointer := func(v float64) *float64 { return &v }
	valid := Rule{ID: "river", SensorID: "river", Enabled: true, LevelWarning: pointer(0), LevelCritical: pointer(3), RiseWarning: pointer(.1), RiseCritical: pointer(.2), RisePeriodSeconds: 300, RiseWindowSeconds: 600, RiseMinSeconds: 30, StaleSeconds: 900}
	if err := valid.Validate(); err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*Rule){
		"missing threshold":          func(r *Rule) { r.LevelWarning = nil },
		"reversed levels":            func(r *Rule) { r.LevelWarning = pointer(4) },
		"reversed rise":              func(r *Rule) { r.RiseCritical = pointer(.01) },
		"zero rate":                  func(r *Rule) { r.RiseWarning = pointer(0) },
		"zero period":                func(r *Rule) { r.RisePeriodSeconds = 0 },
		"short window":               func(r *Rule) { r.RiseWindowSeconds = 30 },
		"excess hysteresis":          func(r *Rule) { r.RiseHysteresis = .1 },
		"hold longer than freshness": func(r *Rule) { r.HoldSeconds = 901 },
	} {
		t.Run(name, func(t *testing.T) {
			r := valid
			change(&r)
			if r.Validate() == nil {
				t.Fatal("invalid parameters accepted")
			}
		})
	}
}

func TestRuleDecodingRejectsAmbiguousParameters(t *testing.T) {
	pointer := func(v float64) *float64 { return &v }
	r := Rule{ID: "r", SensorID: "s", LevelWarning: pointer(0), LevelCritical: pointer(3), RiseWarning: pointer(.1), RiseCritical: pointer(.2), RisePeriodSeconds: 300, RiseWindowSeconds: 600, RiseMinSeconds: 30, StaleSeconds: 900}
	raw, _ := json.Marshal(r)
	for _, body := range []string{
		strings.Replace(string(raw), `"enabled":false,`, "", 1),
		strings.Replace(string(raw), `"enabled":false`, `"enabled":null`, 1),
		strings.Replace(string(raw), `"enabled":false`, `"enabled":false,"enabled":true`, 1),
		strings.Replace(string(raw), `"enabled":false`, `"enabled":false,"sql":"SELECT 1"`, 1),
	} {
		var out Rule
		if json.Unmarshal([]byte(body), &out) == nil {
			t.Fatal("ambiguous parameters accepted")
		}
	}
}
