package config

import (
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSecretFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(path, []byte("secret\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TEST_SECRET", "")
	t.Setenv("TEST_SECRET_FILE", path)
	got, err := Secret("TEST_SECRET")
	if err != nil || got != "secret" {
		t.Fatalf("%q %v", got, err)
	}
	t.Setenv("TEST_SECRET", "other")
	if _, err = Secret("TEST_SECRET"); err == nil {
		t.Fatal("ambiguous secret accepted")
	}
}
func TestMissingSecretIsRedacted(t *testing.T) {
	t.Setenv("TEST_SECRET", "")
	t.Setenv("TEST_SECRET_FILE", "/nonexistent/credential")
	_, err := Secret("TEST_SECRET")
	if err == nil || strings.Contains(err.Error(), "/nonexistent") {
		t.Fatal(err)
	}
}
func TestPasswordEscaping(t *testing.T) {
	t.Setenv("DATABASE_URL", "")
	t.Setenv("DB_PASSWORD", "p@ss:/?#%word")
	t.Setenv("DB_PASSWORD_FILE", "")
	t.Setenv("DB_HOST", "::1")
	c, err := Load(false)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(c.DSN)
	if err != nil {
		t.Fatal(err)
	}
	pass, _ := u.User.Password()
	if pass != "p@ss:/?#%word" || u.Host != "[::1]:5432" {
		t.Fatal("DSN corrupted credentials or IPv6 host")
	}
}
