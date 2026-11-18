package auth

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestPasswordReturnsIssuedKey proves the happy path returns the key from the
// endpoint and sends the credentials in the body, never the URL.
func TestPasswordReturnsIssuedKey(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/sso/cli/password" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		if r.URL.RawQuery != "" {
			t.Errorf("credentials must not appear in the query string, got %q", r.URL.RawQuery)
		}
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["username"] != "alice" || body["password"] != "secret" {
			t.Errorf("unexpected body: %v", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"key":"sk-issued","user_id":"alice"}`))
	}))
	defer server.Close()

	key, err := PasswordLogin{HTTP: server.Client()}.Password(context.Background(), server.URL, "alice", "secret")
	if err != nil {
		t.Fatalf("login: %v", err)
	}
	if key != "sk-issued" {
		t.Fatalf("wrong key %q", key)
	}
}

// TestPasswordRejectsWithoutEchoingSecrets proves a failed sign-in surfaces only
// the status, so a typo'd password or a server detail cannot leak back.
func TestPasswordRejectsWithoutEchoingSecrets(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "server-secret-detail", http.StatusUnauthorized)
	}))
	defer server.Close()

	_, err := PasswordLogin{HTTP: server.Client()}.Password(context.Background(), server.URL, "alice", "wrong")
	if err == nil {
		t.Fatal("a 401 must fail")
	}
	if strings.Contains(err.Error(), "server-secret-detail") {
		t.Fatalf("error must not echo the response body: %v", err)
	}
}

// TestPasswordRejectsMissingKey proves a 200 without a credential is not treated
// as success.
func TestPasswordRejectsMissingKey(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"user_id":"alice"}`))
	}))
	defer server.Close()
	if _, err := (PasswordLogin{HTTP: server.Client()}).Password(context.Background(), server.URL, "a", "b"); err == nil {
		t.Fatal("a response without a key must fail")
	}
}
