package postgres

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/alerts"
)

// ValidateWebhook requires TLS and disallows redirects/URL credentials. The URL
// is operator-managed secret configuration, never supplied by an API caller.
func ValidateWebhook(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return errors.New("ALERT_WEBHOOK_URL must be an HTTPS URL without userinfo or fragment")
	}
	return nil
}

// SendNotifications provides at-least-once delivery. Receivers must deduplicate
// Idempotency-Key; a crash after remote acceptance can cause redelivery.
func (s *Store) SendNotifications(ctx context.Context, endpoint string) {
	client := notificationClient(nil)
	timer := time.NewTicker(time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		for i := 0; i < 20; i++ {
			n, err := s.ClaimNotification(ctx)
			if errors.Is(err, alerts.ErrNotFound) {
				break
			}
			if err != nil {
				slog.Error("cannot claim alert notification")
				break
			}
			delivered := deliverNotification(ctx, client, endpoint, n)
			if err = s.FinishNotification(ctx, n, delivered); err != nil {
				slog.Error("cannot record alert notification delivery")
			}
		}
	}
}

func notificationClient(transport http.RoundTripper) *http.Client {
	return &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}
func deliverNotification(ctx context.Context, client *http.Client, endpoint string, n Notification) bool {
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewReader(n.Event))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "danubaas-alert-"+strconv.FormatInt(n.ID, 10))
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}
