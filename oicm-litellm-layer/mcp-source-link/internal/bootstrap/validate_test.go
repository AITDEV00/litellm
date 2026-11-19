package bootstrap

import (
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/state"
	"oicm-litellm-layer/mcp-source-link/internal/target"
)

func mustTarget(t *testing.T, value string) target.Target {
	t.Helper()
	parsed, err := target.Parse(value)
	if err != nil {
		t.Fatalf("parse target %q: %v", value, err)
	}
	return parsed
}

// TestValidateIssuedRejectsBadIdentity proves each malformed identity shape the
// gateway could return is caught before it is persisted.
//
// A private IPv4 target is now valid: a source may be a service or host off
// loopback, so only a malformed target is rejected.
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
	private := good
	private.Target = "10.34.104.99:4000"
	if err := validateIssued("sid-1", private); err != nil {
		t.Fatalf("a private IPv4 target must be accepted: %v", err)
	}
	cases := map[string]IssuedIdentity{
		"wrong source":     {WorkerID: "w", SourceID: "sid-2", Target: "127.0.0.1:18080", Generation: 1, Credential: good.Credential},
		"malformed target": {WorkerID: "w", SourceID: "sid-1", Target: "not a host", Generation: 1, Credential: good.Credential},
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

// Two distinct full-length source names must not collide on one worker label.
//
// The source name ends in a digest that separates otherwise-similar long targets
// (two full IPv6 addresses, two long service names sharing a prefix). The old
// WorkerLabel truncated to 32 characters and cut that digest off, so both sources
// derived the same label and the server rejected the second registration. The
// label must stay grammar-valid, deterministic, and distinct.
func TestWorkerLabelDistinguishesFullLengthIdentities(t *testing.T) {
	// Derive names exactly as the connector does, from full authoritative targets.
	fullV6A := mustTarget(t, "[2001:db8:1:2:3:4:5:6]:9001")
	fullV6B := mustTarget(t, "[2001:db8:1:2:3:4:5:7]:9001")
	names := []string{
		state.DeriveName("alpha", fullV6A),
		state.DeriveName("alpha", fullV6B),
		"alpha.inventory-service-with-a-shared-prefix-one.svc.9001.0000000001",
		"alpha.inventory-service-with-a-shared-prefix-two.svc.9001.0000000002",
	}
	seen := map[string]string{}
	for _, name := range names {
		label := WorkerLabel(name)
		if !workerLabelPattern.MatchString(label) {
			t.Fatalf("label %q for name %q violates the gateway grammar", label, name)
		}
		if len(label) > 32 {
			t.Fatalf("label %q is %d chars, want <= 32", label, len(label))
		}
		if previous, ok := seen[label]; ok {
			t.Fatalf("distinct names %q and %q collide on label %q", previous, name, label)
		}
		seen[label] = name
		if WorkerLabel(name) != label {
			t.Fatalf("label for %q is not deterministic", name)
		}
	}
}

// A short name that already fits the gateway grammar must pass through verbatim,
// so existing sources keep their issued label on resume.
func TestWorkerLabelPreservesExistingShortLabels(t *testing.T) {
	for _, short := range []string{"pets-api", "alpha-9001", "ward-01"} {
		if got := WorkerLabel(short); got != short {
			t.Fatalf("short label %q became %q; legacy labels must not change", short, got)
		}
	}
}
