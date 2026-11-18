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
	"oicm-litellm-layer/mcp-source-link/internal/state"
)

// scripted is a Prompter that replays canned answers, so a test drives the real
// prompt sequence without a terminal.
type scripted struct {
	lines    []string
	secrets  []string
	asked    []string
	lineHits int
	secHits  int
}

func (s *scripted) Line(prompt string) (string, error) { return s.LineDefault(prompt, "") }

func (s *scripted) LineDefault(prompt, defaultValue string) (string, error) {
	s.asked = append(s.asked, prompt)
	if s.lineHits >= len(s.lines) {
		return defaultValue, nil
	}
	answer := s.lines[s.lineHits]
	s.lineHits++
	if answer == "" {
		return defaultValue, nil
	}
	return answer, nil
}

func (s *scripted) Secret(prompt string) (string, error) {
	s.asked = append(s.asked, prompt)
	if s.secHits >= len(s.secrets) {
		return "", errors.New("no secret scripted")
	}
	answer := s.secrets[s.secHits]
	s.secHits++
	return answer, nil
}

// fakeLogin records how many times a password exchange happened and what account
// it corresponded to, so a test can assert a second source never logs in again.
type fakeLogin struct {
	calls []string
	key   string
}

func (f *fakeLogin) Password(_ context.Context, gatewayURL, username, _ string) (string, error) {
	f.calls = append(f.calls, gatewayURL+"|"+username)
	if f.key == "" {
		return "sk-" + username, nil
	}
	return f.key, nil
}

func newStore(t *testing.T) *state.Store {
	t.Helper()
	store, err := state.Open(t.TempDir())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return store
}

func writeSpec(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "openapi.json")
	spec := `{"openapi":"3.1.0","info":{"title":"s","version":"1"},"paths":{"/read":{"get":{"operationId":"read"}}}}`
	if err := os.WriteFile(path, []byte(spec), 0o600); err != nil {
		t.Fatalf("write spec: %v", err)
	}
	return path
}

// staticGateway answers every enroll with the same deterministic identity, so a
// test can prove a repeat registration resolves to one source rather than two.
func staticGateway(t *testing.T, sourceID string) *bootstrap.Client {
	t.Helper()
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/mcp-sources") {
			_ = json.NewEncoder(w).Encode(map[string]string{"source_id": sourceID, "owner": "alice", "status": "approved"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"worker_id": "w-" + sourceID, "source_id": sourceID, "target": "127.0.0.1:9000",
			"generation": 1, "credential": strings.Repeat("a", 64),
		})
	}))
	t.Cleanup(server.Close)
	client, err := bootstrap.New(server.URL, "sk-alice", server.Client())
	if err != nil {
		t.Fatalf("bootstrap.New: %v", err)
	}
	return client
}

// N1: adding an account asks only for URL, username and password, and stores the
// key rather than the password.
func TestAddAccountAsksOnlyThreeQuestions(t *testing.T) {
	store := newStore(t)
	prompt := &scripted{
		lines:   []string{"https://gw.example", "alice"},
		secrets: []string{"hunter2"},
	}
	login := &fakeLogin{}
	account, err := AddAccount(context.Background(), store, prompt, login, "alpha")
	if err != nil {
		t.Fatalf("AddAccount: %v", err)
	}
	if account.Alias != "alpha" || account.Username != "alice" || account.GatewayURL != "https://gw.example" {
		t.Fatalf("unexpected account: %+v", account)
	}
	if len(prompt.asked) != 3 {
		t.Fatalf("account add asked %d questions, want 3: %v", len(prompt.asked), prompt.asked)
	}
	for _, question := range prompt.asked {
		if strings.Contains(strings.ToLower(question), "proof") || strings.Contains(strings.ToLower(question), "dataset") {
			t.Fatalf("account add asked an opaque question: %q", question)
		}
	}
	key, err := store.AccountKey("alpha")
	if err != nil || key != "sk-alice" {
		t.Fatalf("stored key = %q %v", key, err)
	}
}

// The raw password must never be persisted anywhere in the state directory.
func TestAddAccountNeverStoresPassword(t *testing.T) {
	store := newStore(t)
	prompt := &scripted{lines: []string{"https://gw.example", "alice"}, secrets: []string{"hunter2-secret"}}
	if _, err := AddAccount(context.Background(), store, prompt, &fakeLogin{}, "alpha"); err != nil {
		t.Fatalf("AddAccount: %v", err)
	}
	leaked := false
	_ = filepath.Walk(store.Base(), func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		contents, _ := os.ReadFile(path)
		if strings.Contains(string(contents), "hunter2-secret") {
			leaked = true
		}
		return nil
	})
	if leaked {
		t.Fatal("the raw password was written to the state directory")
	}
}

