package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func writeBootstrap(t *testing.T, dir, gateway string) {
	t.Helper()
	input := BootstrapInput{
		Alias:      "pets",
		DatasetID:  "d1",
		ProofKey:   "key",
		Target:     "127.0.0.1:18080",
		Spec:       "{}",
		GatewayURL: gateway,
	}
	if err := WriteBootstrapInput(filepath.Join(dir, "bootstrap.json"), input); err != nil {
		t.Fatal(err)
	}
}

// TestResolveGatewayFallsBackToPersistedBootstrap is the R2 regression: an
// installed service must restart from the URL written during init, with no
// hand-written environment file. Before this, startup demanded
// MCP_SOURCE_LINK_PUBLIC_URL and a successful first setup could not restart.
func TestResolveGatewayFallsBackToPersistedBootstrap(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://gateway.example")
	t.Setenv("MCP_SOURCE_LINK_PUBLIC_URL", "")

	resolved, err := ResolveGateway(dir)
	if err != nil {
		t.Fatalf("persisted gateway must be enough to start: %v", err)
	}
	if resolved != "https://gateway.example" {
		t.Fatalf("got %q, want the persisted gateway", resolved)
	}
}

// TestResolveGatewayPrefersExplicitOverride pins the precedence so automation can
// still point a connector at another gateway without editing stored state.
func TestResolveGatewayPrefersExplicitOverride(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://persisted.example")
	t.Setenv("MCP_SOURCE_LINK_PUBLIC_URL", "https://override.example")

	resolved, err := ResolveGateway(dir)
	if err != nil {
		t.Fatalf("override must be accepted: %v", err)
	}
	if resolved != "https://override.example" {
		t.Fatalf("got %q, want the explicit override", resolved)
	}
}

func TestResolveGatewayWithoutAnyConfigurationIsActionable(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("MCP_SOURCE_LINK_PUBLIC_URL", "")

	if _, err := ResolveGateway(dir); err == nil {
		t.Fatal("no configuration must fail")
	} else if want := "run: source-link init"; !contains(err.Error(), want) {
		t.Fatalf("error must tell the operator what to do, got %v", err)
	}
}

func TestValidateGatewayURLRejectsUnsafeForms(t *testing.T) {
	cases := map[string]string{
		"plain http":       "http://gateway.example",
		"no host":          "https://",
		"userinfo":         "https://user:pass@gateway.example",
		"path":             "https://gateway.example/api",
		"query":            "https://gateway.example?x=1",
		"not a url at all": "gateway",
	}
	for name, value := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ValidateGatewayURL(value); err == nil {
				t.Fatalf("%q must be rejected", value)
			}
		})
	}
	if got, err := ValidateGatewayURL("https://gateway.example/"); err != nil || got != "https://gateway.example" {
		t.Fatalf("a trailing slash must normalize away, got %q err %v", got, err)
	}
}

// TestBootstrapInputRoundTripsGatewayURL guards the persistence the resolver
// depends on: if the field stopped being written, restart would silently fail.
func TestBootstrapInputRoundTripsGatewayURL(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://gateway.example")

	raw, err := os.ReadFile(filepath.Join(dir, "bootstrap.json"))
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["gateway_url"] != "https://gateway.example" {
		t.Fatalf("gateway_url must persist, got %v", decoded["gateway_url"])
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle || len(needle) == 0 ||
		indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

// TestResolveTunnelGatewayCanDifferFromREST covers the dev topology: the public
// host routes the tunnel path to the dev broker but ordinary REST to prod, so a
// dev source host needs a distinct tunnel endpoint. With no override the tunnel
// shares the REST gateway, which is the normal production shape.
func TestResolveTunnelGatewayCanDifferFromREST(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://rest.example")
	t.Setenv("MCP_SOURCE_LINK_PUBLIC_URL", "")
	t.Setenv("MCP_SOURCE_LINK_TUNNEL_URL", "https://tunnel.example")

	tunnel, err := ResolveTunnelGateway(dir)
	if err != nil {
		t.Fatalf("a distinct tunnel endpoint must be accepted: %v", err)
	}
	if tunnel != "https://tunnel.example" {
		t.Fatalf("got %q, want the explicit tunnel endpoint", tunnel)
	}
	rest, err := ResolveGateway(dir)
	if err != nil || rest != "https://rest.example" {
		t.Fatalf("REST must stay on its own endpoint, got %q err %v", rest, err)
	}
}

func TestResolveTunnelGatewayDefaultsToTheRESTGateway(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://gateway.example")
	t.Setenv("MCP_SOURCE_LINK_PUBLIC_URL", "")
	t.Setenv("MCP_SOURCE_LINK_TUNNEL_URL", "")

	tunnel, err := ResolveTunnelGateway(dir)
	if err != nil || tunnel != "https://gateway.example" {
		t.Fatalf("the tunnel must default to the REST gateway, got %q err %v", tunnel, err)
	}
}

func TestResolveTunnelGatewayRejectsPlainHTTP(t *testing.T) {
	dir := t.TempDir()
	writeBootstrap(t, dir, "https://gateway.example")
	t.Setenv("MCP_SOURCE_LINK_TUNNEL_URL", "http://tunnel.example")

	if _, err := ResolveTunnelGateway(dir); err == nil {
		t.Fatal("a plain-HTTP tunnel endpoint must be rejected, not silently downgraded")
	}
}
