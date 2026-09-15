// Package monitoring renders native Grafana band overrides for discovered sensors.
package monitoring

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"hash/fnv"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Station struct{ ID, Label string }

// Color is stable when a sensor is selected alone or other sensors are added.
func Color(id string) string {
	known := map[string]string{"quickstart": "green", "WL-002": "yellow", "WL-003": "blue", "WL-004": "orange", "WL-005": "red"}
	if color, ok := known[id]; ok {
		return color
	}
	palette := []string{"green", "yellow", "blue", "orange", "red", "purple", "cyan"}
	h := fnv.New32a()
	_, _ = h.Write([]byte(id))
	return palette[int(h.Sum32())%len(palette)]
}

func Render(source []byte, stations []Station) ([]byte, error) {
	var doc map[string]any
	if err := json.Unmarshal(source, &doc); err != nil {
		return nil, err
	}
	panels, ok := doc["panels"].([]any)
	if !ok {
		return nil, errors.New("missing panels")
	}
	stations = append([]Station(nil), stations...)
	labels := map[string]int{}
	for _, station := range stations {
		labels[station.Label]++
	}
	sort.Slice(stations, func(i, j int) bool { return stations[i].ID < stations[j].ID })
	found := false
	for _, entry := range panels {
		p, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		if p["id"] != float64(1) {
			continue
		}
		fc, ok := p["fieldConfig"].(map[string]any)
		if !ok {
			return nil, errors.New("missing chart configuration")
		}
		overrides := []any{}
		for _, station := range stations {
			for _, kind := range []string{"value", "minimum", "maximum"} {
				name := station.Label
				if labels[name] > 1 {
					name += " (" + station.ID + ")"
				}
				if kind != "value" {
					name += " · " + kind
				}
				properties := []any{map[string]any{"id": "displayName", "value": name}, map[string]any{"id": "color", "value": map[string]any{"mode": "fixed", "fixedColor": Color(station.ID)}}}
				if kind != "value" {
					properties = append(properties, map[string]any{"id": "custom.lineWidth", "value": 0}, map[string]any{"id": "custom.hideFrom", "value": map[string]bool{"legend": true, "tooltip": false, "viz": false}})
				}
				if kind == "maximum" {
					properties = append(properties, map[string]any{"id": "custom.fillBelowTo", "value": station.ID + ":minimum"}, map[string]any{"id": "custom.fillOpacity", "value": 18})
				}
				overrides = append(overrides, map[string]any{"matcher": map[string]any{"id": "byName", "options": station.ID + ":" + kind}, "properties": properties})
			}
		}
		fc["overrides"] = overrides
		found = true
	}
	if !found {
		return nil, errors.New("missing water-level chart")
	}
	rendered, err := json.MarshalIndent(doc, "", "  ")
	return append(rendered, '\n'), err
}

func queryFrom(source []byte) (string, error) {
	var doc struct {
		Templating struct {
			List []struct{ Name, Query string }
		}
	}
	if err := json.Unmarshal(source, &doc); err != nil {
		return "", err
	}
	for _, v := range doc.Templating.List {
		if v.Name == "sensor" && v.Query != "" {
			return v.Query, nil
		}
	}
	return "", errors.New("missing sensor query")
}

func synchronize(ctx context.Context, pool *pgxpool.Pool, sourcePath, outputPath string) error {
	source, err := os.ReadFile(sourcePath)
	if err != nil {
		return err
	}
	query, err := queryFrom(source)
	if err != nil {
		return err
	}
	rows, err := pool.Query(ctx, query)
	if err != nil {
		return err
	}
	defer rows.Close()
	stations := []Station{}
	for rows.Next() {
		var s Station
		if err := rows.Scan(&s.Label, &s.ID); err != nil {
			return err
		}
		stations = append(stations, s)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	rendered, err := Render(source, stations)
	if err != nil {
		return err
	}
	existing, _ := os.ReadFile(outputPath)
	if !bytes.Equal(existing, rendered) {
		temporary, err := os.CreateTemp(filepath.Dir(outputPath), ".dashboard-*")
		if err != nil {
			return err
		}
		name := temporary.Name()
		defer os.Remove(name)
		if _, err = temporary.Write(rendered); err != nil {
			temporary.Close()
			return err
		}
		if err = temporary.Chmod(0644); err != nil {
			temporary.Close()
			return err
		}
		if err = temporary.Close(); err != nil {
			return err
		}
		if err = os.Rename(name, outputPath); err != nil {
			return err
		}
		slog.Info("updated Grafana sensor bands", "sensors", len(stations))
	}
	return os.WriteFile(filepath.Join(filepath.Dir(outputPath), ".healthy"), []byte(time.Now().UTC().Format(time.RFC3339)), 0644)
}

// Run uses the existing read-only Grafana database role. Failed queries retain the
// last good dashboard; only successful polls refresh the health heartbeat.
func Run(ctx context.Context, dsn, sourcePath, outputPath string) error {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return errors.New("invalid dashboard database configuration")
	}
	defer pool.Close()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		poll, cancel := context.WithTimeout(ctx, 5*time.Second)
		err := synchronize(poll, pool, sourcePath, outputPath)
		cancel()
		if err != nil {
			slog.Error("Grafana dashboard synchronization failed; retaining last successful dashboard")
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}
