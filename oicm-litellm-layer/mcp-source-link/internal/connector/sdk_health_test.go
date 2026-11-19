package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

// startSDKServer launches the installed MCP Python SDK's StreamableHTTP server as a
// local fixture and returns its base URL plus a stop function. It skips the test
// when the Python interpreter or SDK is unavailable, so the package still builds on
// a machine without the venv.
func startSDKServer(t *testing.T) (string, func()) {
	return startSDKServerMode(t, "")
}

func startSDKServerMode(t *testing.T, rejectNotification string) (string, func()) {
	t.Helper()
	python := os.Getenv("MCP_HEALTH_PYTHON")
	if python == "" {
		python = findRepoPython()
	}
	if python == "" {
		t.Skip("SDK-backed health fixture skipped: no repo .venv/bin/python with the mcp SDK found")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	script := filepath.Join("testdata", "sdk_health_server.py")
	command := exec.Command(python, script)
	command.Env = append(os.Environ(), fmt.Sprintf("MCP_PORT=%d", port))
	if rejectNotification != "" {
		command.Env = append(command.Env, fmt.Sprintf("MCP_REJECT_NOTIFICATION=%s", rejectNotification))
	}
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		t.Fatalf("start SDK server: %v", err)
	}
	stop := func() {
		_ = command.Process.Kill()
		_, _ = command.Process.Wait()
	}
	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return base, stop
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	stop()
	t.Fatalf("SDK server did not become healthy at %s", base)
	return "", func() {}
}

// sdkSessions reads the fixture's live session count.
func sdkSessions(t *testing.T, base string) int {
	t.Helper()
	resp, err := http.Get(base + "/__status")
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	defer resp.Body.Close()
	var body struct {
		Sessions int `json:"sessions"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode status: %v", err)
	}
	return body.Sessions
}

// The real SDK success path: three health probes pool exactly one session, and
// Close deletes it, leaving zero. This is the positive lifecycle the deterministic
// fixture cannot prove against a real protocol implementation.
func TestSDKHealthPoolsOneSessionAndCloses(t *testing.T) {
	base, stop := startSDKServer(t)
	defer stop()
	server := NewServer(http.DefaultTransport, "127.0.0.1", "mcp", "")
	// Point healthURL at the fixture by shaping the Server; NewServer takes
	// authority, so rebuild the health URL target through authority+path.
	server.authority = authorityOfURL(t, base)
	server.path = "/mcp"

	for probe := 0; probe < 3; probe++ {
		if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
			t.Fatalf("probe %d failed: %v", probe, err)
		}
		if got := sdkSessions(t, base); got != 1 {
			t.Fatalf("after probe %d the SDK reports %d sessions, want 1", probe, got)
		}
	}

	// A separate real SDK client must still complete a full lifecycle on the same
	// server the health client polls: initialize, list tools, call one tool, and
	// terminate. Its session must not disturb the pooled health session, and after
	// it closes only the health session may remain.
	resp, err := http.Post(base+"/__client", "application/json", nil)
	if err != nil {
		t.Fatalf("client lifecycle: %v", err)
	}
	defer resp.Body.Close()
	var lifecycle struct {
		Tools    []string `json:"tools"`
		Content  []string `json:"content"`
		Sessions int      `json:"sessions"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&lifecycle); err != nil {
		t.Fatalf("decode client lifecycle: %v", err)
	}
	if len(lifecycle.Tools) != 1 || lifecycle.Tools[0] != "echo" {
		t.Fatalf("real client listed tools %v, want [echo]", lifecycle.Tools)
	}
	if len(lifecycle.Content) != 1 || !strings.Contains(lifecycle.Content[0], "world") {
		t.Fatalf("real client tool call returned %v, want echoed content", lifecycle.Content)
	}
	if lifecycle.Sessions != 1 {
		t.Fatalf("after the client terminated the SDK reports %d sessions, want the 1 pooled health session", lifecycle.Sessions)
	}
	if got := sdkSessions(t, base); got != 1 {
		t.Fatalf("after the client lifecycle the SDK reports %d sessions, want 1", got)
	}

	server.CloseHealthSession(context.Background())
	if got := sdkSessions(t, base); got != 0 {
		t.Fatalf("after close the SDK reports %d sessions, want 0", got)
	}
}

// The real SDK failure path: initialize is admitted and allocates a session,
// then the completing notification is refused. The probe must report unhealthy
// and the allocated session must not survive; CloseHealthSession on a probe
// that never stored a session must not leak the one the server kept.
func TestSDKRejectedNotificationLeavesNoSession(t *testing.T) {
	base, stop := startSDKServerMode(t, "1")
	defer stop()
	server := NewServer(http.DefaultTransport, "127.0.0.1", "mcp", "")
	server.authority = authorityOfURL(t, base)
	server.path = "/mcp"

	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("probe succeeded against a server that rejects the initialized notification, want unhealthy")
	}
	if got := sdkSessions(t, base); got != 0 {
		t.Fatalf("after the rejected handshake the SDK reports %d sessions, want 0", got)
	}
	server.CloseHealthSession(context.Background())
	if got := sdkSessions(t, base); got != 0 {
		t.Fatalf("after close the SDK reports %d sessions, want 0", got)
	}
}

func authorityOfURL(t *testing.T, raw string) string {
	t.Helper()
	parsed, err := http.NewRequest(http.MethodGet, raw, nil)
	if err != nil {
		t.Fatalf("parse %q: %v", raw, err)
	}
	return parsed.URL.Host
}

// findRepoPython walks up from the test's working directory to the repository root
// and returns the first .venv/bin/python that can import the mcp SDK, so the fixture
// uses the interpreter that actually has the SDK rather than a sibling Go venv.
func findRepoPython() string {
	dir, err := os.Getwd()
	if err != nil {
		return ""
	}
	for {
		candidate := filepath.Join(dir, ".venv", "bin", "python")
		if _, statErr := os.Stat(candidate); statErr == nil {
			probe := exec.Command(candidate, "-c", "import mcp, uvicorn")
			if probe.Run() == nil {
				return candidate
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}
