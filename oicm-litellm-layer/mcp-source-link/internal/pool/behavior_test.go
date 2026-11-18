package pool_test

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/harness"
)

// Draining must stop new dispatches while keeping the session alive, so an
// operator can drain without killing in-flight work.
func TestDrainExcludesDispatchButKeepsSession(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")
	target := f.TunnelServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, p, nil)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	headers := http.Header{"Authorization": {"Bearer " + harness.ServiceKey}}
	if status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers); status != http.StatusOK || body != "A" {
		t.Fatalf("pre-drain read got %d %s", status, body)
	}

	f.SetPhase(harness.WorkerA, "draining")
	f.Runtime.Reconcile()
	harness.Eventually(t, func() bool {
		status, _ := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers)
		return status == http.StatusServiceUnavailable
	})
	if f.Runtime.Existing(harness.WorkerA) == nil {
		t.Fatal("drain closed the live session")
	}

	f.SetPhase(harness.WorkerA, "active")
	f.Runtime.Reconcile()
	harness.Eventually(t, func() bool {
		status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers)
		return status == http.StatusOK && body == "A"
	})
}
