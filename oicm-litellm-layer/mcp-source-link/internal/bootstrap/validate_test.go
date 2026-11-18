package bootstrap

import (
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// TestValidateIssuedRejectsBadIdentity proves each malformed identity shape the
// gateway could return is caught before it is persisted.
func TestValidateIssuedRejectsBadIdentity(t *testing.T) {
	good := IssuedIdentity{
		WorkerID:   "w",
		SourceID:   "sid-1",
		Target:     "127.0.0.1:18080",
		Generation: 1,
		Credential: "abababababababababababababababababababababababababababababababab",
	}
	if err := validateIssued("sid-1", good); err != nil {
		t.Fatalf("a well-formed identity must pass: %v", err)
	}
	cases := map[string]IssuedIdentity{
		"wrong source":     {WorkerID: "w", SourceID: "sid-2", Target: "127.0.0.1:18080", Generation: 1, Credential: good.Credential},
		"non-loopback":     {WorkerID: "w", SourceID: "sid-1", Target: "10.0.0.1:80", Generation: 1, Credential: good.Credential},
		"non-hex":          {WorkerID: "w", SourceID: "sid-1", Target: "127.0.0.1:18080", Generation: 1, Credential: string(make([]byte, 0)) + repeat("z", 64)},
		"short credential": {WorkerID: "w", SourceID: "sid-1", Target: "127.0.0.1:18080", Generation: 1, Credential: "abab"},
		"zero generation":  {WorkerID: "w", SourceID: "sid-1", Target: "127.0.0.1:18080", Generation: 0, Credential: good.Credential},
		"missing worker":   {WorkerID: "", SourceID: "sid-1", Target: "127.0.0.1:18080", Generation: 1, Credential: good.Credential},
	}
	for name, identity := range cases {
		if err := validateIssued("sid-1", identity); err == nil {
			t.Errorf("%s must be rejected", name)
		}
	}
}

// TestWorkerLabelSatisfiesGatewayPattern proves the derived label always matches
// the gateway rule and is deterministic.
func TestWorkerLabelSatisfiesGatewayPattern(t *testing.T) {
	for _, alias := range []string{"pets", "pets-api", "warehouse_inventory", "A", "9lives", "---"} {
		label := WorkerLabel(alias)
		if !workerLabelPattern.MatchString(label) {
			t.Errorf("alias %q produced invalid label %q", alias, label)
		}
		if WorkerLabel(alias) != label {
			t.Errorf("alias %q is not deterministic", alias)
		}
	}
	if WorkerLabel("pets-api") != "pets-api" {
		t.Error("a valid alias must pass through verbatim")
	}
}

// TestNewRejectsInsecureBase proves the REST base must be HTTPS with a key.
func TestNewRejectsInsecureBase(t *testing.T) {
	if _, err := New("http://gateway", "key", nil); err == nil {
		t.Fatal("a plain-HTTP base must be rejected")
	}
	if _, err := New("https://gateway", "", nil); err == nil {
		t.Fatal("an empty user key must be rejected")
	}
}

func repeat(value string, n int) string {
	out := make([]byte, 0, len(value)*n)
	for i := 0; i < n; i++ {
		out = append(out, value...)
	}
	return string(out)
}

var _ = config.ParseTarget
