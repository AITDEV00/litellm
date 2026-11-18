//go:build compat

// Package compat is the local old/new compatibility check for the sidecar's
// wire contract. Run it with `make -C oicm-litellm-layer mcp-source-link-compat`.
//
// It is outside the default test run and is not a CI gate. It does the one
// thing a single-version test cannot: build a connector from an earlier
// revision, point it at this checkout's broker, and prove it still admits,
// probes, and serves a read. That is what keeps a broker roll from stranding
// connectors already deployed on source hosts.
//
// The previous connector is built from a git revision through a throwaway
// worktree, so the check needs no vendored binary and no registry. Set
// MCP_SOURCE_LINK_PREVIOUS_REV to compare against a different revision.
package compat

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/harness"
)

// transitionRevision is the last commit before reverse-h2 was deleted. A
// connector built there still carries the reverse-h2 fallback, which is exactly
// the fleet the current broker has to keep serving.
const transitionRevision = "bb2f8dc7148"

func goBinary(t *testing.T) string {
	t.Helper()
	if explicit := os.Getenv("GO_BIN"); explicit != "" {
		return explicit
	}
	exe, err := exec.LookPath("go")
	if err != nil {
		t.Fatalf("no go toolchain on PATH; set GO_BIN: %v", err)
	}
	return exe
}

// buildConnectorAt compiles the connector at a revision into a scratch path.
func buildConnectorAt(t *testing.T, revision string) string {
	t.Helper()
	tree := filepath.Join(t.TempDir(), "src")
	if out, err := exec.Command("git", "worktree", "add", "--detach", tree, revision).CombinedOutput(); err != nil {
		t.Fatalf("worktree add %s: %v\n%s", revision, err, out)
	}
	t.Cleanup(func() { _ = exec.Command("git", "worktree", "remove", "--force", tree).Run() })

	out := filepath.Join(t.TempDir(), "source-link")
	build := exec.Command(goBinary(t), "build", "-o", out, ".")
	build.Dir = filepath.Join(tree, "oicm-litellm-layer", "mcp-source-link")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if combined, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build connector at %s: %v\n%s", revision, err, combined)
	}
	return out
}

// connectorIdentity writes the owner-only identity file ReadConnector requires.
func connectorIdentity(t *testing.T, peer config.Peer, target string) string {
	t.Helper()
	identity := config.ConnectorConfig{
		WorkerID:   peer.WorkerID,
		SourceID:   peer.SourceID,
		Target:     target,
		Generation: peer.Generation,
		Credential: harness.NotSecret,
	}
	body, err := json.Marshal(identity)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "connector.json")
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// runConnector starts a connector build against the TLS gateway and stops it
// when the test ends.
func runConnector(t *testing.T, binary, gateway, caPath string, peer config.Peer, target string) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, binary, "connector")
	cmd.Env = append(os.Environ(),
		"MCP_SOURCE_LINK_PUBLIC_URL="+gateway,
		"MCP_SOURCE_LINK_CA_FILE="+caPath,
		"MCP_SOURCE_LINK_CONNECTOR_CONFIG="+connectorIdentity(t, peer, target),
		// The revision under test predates tunnel-only connectors and still
		// selects reverse-h2 when this is unset. Pin it so the check compares
		// the tunnel contract rather than the old default.
		"MCP_SOURCE_LINK_TRANSPORT=tunnel",
	)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start connector: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		_ = cmd.Wait()
	})
}

// runCurrentConnector starts THIS checkout's connector against the fixture gateway
// using an isolated multi-account state directory, so the control run serves only
// the fixture source and never reads the developer's ambient ~/.config/source-agent.
//
// The current binary reads accounts.json/sources.json (the multi-account model),
// not MCP_SOURCE_LINK_CONNECTOR_CONFIG, so without an isolated state dir it would
// pick up whatever sources the developer registered locally and dial their
// gateways. The seeded source carries exactly the fixture peer's worker identity,
// target and kind, so the tunnel admits as the same member.
func runCurrentConnector(t *testing.T, binary, gateway, caPath string, peer config.Peer) {
	t.Helper()
	stateDir := t.TempDir()
	seedCurrentState(t, stateDir, gateway, peer)
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, binary, "connector")
	cmd.Env = append(os.Environ(),
		"MCP_SOURCE_LINK_STATE_DIR="+stateDir,
		"MCP_SOURCE_LINK_TUNNEL_URL="+gateway,
		"MCP_SOURCE_LINK_CA_FILE="+caPath,
		"MCP_SOURCE_LINK_TRANSPORT=tunnel",
	)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start current connector: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		_ = cmd.Wait()
	})
}

