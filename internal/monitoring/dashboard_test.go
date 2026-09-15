package monitoring

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestRenderSensorBands(t *testing.T) {
	source, err := os.ReadFile("../../deploy/grafana/dashboards/sensors.json")
	if err != nil {
		t.Fatal(err)
	}
	stations := []Station{{"WL-004", "WL-004"}, {"river:upper.1", "River \"upper\""}, {"WL-005", "WL-005"}}
	rendered, err := Render(source, stations)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Panels []struct {
			ID          int
			FieldConfig struct {
				Overrides []struct {
					Matcher    struct{ Options string }
					Properties []struct {
						ID    string
						Value json.RawMessage
					}
				}
			}
		}
	}
	if err = json.Unmarshal(rendered, &doc); err != nil {
		t.Fatal(err)
	}
	found := 0
	for _, p := range doc.Panels {
		if p.ID != 1 {
			continue
		}
		if len(p.FieldConfig.Overrides) != 9 {
			t.Fatal("missing sensor fields")
		}
		for _, override := range p.FieldConfig.Overrides {
			if !strings.HasSuffix(override.Matcher.Options, ":maximum") {
				continue
			}
			id := strings.TrimSuffix(override.Matcher.Options, ":maximum")
			for _, property := range override.Properties {
				if property.ID == "custom.fillBelowTo" {
					var target string
					json.Unmarshal(property.Value, &target)
					if target != id+":minimum" {
						t.Fatal("band crosses sensors")
					}
					found++
				}
			}
		}
	}
	if found != 3 {
		t.Fatal("missing bands")
	}
	alone, err := Render(source, []Station{stations[0]})
	if err != nil {
		t.Fatal(err)
	}
	if !json.Valid(alone) {
		t.Fatal("invalid dashboard")
	}
	var isolated struct {
		Panels []struct {
			ID          int
			FieldConfig struct{ Overrides []json.RawMessage }
		}
	}
	if err = json.Unmarshal(alone, &isolated); err != nil {
		t.Fatal(err)
	}
	var full struct {
		Panels []struct {
			ID          int
			FieldConfig struct{ Overrides []json.RawMessage }
		}
	}
	if err = json.Unmarshal(rendered, &full); err != nil {
		t.Fatal(err)
	}
	for _, panel := range isolated.Panels {
		if panel.ID != 1 {
			continue
		}
		for _, override := range panel.FieldConfig.Overrides {
			matched := false
			for _, original := range full.Panels {
				if original.ID != 1 {
					continue
				}
				for _, candidate := range original.FieldConfig.Overrides {
					if string(candidate) == string(override) {
						matched = true
					}
				}
			}
			if !matched {
				t.Fatal("filtering changes field configuration")
			}
		}
	}

	// Removing or adding other stations must not change sensor colours.
	if Color("WL-004") != "orange" {
		t.Fatal("unstable colours")
	}
	repeat, err := Render(source, stations)
	if err != nil || string(repeat) != string(rendered) {
		t.Fatal("render is not deterministic")
	}
	empty, err := Render(source, nil)
	if err != nil || !json.Valid(empty) {
		t.Fatal("empty roster must render")
	}
}

func TestDuplicateLabelsRemainDistinct(t *testing.T) {
	source, err := os.ReadFile("../../deploy/grafana/dashboards/sensors.json")
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := Render(source, []Station{{"quickstart", "WL-001"}, {"WL-001", "WL-001"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, label := range []string{"WL-001 (quickstart)", "WL-001 (WL-001)"} {
		if !strings.Contains(string(rendered), label) {
			t.Fatal("ambiguous display names")
		}
	}
}
