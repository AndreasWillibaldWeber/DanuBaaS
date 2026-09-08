// Package config loads runtime settings without exposing secrets in errors.
package config

import (
	"errors"
	"net"
	"net/url"
	"os"
	"strings"
)

type Config struct {
	Listen       string
	HealthListen string
	APIKey       string
	DSN          string
}

func Secret(name string) (string, error) {
	value, file := os.Getenv(name), os.Getenv(name+"_FILE")
	if value != "" && file != "" {
		return "", errors.New(name + " and " + name + "_FILE are mutually exclusive")
	}
	if file != "" {
		data, err := os.ReadFile(file)
		if err != nil {
			return "", errors.New("cannot read " + name + "_FILE")
		}
		value = strings.TrimRight(string(data), "\r\n")
	}
	if value == "" {
		return "", errors.New(name + " or " + name + "_FILE is required")
	}
	return value, nil
}
func Load(requireAPIKey bool) (Config, error) {
	c := Config{Listen: env("LISTEN_ADDR", ":8080"), HealthListen: env("HEALTH_ADDR", ":8081")}
	var err error
	if requireAPIKey {
		c.APIKey, err = Secret("API_KEY")
		if err != nil {
			return c, err
		}
		if len(c.APIKey) < 32 {
			return c, errors.New("API_KEY must contain at least 32 bytes")
		}
	}
	if os.Getenv("DATABASE_URL") != "" {
		c.DSN = os.Getenv("DATABASE_URL")
		return c, nil
	}
	pass, err := Secret("DB_PASSWORD")
	if err != nil {
		return c, err
	}
	u := url.URL{Scheme: "postgres", User: url.UserPassword(env("DB_USER", "sensor_api"), pass), Host: net.JoinHostPort(env("DB_HOST", "localhost"), env("DB_PORT", "5432")), Path: "/" + env("DB_NAME", "sensors")}
	q := url.Values{"sslmode": {env("DB_SSLMODE", "require")}}
	u.RawQuery = q.Encode()
	c.DSN = u.String()
	return c, nil
}
func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
