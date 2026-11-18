package connector

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// TestLoadIdentityBootstrapsOnceAndReusesPersistedIdentity drives the real REST
// path against an injected TLS gateway. It proves the first-use flow persists an
// owner-only identity and that a restart reuses it without a second registration.
func TestLoadIdentityBootstrapsOnceAndReusesPersistedIdentity(t *testing.T) {
	dir := t.TempDir()
	bootstrapFile := filepath.Join(dir, "bootstrap.json")
	identityFile := filepath.Join(dir, "identity.json")
	writeOwnerOnly(t, bootstrapFile, `{
		"alias": "pets_api",
		"dataset_id": "dataset-1",
		"proof_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		"target": "127.0.0.1:18080",
		"spec": "{\"openapi\":\"3.1.0\",\"paths\":{\"/v1/pets\":{\"get\":{}}}}"
	}`)

	var enrollCalls, workerCalls int
	gateway := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer ordinary-user-key" {
			t.Errorf("request must carry the ordinary user key, got %q", got)
		}
		switch {
		case r.URL.Path == "/mcp-sources":
			enrollCalls++
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if _, present := body["owner"]; present {
				t.Error("the request must not send an owner field; the gateway derives it")
			}
			writeJSON(t, w, map[string]any{"source_id": "sid-1", "owner": "alice", "status": "pending_review"})
		case r.URL.Path == "/mcp-source-link-sources/sid-1/workers":
			workerCalls++
			writeJSON(t, w, map[string]any{
				"worker_id":  "worker-1",
				"source_id":  "sid-1",
				"target":     "127.0.0.1:18080",
				"generation": 1,
				"credential": "abababababababababababababababababababababababababababababababab",
			})
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer gateway.Close()

	t.Setenv("MCP_SOURCE_LINK_BOOTSTRAP_INPUT", bootstrapFile)
	t.Setenv("MCP_SOURCE_LINK_CONNECTOR_CONFIG", identityFile)
	t.Setenv("MCP_SOURCE_LINK_USER_KEY", "ordinary-user-key")

	identity, err := LoadIdentity(context.Background(), gateway.URL, gateway.Client())
	if err != nil {
		t.Fatalf("first bootstrap: %v", err)
	}
	if identity.WorkerID != "worker-1" || identity.SourceID != "sid-1" || identity.Generation != 1 {
		t.Fatalf("issued identity is wrong: %+v", identity)
	}
	info, err := os.Stat(identityFile)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("persisted identity must be mode 0600, got %o", info.Mode().Perm())
	}

	reused, err := LoadIdentity(context.Background(), gateway.URL, gateway.Client())
	if err != nil {
		t.Fatalf("restart: %v", err)
	}
	if reused != identity {
		t.Fatalf("restart changed identity: %+v vs %+v", reused, identity)
	}
	if enrollCalls != 1 || workerCalls != 1 {
		t.Fatalf("restart must not re-register: enroll=%d worker=%d", enrollCalls, workerCalls)
	}
}

// TestLoadIdentityFailsLoudlyOnUnsafePermissions proves a world-readable bootstrap
// file is a fatal, actionable error rather than a silent acceptance.
func TestLoadIdentityFailsLoudlyOnUnsafePermissions(t *testing.T) {
	dir := t.TempDir()
	bootstrapFile := filepath.Join(dir, "bootstrap.json")
	writeOwnerOnly(t, bootstrapFile, `{}`)
	if err := os.Chmod(bootstrapFile, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MCP_SOURCE_LINK_BOOTSTRAP_INPUT", bootstrapFile)
	t.Setenv("MCP_SOURCE_LINK_CONNECTOR_CONFIG", filepath.Join(dir, "identity.json"))
	if _, err := LoadIdentity(context.Background(), "https://gateway.invalid", http.DefaultClient); err == nil {
		t.Fatal("a non-owner-only bootstrap file must be rejected")
	}
}

// TestEnrollOnceSurfacesGatewayFailure proves a non-retryable gateway status stops
// the loop with an error that names the path but never echoes a credential.
func TestEnrollOnceSurfacesGatewayFailure(t *testing.T) {
	gateway := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "server-secret-must-not-escape", http.StatusForbidden)
	}))
	defer gateway.Close()
	input := config.BootstrapInput{Alias: "pets_api", DatasetID: "d", ProofKey: "pk", Spec: "{}"}
	client, err := bootstrap.New(gateway.URL, "user-key", gateway.Client())
	if err != nil {
		t.Fatal(err)
	}
	target, err := config.ParseTarget("127.0.0.1:18080")
	if err != nil {
		t.Fatal(err)
	}
	_, err = enrollOnce(context.Background(), client, input, target)
	if err == nil {
		t.Fatal("a 403 from the gateway must not be treated as success")
	}
	if strings.Contains(err.Error(), "server-secret-must-not-escape") || strings.Contains(err.Error(), "user-key") {
		t.Fatalf("error must not echo response body or credential: %v", err)
	}
}

func writeOwnerOnly(t *testing.T, path string, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}

func writeJSON(t *testing.T, w http.ResponseWriter, payload map[string]any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		t.Fatal(err)
	}
}

// TestWorkerLabelMatchesGatewayPattern proves the derived label always satisfies
// the gateway's worker-label rule, and that aliases which already fit are unchanged.
func TestWorkerLabelMatchesGatewayPattern(t *testing.T) {
	pattern := regexp.MustCompile(`^[a-z][a-z0-9-]{1,31}$`)
	for _, alias := range []string{
		"pets", "pets-api", "warehouse_inventory", "Warehouse_Inventory", "a",
		"9lives", "---", strings.Repeat("x", 60), "with.dot.and_underscore",
	} {
		label := WorkerLabel(alias)
		if !pattern.MatchString(label) {
			t.Errorf("alias %q produced invalid label %q", alias, label)
		}
		if WorkerLabel(alias) != label {
			t.Errorf("alias %q is not deterministic", alias)
		}
	}
	if got := WorkerLabel("pets-api"); got != "pets-api" {
		t.Errorf("an already-valid alias must pass through verbatim, got %q", got)
	}
}
