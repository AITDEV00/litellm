package setup

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// fakePrompter answers prompts from a script, and records what was asked, so a
// test can assert the flow did not ask for a secret it should not.
type fakePrompter struct {
	lines   []string
	secrets []string
	asked   []string
}

func (f *fakePrompter) Line(prompt string) (string, error) {
	f.asked = append(f.asked, prompt)
	if len(f.lines) == 0 {
		return "", errors.New("no scripted line")
	}
	value := f.lines[0]
	f.lines = f.lines[1:]
	return value, nil
}

func (f *fakePrompter) Secret(prompt string) (string, error) {
	f.asked = append(f.asked, prompt)
	if len(f.secrets) == 0 {
		return "", errors.New("no scripted secret")
	}
	value := f.secrets[0]
	f.secrets = f.secrets[1:]
	return value, nil
}

type fakeLogin struct {
	key      string
	err      error
	calls    int
	lastUser string
}

func (f *fakeLogin) Password(_ context.Context, _ string, username, password string) (string, error) {
	f.calls++
	f.lastUser = username
	if password == "" {
		return "", errors.New("empty password")
	}
	return f.key, f.err
}

func newGateway(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/mcp-sources":
			writeJSON(w, map[string]any{"source_id": "sid-1", "owner": "alice", "status": "pending_review"})
		case "/mcp-source-link-sources/sid-1/workers":
			writeJSON(w, map[string]any{
				"worker_id":  "worker-1",
				"source_id":  "sid-1",
				"target":     "127.0.0.1:18080",
				"generation": 1,
				"credential": "abababababababababababababababababababababababababababababababab",
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func writeJSON(w http.ResponseWriter, payload map[string]any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeSpec(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "openapi.json")
	body := `{"openapi":"3.1.0","info":{"title":"inventory","version":"1"},"paths":{"/stock":{"get":{}}}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func scripted(gateway string, specPath string) *fakePrompter {
	return &fakePrompter{
		lines:   []string{gateway, "alice", "pets_api", "dataset-1", "127.0.0.1:18080", "A" + strings.Repeat("A", 42) + "=", specPath},
		secrets: []string{"correct horse battery staple"},
	}
}

// TestInitializeWritesOwnerOnlyStateAndPersistsIdentity drives the real setup
// flow against a fake terminal and a real HTTPS gateway, and asserts every
// artifact is mode 0600 and the identity is the issued one.
func TestInitializeWritesOwnerOnlyStateAndPersistsIdentity(t *testing.T) {
	gateway := newGateway(t)
	dir := t.TempDir()
	paths := PathsFor(dir)
	login := &fakeLogin{key: "sk-user-key"}

	identity, err := Initialize(context.Background(), paths, scripted(gateway.URL, writeSpec(t, dir)), login, clientFor(t, gateway))
	if err != nil {
		t.Fatalf("initialize: %v", err)
	}
	if identity.WorkerID != "worker-1" || identity.SourceID != "sid-1" || identity.Generation != 1 {
		t.Fatalf("wrong identity: %+v", identity)
	}
	for _, path := range []string{paths.UserKey, paths.Bootstrap, paths.Identity} {
		info, statErr := os.Stat(path)
		if statErr != nil {
			t.Fatalf("missing %s: %v", path, statErr)
		}
		if info.Mode().Perm() != 0o600 {
			t.Errorf("%s must be mode 0600, got %o", path, info.Mode().Perm())
		}
	}
	if login.calls != 1 || login.lastUser != "alice" {
		t.Fatalf("login should run exactly once as alice, got calls=%d user=%q", login.calls, login.lastUser)
	}
}

// TestInitializeIsIdempotent proves a second run reuses the identity and never
// logs in or re-registers again.
func TestInitializeIsIdempotent(t *testing.T) {
	gateway := newGateway(t)
	dir := t.TempDir()
	paths := PathsFor(dir)
	login := &fakeLogin{key: "sk-user-key"}
	spec := writeSpec(t, dir)

	first, err := Initialize(context.Background(), paths, scripted(gateway.URL, spec), login, clientFor(t, gateway))
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	second, err := Initialize(context.Background(), paths, &fakePrompter{}, login, clientFor(t, gateway))
	if err != nil {
		t.Fatalf("second: %v", err)
	}
	if first != second {
		t.Fatalf("identity changed on rerun: %+v vs %+v", first, second)
	}
	if login.calls != 1 {
		t.Fatalf("rerun must not log in again, calls=%d", login.calls)
	}
}

// TestInitializeRejectsBadInputBeforeNetwork proves a non-loopback target or a
// missing spec fails without any login or enrollment attempt.
func TestInitializeRejectsBadInputBeforeNetwork(t *testing.T) {
	gateway := newGateway(t)
	dir := t.TempDir()
	login := &fakeLogin{key: "sk-user-key"}

	prompt := scripted(gateway.URL, writeSpec(t, dir))
	prompt.lines[4] = "10.0.0.1:80" // a non-loopback target
	_, err := Initialize(context.Background(), PathsFor(dir), prompt, login, clientFor(t, gateway))
	if err == nil {
		t.Fatal("a non-loopback target must be rejected")
	}
	if login.calls != 0 {
		t.Fatalf("no login should happen for invalid input, calls=%d", login.calls)
	}

	missing := scripted(gateway.URL, filepath.Join(dir, "does-not-exist.json"))
	_, err = Initialize(context.Background(), PathsFor(t.TempDir()), missing, login, clientFor(t, gateway))
	if err == nil {
		t.Fatal("a missing OpenAPI file must be rejected")
	}
}

// TestInitializeRejectsMalformedIssuedIdentity proves a mismatched source id or a
// non-hex credential never reaches disk.
func TestInitializeRejectsMalformedIssuedIdentity(t *testing.T) {
	bad := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/mcp-sources":
			writeJSON(w, map[string]any{"source_id": "sid-1", "owner": "alice", "status": "pending_review"})
		case "/mcp-source-link-sources/sid-1/workers":
			writeJSON(w, map[string]any{
				"worker_id": "worker-1", "source_id": "OTHER", "target": "127.0.0.1:18080",
				"generation": 1, "credential": strings.Repeat("z", 64),
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer bad.Close()
	dir := t.TempDir()
	paths := PathsFor(dir)
	_, err := Initialize(context.Background(), paths, scripted(bad.URL, writeSpec(t, dir)), &fakeLogin{key: "k"}, nil)
	if err == nil {
		t.Fatal("a malformed worker identity must be rejected")
	}
	if _, statErr := os.Stat(paths.Identity); statErr == nil {
		t.Fatal("a malformed identity must not be persisted")
	}
}

// TestInitializeSurfacesLoginFailure proves a failed sign-in aborts and leaves no
// identity behind.
func TestInitializeSurfacesLoginFailure(t *testing.T) {
	gateway := newGateway(t)
	dir := t.TempDir()
	paths := PathsFor(dir)
	login := &fakeLogin{err: errors.New("sign in failed (401 Unauthorized)")}
	_, err := Initialize(context.Background(), paths, scripted(gateway.URL, writeSpec(t, dir)), login, clientFor(t, gateway))
	if err == nil {
		t.Fatal("a failed login must abort initialization")
	}
	if _, statErr := os.Stat(paths.Identity); statErr == nil {
		t.Fatal("no identity should exist after a failed login")
	}
}

// clientFor builds the bootstrap client that trusts the test gateway's cert, so
// the flow exercises real HTTPS rather than a bypass.
func clientFor(t *testing.T, gateway *httptest.Server) *bootstrap.Client {
	t.Helper()
	client, err := bootstrap.New(gateway.URL, "sk-user-key", gateway.Client())
	if err != nil {
		t.Fatal(err)
	}
	return client
}

// TestInitializeResumesAfterWorkerFailure proves a first attempt that enrolls the
// source but fails to obtain the worker identity is recoverable without a second
// prompt or login: the stored key and bootstrap input drive the retry from disk.
func TestInitializeResumesAfterWorkerFailure(t *testing.T) {
	var workerCalls int
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/mcp-sources":
			writeJSON(w, map[string]any{"source_id": "sid-1", "owner": "alice", "status": "pending_review"})
		case "/mcp-source-link-sources/sid-1/workers":
			workerCalls++
			if workerCalls == 1 {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			writeJSON(w, map[string]any{
				"worker_id":  "worker-1",
				"source_id":  "sid-1",
				"target":     "127.0.0.1:18080",
				"generation": 1,
				"credential": "abababababababababababababababababababababababababababababababab",
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	dir := t.TempDir()
	paths := PathsFor(dir)
	login := &fakeLogin{key: "sk-user-key"}

	if _, err := Initialize(context.Background(), paths, scripted(server.URL, writeSpec(t, dir)), login, clientFor(t, server)); err == nil {
		t.Fatal("the first attempt should fail while the worker endpoint is unavailable")
	}
	if _, statErr := os.Stat(paths.Identity); statErr == nil {
		t.Fatal("no identity should exist after a failed worker call")
	}
	if _, statErr := os.Stat(paths.Bootstrap); statErr != nil {
		t.Fatalf("bootstrap input must survive the failure: %v", statErr)
	}
	if _, statErr := os.Stat(paths.UserKey); statErr != nil {
		t.Fatalf("user key must survive the failure: %v", statErr)
	}

	resumed, err := Initialize(context.Background(), paths, &fakePrompter{}, login, clientFor(t, server))
	if err != nil {
		t.Fatalf("resume must complete from stored state: %v", err)
	}
	if resumed.WorkerID != "worker-1" || resumed.Generation != 1 {
		t.Fatalf("wrong resumed identity: %+v", resumed)
	}
	if login.calls != 1 {
		t.Fatalf("resume must not log in a second time, calls=%d", login.calls)
	}
	if workerCalls != 2 {
		t.Fatalf("expected exactly one retry, worker calls=%d", workerCalls)
	}
}

// TestInitializeResumeRejectsTerminalConflict proves a permanent gateway refusal
// during resume is surfaced as a terminal registration error, not as a
// network-looking "not yet available" that a supervisor would retry forever.
func TestInitializeResumeRejectsTerminalConflict(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/mcp-sources":
			writeJSON(w, map[string]any{"source_id": "sid-1", "owner": "alice", "status": "pending_review"})
		case "/mcp-source-link-sources/sid-1/workers":
			w.WriteHeader(http.StatusConflict)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	dir := t.TempDir()
	paths := PathsFor(dir)
	login := &fakeLogin{key: "sk-user-key"}
	_, err := Initialize(context.Background(), paths, scripted(server.URL, writeSpec(t, dir)), login, clientFor(t, server))
	if err == nil {
		t.Fatal("a conflicting worker label must abort")
	}
	if !errors.Is(err, bootstrap.ErrTerminalRegistration) {
		t.Fatalf("a 409 must be terminal, got %v", err)
	}
}

var _ = bootstrap.WorkerLabel
var _ = config.ValidateBootstrapInput