// seedCurrentState writes the current multi-account state layout for one source:
// accounts.json, the owner-only account key, and sources.json with a registered
// source whose identity matches peer.
func seedCurrentState(t *testing.T, stateDir, gateway string, peer config.Peer) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(stateDir, "accounts"), 0o700); err != nil {
		t.Fatal(err)
	}
	accounts := map[string]any{
		"accounts": []map[string]any{{"alias": "alpha", "gateway_url": gateway, "username": "owner@example.invalid"}},
	}
	writeJSON(t, filepath.Join(stateDir, "accounts.json"), accounts)
	if err := os.WriteFile(filepath.Join(stateDir, "accounts", "alpha.key"), []byte(harness.NotSecret), 0o600); err != nil {
		t.Fatal(err)
	}
	source := map[string]any{
		"sources": []map[string]any{{
			"name": "alpha.fixture", "account": "alpha", "target": peer.Target,
			"kind": peer.Kind, "path": "/mcp", "worker_id": peer.WorkerID,
			"source_id": peer.SourceID, "generation": peer.Generation,
			"credential": harness.NotSecret, "enabled": true,
		}},
	}
	writeJSON(t, filepath.Join(stateDir, "sources.json"), source)
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
}

// TestPreviousConnectorServesCurrentBroker is the forward-compatibility proof: a
// connector built before the transition still admits, probes, and serves a real
// source read against this checkout's broker.
func TestPreviousConnectorServesCurrentBroker(t *testing.T) {
	revision := transitionRevision
	if override := os.Getenv("MCP_SOURCE_LINK_PREVIOUS_REV"); override != "" {
		revision = override
	}
	binary := buildConnectorAt(t, revision)

	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	gateway, caPath := f.TLSGateway(t)

	peer := harness.Peer(harness.WorkerA, 1, pub)
	peer.Target = strings.TrimPrefix(origin.URL, "http://")
	peer.ExpectedCatalog = digest
	f.SetPeer(peer, "")

	runConnector(t, binary, gateway, caPath, peer, peer.Target)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	headers := http.Header{"Authorization": {"Bearer " + harness.ServiceKey}, "Cookie": {"private=not-forwarded"}}
	status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("previous connector read got %d %q", status, body)
	}
}

// TestCurrentConnectorServesCurrentBroker is the control for the comparison. It
// runs the same scenario with the connector from this checkout, so a failure in
// the previous-connector case can be attributed to the version gap rather than
// to the harness. Serving a broker built from the older revision is the same
// assertion in the other direction: the proto is unchanged between them, so
// only the connector binary differs.
func TestCurrentConnectorServesCurrentBroker(t *testing.T) {
	binary := buildConnectorAt(t, "HEAD")

	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	gateway, caPath := f.TLSGateway(t)

	peer := harness.Peer(harness.WorkerA, 1, pub)
	peer.Target = strings.TrimPrefix(origin.URL, "http://")
	peer.ExpectedCatalog = digest
	f.SetPeer(peer, "")

	runCurrentConnector(t, binary, gateway, caPath, peer)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	headers := http.Header{"Authorization": {"Bearer " + harness.ServiceKey}}
	status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("current connector read got %d %q", status, body)
	}
}

// TestConnectorDegradesWhenBrokerLacksCapability proves the negotiation surface
// works the way mixed fleets need: a connector that advertises only "execute"
// still admits onto the broker, and the broker simply never pushes the probe it
// cannot serve. If capability gating ever hard-failed the tunnel instead,
// every older connector would be refused rather than degraded.
func TestConnectorDegradesWhenBrokerLacksCapability(t *testing.T) {
	previous := connector.Capabilities
	connector.Capabilities = []string{"execute"}
	t.Cleanup(func() { connector.Capabilities = previous })

	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	target := f.TunnelServer(t)
	peer := harness.Peer(harness.WorkerA, 1, pub)
	peer.Target = strings.TrimPrefix(origin.URL, "http://")
	peer.ExpectedCatalog = digest
	f.SetPeer(peer, "")

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, peer, nil)

	// Admission does not depend on "probe": the member registers regardless, and
	// a probe-less connector is simply never marked verified.
	harness.Eventually(t, func() bool { return f.Runtime.Existing(harness.WorkerA) != nil })
	if f.ReadyTunnel(t, harness.WorkerA) {
		t.Fatal("a connector that cannot probe must not be marked verified")
	}
}
