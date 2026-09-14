package httpapi

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/alerts"
)

type AlertStore interface {
	SaveRule(context.Context, alerts.Rule, string, bool) (json.RawMessage, error)
	AlertQuery(context.Context, string, string, int64, int, bool) (json.RawMessage, error)
	Acknowledge(context.Context, int64, string, string) error
}

// NewAlerts keeps ingestion credentials outside the administrative write boundary.
func NewAlerts(store AlertStore, readKey, adminKey string) (http.Handler, error) {
	if len(readKey) < 32 || len(adminKey) < 32 || readKey == adminKey {
		return nil, errors.New("distinct read and administrative keys of at least 32 bytes are required")
	}
	return &alertAPI{store: store, readKey: sha256.Sum256([]byte(readKey)), adminKey: sha256.Sum256([]byte(adminKey))}, nil
}

type alertAPI struct {
	store             AlertStore
	readKey, adminKey [32]byte
}

func (a *alertAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	key := sha256.Sum256([]byte(r.Header.Get("X-API-Key")))
	admin := subtle.ConstantTimeCompare(key[:], a.adminKey[:]) == 1
	if len(r.Header.Values("X-API-Key")) != 1 || (!admin && subtle.ConstantTimeCompare(key[:], a.readKey[:]) != 1) {
		problem(w, 401, "unauthorized", "a valid API key is required")
		return
	}
	if r.Method != "GET" && !admin {
		problem(w, 403, "forbidden", "the alert administrative key is required")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v1/"), "/")
	q, queryErr := url.ParseQuery(r.URL.RawQuery)
	if queryErr != nil {
		problem(w, 400, "invalid_query", "malformed query encoding")
		return
	}
	after := int64(0)
	limit := 100
	active := false
	for k, v := range q {
		if len(v) != 1 || (k != "after" && k != "limit" && k != "active") {
			problem(w, 400, "invalid_query", "unsupported or repeated query parameter")
			return
		}
	}
	if r.Method != "GET" && r.URL.RawQuery != "" {
		problem(w, 400, "invalid_query", "writes do not accept query parameters")
		return
	}
	if q.Has("limit") {
		n, err := strconv.Atoi(q.Get("limit"))
		if err != nil || n < 1 || n > 1000 {
			problem(w, 400, "invalid_query", "limit must be 1..1000")
			return
		}
		limit = n
	}
	if q.Has("active") {
		if q.Get("active") != "true" && q.Get("active") != "false" {
			problem(w, 400, "invalid_query", "active must be true or false")
			return
		}
		active = q.Get("active") == "true"
	}
	resource, id := "", ""
	if len(parts) == 1 && parts[0] == "alerting-status" {
		resource = "status"
	}
	if len(parts) > 0 && parts[0] == "alert-rules" {
		resource = "rules"
		id = q.Get("after")
		if len(parts) >= 2 {
			resource = "rule"
			id = parts[1]
		}
		if len(parts) == 3 && parts[2] == "versions" {
			resource = "versions"
		} else if len(parts) > 2 {
			resource = ""
		}
	}
	if len(parts) > 0 && parts[0] == "alerts" {
		resource = "alerts"
		if len(parts) >= 2 {
			resource = "alert"
			id = parts[1]
			n, e := strconv.ParseInt(id, 10, 64)
			if e != nil || n < 1 {
				problem(w, 400, "invalid_id", "alert ID must be a positive integer")
				return
			}
		}
		if len(parts) == 3 {
			switch parts[2] {
			case "events":
				resource = "events"
			case "acknowledgments":
				resource = "ack"
			default:
				resource = ""
			}
		} else if len(parts) > 3 {
			resource = ""
		}
	}
	if resource == "" {
		problem(w, 404, "not_found", "resource not found")
		return
	}
	for k := range q {
		allowed := (k == "limit" || k == "after") && (resource == "rules" || resource == "versions" || resource == "alerts" || resource == "events") || k == "active" && resource == "alerts"
		if !allowed {
			problem(w, 400, "invalid_query", "query parameter is not supported on this resource")
			return
		}
	}
	if q.Has("after") && resource != "rules" {
		n, e := strconv.ParseInt(q.Get("after"), 10, 64)
		if e != nil || n < 0 {
			problem(w, 400, "invalid_query", "after must be nonnegative")
			return
		}
		after = n
	}
	if r.Method == "GET" && resource != "ack" {
		out, err := a.store.AlertQuery(ctx, resource, id, after, limit, active)
		if err != nil {
			a.failure(w, err)
			return
		}
		status := 200
		if resource == "status" {
			var h struct {
				Healthy bool `json:"healthy"`
			}
			_ = json.Unmarshal(out, &h)
			if !h.Healthy {
				status = 503
			}
		}
		respond(w, status, out)
		return
	}
	if (resource == "rules" && r.Method == "POST") || (resource == "rule" && r.Method == "PUT") {
		var rule alerts.Rule
		if !decodeAlertBody(w, r, &rule) {
			return
		}
		if (resource == "rule" && rule.ID != id) || (resource == "rules" && rule.Version != 0) || (resource == "rule" && rule.Version < 1) || !rule.UpdatedAt.IsZero() {
			problem(w, 422, "invalid_rule", "ID/version must match the operation; updated_at is server-managed")
			return
		}
		if err := rule.Validate(); err != nil {
			problem(w, 422, "invalid_rule", err.Error())
			return
		}
		out, err := a.store.SaveRule(ctx, rule, "alert-admin", false)
		if err != nil {
			a.failure(w, err)
			return
		}
		status := 200
		if resource == "rules" {
			status = 201
			w.Header().Set("Location", "/api/v1/alert-rules/"+rule.ID)
		}
		respond(w, status, out)
		return
	}
	if resource == "ack" && r.Method == "POST" {
		var body struct {
			Note string `json:"note"`
		}
		if !decodeAlertBody(w, r, &body) {
			return
		}
		if len(body.Note) > 2000 {
			problem(w, 422, "invalid_acknowledgment", "note exceeds 2000 bytes")
			return
		}
		n, _ := strconv.ParseInt(id, 10, 64)
		if err := a.store.Acknowledge(ctx, n, "alert-admin", body.Note); err != nil {
			a.failure(w, err)
			return
		}
		respond(w, 200, map[string]bool{"acknowledged": true})
		return
	}
	allow := "GET"
	switch resource {
	case "rules":
		allow = "GET, POST"
	case "rule":
		allow = "GET, PUT"
	case "ack":
		allow = "POST"
	}
	w.Header().Set("Allow", allow)
	problem(w, 405, "method_not_allowed", "method not supported")
}
func decodeAlertBody(w http.ResponseWriter, r *http.Request, target any) bool {
	if strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0]) != "application/json" {
		problem(w, 415, "unsupported_media_type", "Content-Type must be application/json")
		return false
	}
	if r.Header.Get("Content-Encoding") != "" {
		problem(w, 415, "unsupported_encoding", "encoded request bodies are not supported")
		return false
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 16<<10))
	if err != nil {
		var max *http.MaxBytesError
		if errors.As(err, &max) {
			problem(w, 413, "body_too_large", "body exceeds 16 KiB")
		} else {
			problem(w, 400, "invalid_json", "invalid request body")
		}
		return false
	}
	if err = uniqueKeys(data); err != nil {
		problem(w, 400, "invalid_json", "duplicate JSON fields")
		return false
	}
	if len(strings.TrimSpace(string(data))) == 0 || strings.TrimSpace(string(data))[0] != '{' {
		problem(w, 400, "invalid_json", "expected a JSON object")
		return false
	}
	if err = alerts.Decode(strings.NewReader(string(data)), target); err != nil {
		problem(w, 400, "invalid_json", err.Error())
		return false
	}
	return true
}
func (a *alertAPI) failure(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, alerts.ErrInvalid):
		problem(w, 422, "invalid_rule", "rule identity is immutable and parameters must satisfy database constraints")
	case errors.Is(err, alerts.ErrNotFound):
		problem(w, 404, "not_found", "resource not found")
	case errors.Is(err, alerts.ErrConflict):
		problem(w, 409, "conflict", "rule already exists, sensor is configured, or version changed")
	default:
		slog.Error("alert operation failed", "error", err)
		problem(w, 500, "internal_error", "alert operation failed")
	}
}
