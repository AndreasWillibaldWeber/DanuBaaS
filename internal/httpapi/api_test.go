package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/sensor"
)

const testKey = "0123456789abcdef0123456789abcdef"
const firstID = "12345678-1234-4234-8234-123456789001"
const firstBody = `{"id":"12345678-1234-4234-8234-123456789001","sensor_id":"s1","sensor_type":"water-level","timestamp":"2026-09-14T10:00:00Z","value":0,"unit":"m"}`

type memoryStore struct {
	mu      sync.Mutex
	records []sensor.Record
	failure error
	puts    int
}

func (s *memoryStore) Put(_ context.Context, vs []sensor.Value) ([]sensor.Record, int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.puts++
	if s.failure != nil {
		return nil, 0, s.failure
	}
	// Validate conflicts first to model the storage contract's atomicity.
	for _, v := range vs {
		for _, r := range s.records {
			if r.ID == v.ID {
				a, _ := json.Marshal(r.Value)
				b, _ := json.Marshal(v)
				if string(a) != string(b) {
					return nil, 0, sensor.ErrConflict
				}
			}
		}
	}
	out := []sensor.Record{}
	created := 0
	for _, v := range vs {
		found := false
		for _, r := range s.records {
			if r.ID == v.ID {
				out = append(out, r)
				found = true
				break
			}
		}
		if !found {
			r := sensor.Record{Value: v, Sequence: int64(len(s.records) + 1), ReceivedAt: time.Now().UTC()}
			s.records = append(s.records, r)
			out = append(out, r)
			created++
		}
	}
	return out, created, nil
}
func (s *memoryStore) Get(_ context.Context, id string) (sensor.Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failure != nil {
		return sensor.Record{}, s.failure
	}
	for _, r := range s.records {
		if r.ID == id {
			return r, nil
		}
	}
	return sensor.Record{}, sensor.ErrNotFound
}
func (s *memoryStore) List(_ context.Context, after int64, limit int) ([]sensor.Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failure != nil {
		return nil, s.failure
	}
	out := []sensor.Record{}
	for _, r := range s.records {
		if r.Sequence > after && len(out) < limit {
			out = append(out, r)
		}
	}
	return out, nil
}
func (s *memoryStore) Ping(context.Context) error { return s.failure }
func setup(t *testing.T) (http.Handler, *memoryStore) {
	t.Helper()
	s := &memoryStore{}
	h, err := New(s, testKey, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	return h, s
}
func request(h http.Handler, method, path, body, key string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	if key != "" {
		r.Header.Set("X-API-Key", key)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func status(t *testing.T, w *httptest.ResponseRecorder, want int) {
	t.Helper()
	if w.Code != want {
		t.Fatalf("status %d, want %d; body=%s", w.Code, want, w.Body.String())
	}
}

func TestSingleRoundTripAndReplay(t *testing.T) {
	h, s := setup(t)
	created := request(h, "POST", Path, firstBody, testKey)
	status(t, created, 201)
	if created.Header().Get("Location") != Path+"?id="+firstID {
		t.Fatal("missing resource location")
	}
	got := request(h, "GET", Path+"?id="+firstID, "", testKey)
	status(t, got, 200)
	if got.Body.String() != created.Body.String() {
		t.Fatal("GET did not return committed record")
	}
	replay := request(h, "POST", Path, firstBody, testKey)
	status(t, replay, 200)
	if replay.Body.String() != created.Body.String() || len(s.records) != 1 {
		t.Fatal("retry changed record or created duplicate")
	}
	conflict := request(h, "POST", Path, strings.Replace(firstBody, `"value":0`, `"value":1`, 1), testKey)
	status(t, conflict, 409)
}
func TestBatchAndPagination(t *testing.T) {
	h, _ := setup(t)
	second := strings.ReplaceAll(firstBody, firstID, "12345678-1234-4234-8234-123456789002")
	status(t, request(h, "POST", Path, "["+firstBody+","+second+"]", testKey), 201)
	first := request(h, "GET", Path+"?limit=1", "", testKey)
	status(t, first, 200)
	if first.Header().Get("X-Next-Cursor") != "1" || !strings.Contains(first.Header().Get("Link"), "after=1") {
		t.Fatal("missing next-page cursor")
	}
	next := request(h, "GET", Path+"?after=1&limit=1", "", testKey)
	status(t, next, 200)
	if next.Header().Get("X-Next-Cursor") != "" {
		t.Fatal("unexpected next page")
	}
	var a, b []sensor.Record
	if err := json.Unmarshal(first.Body.Bytes(), &a); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(next.Body.Bytes(), &b); err != nil {
		t.Fatal(err)
	}
	if len(a) != 1 || len(b) != 1 || a[0].ID == b[0].ID {
		t.Fatal("pagination duplicated or lost values")
	}
}
func TestEmptyListIsArray(t *testing.T) {
	h, _ := setup(t)
	w := request(h, "GET", Path, "", testKey)
	status(t, w, 200)
	if strings.TrimSpace(w.Body.String()) != "[]" {
		t.Fatal(w.Body.String())
	}
}
func TestAuthenticationAllMethods(t *testing.T) {
	for _, method := range []string{"GET", "POST", "DELETE", "HEAD", "OPTIONS"} {
		for _, key := range []string{"", "incorrect"} {
			t.Run(method+key, func(t *testing.T) {
				h, s := setup(t)
				status(t, request(h, method, Path, firstBody, key), 401)
				if s.puts != 0 {
					t.Fatal("unauthenticated write reached storage")
				}
			})
		}
	}
}
func TestRepeatedAPIKeyRejected(t *testing.T) {
	h, _ := setup(t)
	r := httptest.NewRequest("GET", Path, nil)
	r.Header.Add("X-API-Key", testKey)
	r.Header.Add("X-API-Key", testKey)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	status(t, w, 401)
}
func TestAPIKeyInQueryIsNotAuthentication(t *testing.T) {
	h, _ := setup(t)
	status(t, request(h, "GET", Path+"?api_key="+testKey, "", ""), 401)
}
func TestMethodAndRoute(t *testing.T) {
	h, _ := setup(t)
	w := request(h, "DELETE", Path, "", testKey)
	status(t, w, 405)
	if w.Header().Get("Allow") != "GET, POST" {
		t.Fatal("missing Allow")
	}
	status(t, request(h, "GET", "/healthz", "", testKey), 404)
	status(t, request(h, "GET", Path+"/", "", testKey), 404)
}
func TestRejectedBodiesDoNotWrite(t *testing.T) {
	cases := []struct {
		name, body string
		code       int
	}{
		{"invalid UTF-8", strings.Replace(firstBody, `"unit":"m"`, "\"unit\":\"\xff\"", 1), 400},
		{"large observation", strings.Replace(firstBody, `"unit":"m"`, `"unit":"m","metadata":{"raw":"`+strings.Repeat("x", 16<<10)+`"}`, 1), 422},
		{"empty", "", 400}, {"null", "null", 400}, {"scalar", "1", 400}, {"string", `"hello"`, 400}, {"empty batch", "[]", 422}, {"null entry", "[null]", 422},
		{"trailing", firstBody + " {}", 400}, {"malformed", `{"id":`, 400}, {"unknown", strings.Replace(firstBody, `"unit":"m"`, `"unit":"m","extra":1`, 1), 400},
		{"duplicate key", strings.Replace(firstBody, `"value":0`, `"value":0,"value":1`, 1), 400},
		{"nested duplicate", strings.Replace(firstBody, `"unit":"m"`, `"unit":"m","metadata":{"rssi":1,"rssi":2}`, 1), 400},
		{"missing number", strings.Replace(firstBody, `"value":0,`, "", 1), 422}, {"null number", strings.Replace(firstBody, `"value":0`, `"value":null`, 1), 422},
		{"string number", strings.Replace(firstBody, `"value":0`, `"value":"1"`, 1), 400}, {"overflow number", strings.Replace(firstBody, `"value":0`, `"value":1e400`, 1), 400},
		{"timezone absent", strings.Replace(firstBody, "10:00:00Z", "10:00:00", 1), 400}, {"precision", strings.Replace(firstBody, "10:00:00Z", "10:00:00.0000001Z", 1), 422},
		{"bad uuid", strings.Replace(firstBody, firstID, "bad", 1), 422}, {"blank sensor", strings.Replace(firstBody, `"sensor_id":"s1"`, `"sensor_id":""`, 1), 422},
		{"invalid final item", "[" + firstBody + ",{}]", 422}, {"duplicate batch id", "[" + firstBody + "," + firstBody + "]", 422},
		{"body size", `{"metadata":"` + strings.Repeat("x", MaxBody) + `"}`, 413},
		{"too many values", "[" + strings.Repeat(firstBody+",", MaxBatch) + firstBody + "]", 422},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h, s := setup(t)
			status(t, request(h, "POST", Path, tc.body, testKey), tc.code)
			if s.puts != 0 {
				t.Fatal("invalid batch reached storage")
			}
		})
	}
}
func TestInvalidQuery(t *testing.T) {
	for _, q := range []string{"?limit=0", "?limit=1001", "?limit=no", "?after=-1", "?after=9223372036854775808", "?id=bad", "?id=" + firstID + "&limit=1", "?limit=1&limit=2", "?limit=", "?unknown=x", "?limit=%GG", "?limit=1;after=0"} {
		t.Run(q, func(t *testing.T) { h, _ := setup(t); status(t, request(h, "GET", Path+q, "", testKey), 400) })
	}
}
func TestMissingValue(t *testing.T) {
	h, _ := setup(t)
	status(t, request(h, "GET", Path+"?id="+firstID, "", testKey), 404)
}
func TestStorageErrorsAreRedacted(t *testing.T) {
	h, s := setup(t)
	s.failure = errors.New("password=secret database connection failed")
	for _, method := range []string{"GET", "POST"} {
		w := request(h, method, Path, firstBody, testKey)
		status(t, w, 503)
		if strings.Contains(w.Body.String(), "secret") {
			t.Fatal("leaked storage error")
		}
	}
}
func TestMediaType(t *testing.T) {
	h, _ := setup(t)
	for _, ct := range []string{"", "text/plain", "application/xml"} {
		r := httptest.NewRequest("POST", Path, strings.NewReader(firstBody))
		r.Header.Set("X-API-Key", testKey)
		r.Header.Set("Content-Type", ct)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		status(t, w, 415)
	}
}
func TestConcurrentRetries(t *testing.T) {
	h, s := setup(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := request(h, "POST", Path, firstBody, testKey)
			if w.Code != 200 && w.Code != 201 {
				t.Errorf("retry failed: %d", w.Code)
			}
		}()
	}
	wg.Wait()
	if len(s.records) != 1 {
		t.Fatal("duplicate observations")
	}
}
func TestTimezoneNormalization(t *testing.T) {
	h, _ := setup(t)
	w := request(h, "POST", Path, strings.Replace(firstBody, "10:00:00Z", "12:00:00+02:00", 1), testKey)
	status(t, w, 201)
	if !strings.Contains(w.Body.String(), `"timestamp":"2026-09-14T10:00:00Z"`) {
		t.Fatal(w.Body.String())
	}
	status(t, request(h, "POST", Path, firstBody, testKey), 200)
}
func TestConstructorRejectsWeakKey(t *testing.T) {
	if _, err := New(&memoryStore{}, "short", nil); err == nil {
		t.Fatal("weak key accepted")
	}
}
func FuzzPOSTNeverPanics(f *testing.F) {
	for _, seed := range []string{firstBody, "[]", "null", `{"value":null}`, `{"a":1,"a":2}`} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, body string) {
		if len(body) > MaxBody+1 {
			t.Skip()
		}
		h, _ := setup(t)
		w := request(h, "POST", Path, body, testKey)
		if w.Code < 200 || w.Code >= 500 {
			t.Fatalf("unexpected response: %d %s", w.Code, w.Body.String())
		}
		if !json.Valid(w.Body.Bytes()) {
			t.Fatal(fmt.Sprintf("invalid response JSON: %q", w.Body.String()))
		}
	})
}
