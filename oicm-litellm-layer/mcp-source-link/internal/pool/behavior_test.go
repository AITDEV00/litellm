package pool_test

import (
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
	f.StartSession(t, p)
	harness.Eventually(t, func() bool { return f.ReadyReverse(t, harness.WorkerA) })

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

// A revoked connector must not re-register with its old credential or its old
// generation, which is what makes revoke final rather than advisory.
func TestRevokedConnectorCannotReconnect(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")
	server := f.ReverseServer(t)
	f.StartSession(t, p)
	harness.Eventually(t, func() bool { return f.ReadyReverse(t, harness.WorkerA) })

	f.RevokePeer(harness.WorkerA)
	f.Runtime.Reconcile()
	harness.Eventually(t, func() bool { return f.Runtime.Existing(harness.WorkerA) == nil })

	probe := func(generation, credential string) int {
		req, _ := http.NewRequest(http.MethodPost, server.URL+"/mcp-source-link-reverse", nil)
		req.Header.Set("X-Source-Member", harness.WorkerA)
		req.Header.Set("X-Source-Generation", generation)
		req.Header.Set("Authorization", "Bearer "+credential)
		resp, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	if code := probe("2", "stale-credential"); code != http.StatusUnauthorized {
		t.Fatalf("revoked member re-registered: %d", code)
	}
	if code := probe("1", harness.NotSecret); code != http.StatusUnauthorized {
		t.Fatalf("stale generation re-registered: %d", code)
	}
}
