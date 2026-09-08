// Package httpapi implements the single public sensor resource.
package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"strconv"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
)

const Path = "/api/v1/values"
const MaxBody = 1 << 20
const MaxBatch = 1000
const MaxPage = 1000

type API struct {
	store sensor.Store
	key   [32]byte
	log   *slog.Logger
}

func New(store sensor.Store, key string, log *slog.Logger) (http.Handler, error) {
	if len(key) < 32 {
		return nil, errors.New("API key must contain at least 32 bytes")
	}
	if log == nil {
		log = slog.Default()
	}
	return &API{store: store, key: sha256.Sum256([]byte(key)), log: log}, nil
}

func (a *API) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	supplied := sha256.Sum256([]byte(r.Header.Get("X-API-Key")))
	if len(r.Header.Values("X-API-Key")) != 1 || subtle.ConstantTimeCompare(supplied[:], a.key[:]) != 1 {
		problem(w, 401, "unauthorized", "a valid X-API-Key header is required")
		return
	}
	if r.URL.Path != Path {
		problem(w, 404, "not_found", "resource not found")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	r = r.WithContext(ctx)
	switch r.Method {
	case http.MethodPost:
		a.post(w, r)
	case http.MethodGet:
		a.get(w, r)
	default:
		w.Header().Set("Allow", "GET, POST")
		problem(w, 405, "method_not_allowed", "only GET and POST are supported")
	}
}

func (a *API) post(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" {
		problem(w, 400, "invalid_query", "POST does not accept query parameters")
		return
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		problem(w, 415, "unsupported_media_type", "Content-Type must be application/json")
		return
	}
	if r.Header.Get("Content-Encoding") != "" {
		problem(w, 415, "unsupported_encoding", "encoded request bodies are not supported")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBody))
	if err != nil {
		var limit *http.MaxBytesError
		if errors.As(err, &limit) {
			problem(w, 413, "body_too_large", "body exceeds 1 MiB")
		} else {
			problem(w, 400, "invalid_body", "could not read request body")
		}
		return
	}
	body = bytes.TrimSpace(body)
	if len(body) == 0 || (body[0] != '{' && body[0] != '[') {
		problem(w, 400, "invalid_json", "expected an object or array of objects")
		return
	}
	if err = uniqueKeys(body); err != nil {
		problem(w, 400, "invalid_json", err.Error())
		return
	}
	batch := body[0] == '['
	var values []sensor.Value
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if batch {
		err = dec.Decode(&values)
	} else {
		var v sensor.Value
		err = dec.Decode(&v)
		values = []sensor.Value{v}
	}
	if err != nil {
		problem(w, 400, "invalid_json", "body contains malformed JSON, unknown fields, or incorrect field types")
		return
	}
	if len(values) == 0 || len(values) > MaxBatch {
		problem(w, 422, "invalid_batch", "batch must contain between 1 and 1000 values")
		return
	}
	seen := make(map[string]bool, len(values))
	for i := range values {
		if err = values[i].Normalize(); err != nil {
			problem(w, 422, "invalid_value", fmt.Sprintf("item %d: %s", i, err))
			return
		}
		if seen[values[i].ID] {
			problem(w, 422, "duplicate_id", "IDs must be unique within a batch")
			return
		}
		seen[values[i].ID] = true
	}
	records, created, err := a.store.Put(r.Context(), values)
	if err != nil {
		a.storeError(w, err)
		return
	}
	status := http.StatusOK
	if created > 0 {
		status = http.StatusCreated
	}
	if batch {
		respond(w, status, records)
	} else {
		w.Header().Set("Location", Path+"?id="+records[0].ID)
		respond(w, status, records[0])
	}
}

func (a *API) get(w http.ResponseWriter, r *http.Request) {
	q, err := parseQuery(r)
	if err != nil {
		problem(w, 400, "invalid_query", err.Error())
		return
	}
	if q.id != "" {
		record, err := a.store.Get(r.Context(), q.id)
		if err != nil {
			a.storeError(w, err)
			return
		}
		respond(w, 200, record)
		return
	}
	records, err := a.store.List(r.Context(), q.after, q.limit+1)
	if err != nil {
		a.storeError(w, err)
		return
	}
	if records == nil {
		records = []sensor.Record{}
	}
	if len(records) > q.limit {
		records = records[:q.limit]
		cursor := strconv.FormatInt(records[len(records)-1].Sequence, 10)
		w.Header().Set("X-Next-Cursor", cursor)
		w.Header().Set("Link", fmt.Sprintf(`<%s?after=%s&limit=%d>; rel="next"`, Path, cursor, q.limit))
	}
	respond(w, 200, records)
}

type query struct {
	id    string
	after int64
	limit int
}

func parseQuery(r *http.Request) (query, error) {
	out := query{limit: 100}
	// URL.Query silently drops invalid encodings; ParseQuery must reject them.
	q, err := parseRawQuery(r.URL.RawQuery)
	if err != nil {
		return out, errors.New("malformed query string")
	}
	for key, values := range q {
		if (key != "id" && key != "after" && key != "limit") || len(values) != 1 || values[0] == "" {
			return out, errors.New("unknown, repeated, or empty query parameter")
		}
	}
	if id, ok := q["id"]; ok {
		if len(q) != 1 || !sensor.ValidID(id[0]) {
			return out, errors.New("id must be a UUID and cannot be combined with pagination")
		}
		out.id = id[0]
		return out, nil
	}
	if v := q.Get("after"); v != "" {
		out.after, err = strconv.ParseInt(v, 10, 64)
		if err != nil || out.after < 0 {
			return out, errors.New("after must be a nonnegative integer")
		}
	}
	if v := q.Get("limit"); v != "" {
		out.limit, err = strconv.Atoi(v)
		if err != nil || out.limit < 1 || out.limit > MaxPage {
			return out, errors.New("limit must be between 1 and 1000")
		}
	}
	return out, nil
}

func (a *API) storeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, sensor.ErrNotFound):
		problem(w, 404, "not_found", "value not found")
	case errors.Is(err, sensor.ErrConflict):
		problem(w, 409, "conflict", "an ID already exists with different content; no values were written")
	default:
		a.log.Error("storage operation failed", "error", err)
		problem(w, 503, "storage_unavailable", "storage unavailable; retry with the same IDs")
	}
}
func respond(w http.ResponseWriter, status int, value any) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func problem(w http.ResponseWriter, status int, code, message string) {
	respond(w, status, map[string]any{"error": map[string]string{"code": code, "message": message}})
}
