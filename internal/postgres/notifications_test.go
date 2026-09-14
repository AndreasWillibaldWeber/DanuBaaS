package postgres

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestWebhookDelivery(t *testing.T) {
	for _, status := range []int{200, 204, 302, 429, 500} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			calls := 0
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.Method != "POST" || r.Header.Get("Idempotency-Key") != "danubaas-alert-42" || r.Header.Get("Content-Type") != "application/json" {
					t.Error("incorrect webhook request")
				}
				body, _ := io.ReadAll(r.Body)
				if string(body) != `{"event":"opened"}` {
					t.Error("lost event payload")
				}
				w.Header().Set("Location", "/redirect")
				w.WriteHeader(status)
			}))
			defer server.Close()
			client := notificationClient(server.Client().Transport)
			got := deliverNotification(context.Background(), client, server.URL, Notification{ID: 42, Event: json.RawMessage(`{"event":"opened"}`)})
			if got != (status >= 200 && status < 300) || calls != 1 {
				t.Fatalf("delivered=%v requests=%d", got, calls)
			}
		})
	}
}
func TestWebhookConfiguration(t *testing.T) {
	for _, url := range []string{"http://receiver.org", "https://user:pass@receiver.org", "https://receiver.org/#secret", "missing"} {
		if ValidateWebhook(url) == nil {
			t.Errorf("accepted %s", url)
		}
	}
	if err := ValidateWebhook("https://receiver.org/events?token=secret"); err != nil {
		t.Fatal(err)
	}
}
