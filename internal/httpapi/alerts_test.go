package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/alerts"
)

const alertAdminKey = "administrative-key-0123456789abcdef"
const ruleJSON = `{"id":"river","sensor_id":"river","enabled":true,"level_warning":2,"level_critical":3,"rise_warning":0.1,"rise_critical":0.2,"rise_period_seconds":300,"rise_window_seconds":600,"rise_min_seconds":30,"level_hysteresis":0.05,"rise_hysteresis":0.02,"hold_seconds":0,"stale_seconds":900,"version":0}`

type alertMemory struct {
	calls   int
	err     error
	healthy bool
}

func (s *alertMemory) SaveRule(_ context.Context, r alerts.Rule, actor string, _ bool) (json.RawMessage, error) {
	s.calls++
	if actor != "alert-admin" {
		panic("untrusted actor")
	}
	v, _ := json.Marshal(r)
	return v, s.err
}
func (s *alertMemory) AlertQuery(_ context.Context, resource, id string, after int64, limit int, active bool) (json.RawMessage, error) {
	s.calls++
	if resource == "status" {
		if s.healthy {
			return json.RawMessage(`{"healthy":true}`), nil
		}
		return json.RawMessage(`{"healthy":false}`), nil
	}
	return json.RawMessage(`[]`), s.err
}
func (s *alertMemory) Acknowledge(context.Context, int64, string, string) error {
	s.calls++
	return s.err
}
func TestAlertRoutes(t *testing.T) {
	for _, tc := range []struct {
		name, method, path, key, body string
		status, calls                 int
	}{
		{"missing key", "GET", "/api/v1/alert-rules", "", "", 401, 0},
		{"ingestion key cannot configure", "POST", "/api/v1/alert-rules", testKey, ruleJSON, 403, 0},
		{"read rules", "GET", "/api/v1/alert-rules", testKey, "", 200, 1},
		{"create", "POST", "/api/v1/alert-rules", alertAdminKey, ruleJSON, 201, 1},
		{"missing thresholds", "POST", "/api/v1/alert-rules", alertAdminKey, `{"id":"r","sensor_id":"r"}`, 400, 0},
		{"duplicate field", "POST", "/api/v1/alert-rules", alertAdminKey, `{"id":"r","id":"s"}`, 400, 0},
		{"unknown field", "POST", "/api/v1/alert-rules", alertAdminKey, `{"sql":"SELECT 1"}`, 400, 0},
		{"trailing document", "POST", "/api/v1/alert-rules", alertAdminKey, ruleJSON + `{}`, 400, 0},
		{"null", "POST", "/api/v1/alert-rules", alertAdminKey, `null`, 400, 0},
		{"oversized", "POST", "/api/v1/alert-rules", alertAdminKey, strings.Repeat(" ", 17000), 413, 0},
		{"update version required", "PUT", "/api/v1/alert-rules/river", alertAdminKey, ruleJSON, 422, 0},
		{"write query", "POST", "/api/v1/alert-rules?limit=1", alertAdminKey, ruleJSON, 400, 0},
		{"negative cursor", "GET", "/api/v1/alerts?after=-1", testKey, "", 400, 0},
		{"bad id", "GET", "/api/v1/alerts/x", testKey, "", 400, 0},
		{"invalid boolean", "GET", "/api/v1/alerts?active=yes", testKey, "", 400, 0},
		{"acknowledge", "POST", "/api/v1/alerts/1/acknowledgments", alertAdminKey, `{"note":"Investigating"}`, 200, 1},
		{"forged actor", "POST", "/api/v1/alerts/1/acknowledgments", alertAdminKey, `{"actor":"somebody"}`, 400, 0},
		{"unhealthy evaluator", "GET", "/api/v1/alerting-status", testKey, "", 503, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &alertMemory{}
			h, err := NewAlerts(s, testKey, alertAdminKey)
			if err != nil {
				t.Fatal(err)
			}
			r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
			r.Header.Set("Content-Type", "application/json")
			r.Header.Set("X-API-Key", tc.key)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != tc.status || s.calls != tc.calls {
				t.Fatalf("status %d calls %d: %s", w.Code, s.calls, w.Body)
			}
		})
	}
}
func TestAlertErrorsAndKeyIsolation(t *testing.T) {
	if _, err := NewAlerts(&alertMemory{}, testKey, testKey); err == nil {
		t.Fatal("reused credential accepted")
	}
	for _, tc := range []struct {
		err    error
		status int
	}{{alerts.ErrConflict, 409}, {alerts.ErrNotFound, 404}} {
		s := &alertMemory{err: tc.err}
		h, _ := NewAlerts(s, testKey, alertAdminKey)
		r := httptest.NewRequest(http.MethodPost, "/api/v1/alert-rules", strings.NewReader(ruleJSON))
		r.Header.Set("X-API-Key", alertAdminKey)
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatal(w.Code)
		}
	}
}
