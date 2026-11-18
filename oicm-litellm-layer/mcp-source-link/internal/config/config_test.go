package config

import (
	"os"
	"path/filepath"
	"testing"
)

// The connector identity file is the one place a credential is read from disk,
// so its validation rules are load-bearing: owner-only permissions, a numeric
// loopback target, and a 64-character hex credential.
func TestReadConnectorValidation(t *testing.T) {
	valid := `{"worker_id":"w1","source_id":"s1","target":"127.0.0.1:8080","generation":1,` +
		`"credential":"` + hexCredential() + `"}`

	write := func(t *testing.T, contents string, perm os.FileMode) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), "connector.json")
		if err := os.WriteFile(path, []byte(contents), perm); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, perm); err != nil {
			t.Fatal(err)
		}
		return path
	}

	t.Run("valid file loads", func(t *testing.T) {
		cfg, err := ReadConnector(write(t, valid, 0o600))
		if err != nil {
			t.Fatalf("valid connector rejected: %v", err)
		}
		if cfg.WorkerID != "w1" || cfg.Target != "127.0.0.1:8080" {
			t.Fatalf("fields not loaded: %+v", cfg)
		}
	})

	for _, testCase := range []struct {
		name     string
		contents string
		perm     os.FileMode
	}{
		{"group-readable is refused", valid, 0o640},
		{"world-readable is refused", valid, 0o604},
		{"non-loopback target is refused", `{"worker_id":"w1","source_id":"s1","target":"10.0.0.1:8080","generation":1,"credential":"` + hexCredential() + `"}`, 0o600},
		{"zero port is refused", `{"worker_id":"w1","source_id":"s1","target":"127.0.0.1:0","generation":1,"credential":"` + hexCredential() + `"}`, 0o600},
		{"missing generation is refused", `{"worker_id":"w1","source_id":"s1","target":"127.0.0.1:8080","credential":"` + hexCredential() + `"}`, 0o600},
		{"short credential is refused", `{"worker_id":"w1","source_id":"s1","target":"127.0.0.1:8080","generation":1,"credential":"abcd"}`, 0o600},
		{"non-hex credential is refused", `{"worker_id":"w1","source_id":"s1","target":"127.0.0.1:8080","generation":1,"credential":"` + nonHexCredential() + `"}`, 0o600},
		{"missing worker id is refused", `{"source_id":"s1","target":"127.0.0.1:8080","generation":1,"credential":"` + hexCredential() + `"}`, 0o600},
		{"malformed json is refused", `{`, 0o600},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := ReadConnector(write(t, testCase.contents, testCase.perm)); err == nil {
				t.Fatal("accepted an invalid connector file")
			}
		})
	}

	t.Run("missing file is refused", func(t *testing.T) {
		if _, err := ReadConnector(filepath.Join(t.TempDir(), "absent.json")); err == nil {
			t.Fatal("accepted a missing file")
		}
	})
}

// Load must reject configurations that would leave the worker unreachable or
// unauthenticated.
func TestLoadRequiresControlURLTokenAndListeners(t *testing.T) {
	t.Setenv("MCP_SOURCE_LINK_CONTROL_URL", "https://control.invalid")
	t.Setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "0123456789abcdef0123456789abcdef")
	t.Setenv("MCP_SOURCE_LINK_DATA_LISTEN", "0.0.0.0:9092")
	t.Setenv("MCP_SOURCE_LINK_REVERSE_LISTEN", "0.0.0.0:8093")
	t.Setenv("MCP_SOURCE_LINK_TRANSPORT", TransportReverseH2)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("valid environment rejected: %v", err)
	}
	if cfg.DataListen != "0.0.0.0:9092" || cfg.Transport != TransportReverseH2 {
		t.Fatalf("fields not loaded: %+v", cfg)
	}

	t.Setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "too-short")
	if _, err := Load(); err == nil {
		t.Fatal("accepted a short service token")
	}

	t.Setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "0123456789abcdef0123456789abcdef")
	t.Setenv("MCP_SOURCE_LINK_DATA_LISTEN", "localhost:9092")
	if _, err := Load(); err == nil {
		t.Fatal("accepted a non-numeric listener address")
	}

	t.Setenv("MCP_SOURCE_LINK_DATA_LISTEN", "0.0.0.0:9092")
	t.Setenv("MCP_SOURCE_LINK_TRANSPORT", "carrier-pigeon")
	if _, err := Load(); err == nil {
		t.Fatal("accepted an unknown transport")
	}
}

func TestEnvironmentTrimsAndTreatsBlankAsUnset(t *testing.T) {
	t.Setenv("MCP_TEST_VALUE", "  spaced  ")
	if got := Environment("MCP_TEST_VALUE"); got != "spaced" {
		t.Fatalf("Environment did not trim: %q", got)
	}
	t.Setenv("MCP_TEST_VALUE", "   ")
	if got := Environment("MCP_TEST_VALUE"); got != "" {
		t.Fatalf("blank value not treated as unset: %q", got)
	}
}

func TestSlotIsStableAndSourceDerived(t *testing.T) {
	if Slot("s1") != "s1.source.invalid:1" {
		t.Fatalf("slot format changed: %q", Slot("s1"))
	}
	if Slot("s1") == Slot("s2") {
		t.Fatal("different sources share a slot")
	}
}

func hexCredential() string {
	return "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
func nonHexCredential() string {
	return "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
}