// N2: a second source under an existing account must not ask for a password.
func TestAddSourceReusesAccountWithoutLogin(t *testing.T) {
	store := newStore(t)
	login := &fakeLogin{}
	accountPrompt := &scripted{lines: []string{"https://gw.example", "alice"}, secrets: []string{"pw"}}
	if _, err := AddAccount(context.Background(), store, accountPrompt, login, "alpha"); err != nil {
		t.Fatalf("AddAccount: %v", err)
	}
	if len(login.calls) != 1 {
		t.Fatalf("expected 1 login for account add, got %d", len(login.calls))
	}
	spec := writeSpec(t)

	sourcePrompt := &scripted{}
	source, err := AddSource(context.Background(), store, sourcePrompt, staticGateway(t, "src-1"), AddSourceInput{
		Target:   "127.0.0.1:18201",
		SpecPath: spec,
	})
	if err != nil {
		t.Fatalf("AddSource: %v", err)
	}
	if source.Account != "alpha" || source.Name != "alpha.127.0.0.1.18201" {
		t.Fatalf("unexpected source: %+v", source)
	}
	if len(login.calls) != 1 {
		t.Fatalf("adding a source triggered another login: %v", login.calls)
	}
	for _, question := range sourcePrompt.asked {
		lower := strings.ToLower(question)
		if strings.Contains(lower, "password") || strings.Contains(lower, "username") || strings.Contains(lower, "gateway url") {
			t.Fatalf("source add asked for credentials again: %q", question)
		}
	}
}

// A second source under a different account reuses that account's own key and
// still does not log in again.
func TestAddSourceForSecondAccount(t *testing.T) {
	store := newStore(t)
	login := &fakeLogin{}
	for _, alias := range []string{"alpha", "beta"} {
		prompt := &scripted{lines: []string{"https://gw.example", alias}, secrets: []string{"pw"}}
		if _, err := AddAccount(context.Background(), store, prompt, login, alias); err != nil {
			t.Fatalf("AddAccount(%s): %v", alias, err)
		}
	}
	spec := writeSpec(t)
	first, err := AddSource(context.Background(), store, &scripted{}, staticGateway(t, "shared"), AddSourceInput{
		Account: "alpha", Target: "10.0.0.1:80", SpecPath: spec,
	})
	if err != nil {
		t.Fatalf("alpha source: %v", err)
	}
	second, err := AddSource(context.Background(), store, &scripted{}, staticGateway(t, "shared"), AddSourceInput{
		Account: "beta", Target: "10.0.0.1:80", SpecPath: spec,
	})
	if err != nil {
		t.Fatalf("beta source: %v", err)
	}
	if first.Name == second.Name {
		t.Fatalf("two accounts produced the same source name %q", first.Name)
	}
	if len(login.calls) != 2 {
		t.Fatalf("expected 2 logins (one per account), got %d", len(login.calls))
	}
}

// A repeated registration for the same target in the same account is the same
// source: it returns AlreadyExists and creates no second entry.
func TestAddSourceRepeatIsAlreadyExists(t *testing.T) {
	store := newStore(t)
	if _, err := AddAccount(context.Background(), store, &scripted{
		lines: []string{"https://gw.example", "alice"}, secrets: []string{"pw"},
	}, &fakeLogin{}, "alpha"); err != nil {
		t.Fatalf("AddAccount: %v", err)
	}
	spec := writeSpec(t)
	gateway := staticGateway(t, "stable")
	first, err := AddSource(context.Background(), store, &scripted{}, gateway, AddSourceInput{
		Target: "127.0.0.1:9000", SpecPath: spec,
	})
	if err != nil {
		t.Fatalf("first AddSource: %v", err)
	}
	second, err := AddSource(context.Background(), store, &scripted{}, gateway, AddSourceInput{
		Target: "127.0.0.1:9000", SpecPath: spec,
	})
	var exists AlreadyExists
	if !errors.As(err, &exists) {
		t.Fatalf("repeat registration returned %v, want AlreadyExists", err)
	}
	if exists.Source.Name != first.Name || second.Name != first.Name {
		t.Fatalf("repeat changed the source name: %q vs %q", exists.Source.Name, first.Name)
	}
	sources, err := store.Sources()
	if err != nil {
		t.Fatalf("Sources: %v", err)
	}
	if len(sources) != 1 {
		t.Fatalf("repeat registration created %d sources, want 1", len(sources))
	}
}

// A source added under no selected account fails clearly rather than guessing.
func TestAddSourceWithoutAccountFails(t *testing.T) {
	store := newStore(t)
	spec := writeSpec(t)
	if _, err := AddSource(context.Background(), store, &scripted{}, staticGateway(t, "no-account"), AddSourceInput{
		Target: "127.0.0.1:9000", SpecPath: spec,
	}); err == nil {
		t.Fatal("added a source with no account selected")
	}
}

// An unparseable target must be rejected before anything is stored.
func TestAddSourceRejectsBadTarget(t *testing.T) {
	store := newStore(t)
	if _, err := AddAccount(context.Background(), store, &scripted{
		lines: []string{"https://gw.example", "alice"}, secrets: []string{"pw"},
	}, &fakeLogin{}, "alpha"); err != nil {
		t.Fatalf("AddAccount: %v", err)
	}
	spec := writeSpec(t)
	gateway := staticGateway(t, "bad-target")
	for _, target := range []string{"http://127.0.0.1:1", "example.com:443", "127.0.0.1:0"} {
		if _, err := AddSource(context.Background(), store, &scripted{}, gateway, AddSourceInput{
			Target: target, SpecPath: spec,
		}); err == nil {
			t.Fatalf("accepted invalid target %q", target)
		}
	}
}
