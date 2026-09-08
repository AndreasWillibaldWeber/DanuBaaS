// sensor-api serves the demo resource or applies its database migrations.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
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
	if mode != "serve" && mode != "migrate" {
		return errors.New("usage: sensor-api [serve|migrate|healthcheck]")
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
	server := newServer(cfg.Listen, handler)
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
