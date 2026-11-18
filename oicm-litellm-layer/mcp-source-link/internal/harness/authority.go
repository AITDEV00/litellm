package harness

import (
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// authorityOf returns the Host header a test origin should receive for a target
// string, so the dispatch server and the test origin agree on it.
func authorityOf(t *testing.T, value string) string {
	t.Helper()
	parsed, err := target.Parse(value)
	if err != nil {
		t.Fatalf("target %q: %v", value, err)
	}
	return parsed.Authority()
}
