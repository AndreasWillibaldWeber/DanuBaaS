// sensor-api serves the demo resource or applies its database migrations.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/config"
	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/httpapi"
	"github.com/AndreasWillibaldWeber/DanuBaaS/internal/postgres"
)

func main() {
	if err := run(); err != nil {
		slog.Error("sensor-api stopped", "error", err)
		os.Exit(1)
	}
}
func run() error {
	mode := "serve"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	if mode == "healthcheck" {
		client := http.Client{Timeout: 2 * time.Second}
		resp, err := client.Get("http://127.0.0.1:8081/readyz")
		if err != nil {
			return errors.New("readiness check failed")
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return errors.New("service not ready")
		}
		return nil
	}
	if mode != "serve" && mode != "migrate" && mode != "configure-alerts" {
		return errors.New("usage: sensor-api [serve|migrate|configure-alerts|healthcheck]")
	}
	cfg, err := config.Load(mode == "serve")
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	store, err := postgres.Open(startup, cfg.DSN)
	if err != nil {
		return fmt.Errorf("connect database: %w", err)
	}
	defer store.Close()
	if mode == "migrate" {
		return store.Migrate(startup)
	}
	if mode == "configure-alerts" {
		seconds, err := strconv.Atoi(os.Getenv("ALERT_EVALUATION_SECONDS"))
		if err != nil {
			return errors.New("ALERT_EVALUATION_SECONDS must be an integer")
		}
		file, err := os.Open(os.Getenv("ALERT_RULES_FILE"))
		if err != nil {
			return errors.New("cannot read ALERT_RULES_FILE")
		}
		defer file.Close()
		if err = store.SeedRules(startup, file); err != nil {
			return err
		}
		return store.ConfigureSchedule(startup, seconds)
	}
	adminKey, err := config.Secret("ALERT_ADMIN_KEY")
	if err != nil {
		return err
	}
	alertHandler, err := httpapi.NewAlerts(store, cfg.APIKey, adminKey)
	if err != nil {
		return err
	}
	webhook := ""
	if os.Getenv("ALERT_WEBHOOK_URL") != "" || os.Getenv("ALERT_WEBHOOK_URL_FILE") != "" {
		// An empty mounted file explicitly disables external delivery.
		if os.Getenv("ALERT_WEBHOOK_URL") == "" && os.Getenv("ALERT_WEBHOOK_URL_FILE") != "" {
			data, readErr := os.ReadFile(os.Getenv("ALERT_WEBHOOK_URL_FILE"))
			if readErr != nil {
				return errors.New("cannot read ALERT_WEBHOOK_URL_FILE")
			}
			if len(bytes.TrimSpace(data)) > 0 {
				webhook, err = config.Secret("ALERT_WEBHOOK_URL")
			}
		} else {
			webhook, err = config.Secret("ALERT_WEBHOOK_URL")
		}
		if err != nil {
			return err
		}
		if webhook != "" {
			if err = postgres.ValidateWebhook(webhook); err != nil {
				return err
			}
		}
	}
	handler, err := httpapi.New(store, cfg.APIKey, slog.Default())
	if err != nil {
		return err
	}
	health := http.NewServeMux()
	health.HandleFunc("GET /livez", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	health.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := store.Ping(ctx); err != nil {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(200)
	})
	mux := http.NewServeMux()
	mux.Handle("/api/v1/values", handler)
	for _, path := range []string{"/api/v1/alert-rules", "/api/v1/alert-rules/", "/api/v1/alerts", "/api/v1/alerts/", "/api/v1/alerting-status"} {
		mux.Handle(path, alertHandler)
	}
	senderDone := make(chan struct{})
	go func() {
		defer close(senderDone)
		if webhook != "" {
			store.SendNotifications(ctx, webhook)
		}
	}()
	defer func() { stop(); <-senderDone }()
	server := newServer(cfg.Listen, mux)
	healthServer := newServer(cfg.HealthListen, health)
	failures := make(chan error, 2)
	go func() { failures <- server.ListenAndServe() }()
	go func() { failures <- healthServer.ListenAndServe() }()
	slog.Info("sensor API started", "listen", cfg.Listen)
	var serveErr error
	select {
	case <-ctx.Done():
	case serveErr = <-failures:
	}
	shutdown, cancelShutdown := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancelShutdown()
	// Stop both listeners before the connection pool is closed.
	err = server.Shutdown(shutdown)
	healthErr := healthServer.Shutdown(shutdown)
	if serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
		return serveErr
	}
	return errors.Join(err, healthErr)
}
func newServer(addr string, handler http.Handler) *http.Server {
	return &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
}
