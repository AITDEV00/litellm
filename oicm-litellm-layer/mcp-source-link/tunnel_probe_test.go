package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

type stalledProbeDispatch struct {
	dispatchpb.UnimplementedDispatchServiceServer
	origin *http.Transport
	stall  atomic.Bool
	calls  atomic.Int64
}

func (d *stalledProbeDispatch) Admit(context.Context, *dispatchpb.AdmitRequest) (*dispatchpb.AdmitResponse, error) {
	return &dispatchpb.AdmitResponse{Capabilities: []string{"execute", "probe"}}, nil
}

func (d *stalledProbeDispatch) Probe(ctx context.Context, req *dispatchpb.ProbeRequest) (*dispatchpb.ProbeResponse, error) {
	d.calls.Add(1)
	if d.stall.Load() {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return (&dispatchServer{origin: d.origin}).Probe(ctx, req)
}

func (d *stalledProbeDispatch) Execute(ctx context.Context, req *dispatchpb.DispatchRequest) (*dispatchpb.DispatchResponse, error) {
	return (&dispatchServer{origin: d.origin}).Execute(ctx, req)
}

func probeStallFixture(t *testing.T) (*fixture, *stalledProbeDispatch) {
	t.Helper()
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	origin, catalog := signedOrigin(t, "A", signing)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, signing.Public().(ed25519.PublicKey))
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "tcp", p.Target)
		},
	}
	t.Cleanup(transport.CloseIdleConnections)
	stub := &stalledProbeDispatch{origin: transport}
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, stub)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", notSecret,
	)
	go func() { _, _ = tunnelServer.Serve(sessionCtx) }()
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })
	return f, stub
}

func tunnelRead(t *testing.T, f *fixture) (int, string) {
	t.Helper()
	return read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, http.Header{"Authorization": {"Bearer " + serviceKey}})
}

func TestTunnelRetainsCatalogDuringTransientProbeTimeout(t *testing.T) {
	f, stub := probeStallFixture(t)
	stub.stall.Store(true)
	eventually(t, func() bool { return stub.calls.Load() > 1 })
	if status, body := tunnelRead(t, f); status != http.StatusOK || body != "A" {
		t.Fatalf("probe failure removed valid catalog: status=%d body=%s", status, body)
	}
	f.runtime.mu.Lock()
	member := f.runtime.sessions[workerA].(*tunnelMember)
	f.runtime.mu.Unlock()
	member.mu.Lock()
	member.lastProbe = member.lastProbe.Add(-2 * tunnelHealthGracePeriod)
	member.mu.Unlock()
	if status, _ := tunnelRead(t, f); status != http.StatusServiceUnavailable {
		t.Fatalf("expired probe verification remained eligible: status=%d", status)
	}
	stub.stall.Store(false)
	eventually(t, func() bool {
		status, body := tunnelRead(t, f)
		return status == http.StatusOK && body == "A"
	})
}

func TestTunnelMismatchStillRevokesHealth(t *testing.T) {
	f, _ := probeStallFixture(t)
	wrongCatalog, err := canonicalCatalog([]byte(`{"openapi":"3.1.0","info":{},"paths":{"/read":{"get":{}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	f.control.mu.Lock()
	worker := f.control.workers[workerA]
	worker.ExpectedCatalog = wrongCatalog
	f.control.workers[workerA] = worker
	f.control.mu.Unlock()
	eventually(t, func() bool {
		status, _ := tunnelRead(t, f)
		return status == http.StatusServiceUnavailable
	})
}

func TestGenerationOnlyFenceRejectsExistingSession(t *testing.T) {
	f, _ := probeStallFixture(t)
	f.control.mu.Lock()
	worker := f.control.workers[workerA]
	worker.Phase = "revoked"
	worker.Generation++
	f.control.workers[workerA] = worker
	f.control.mu.Unlock()
	if f.runtime.pick(sourceID, "") != nil {
		t.Fatal("revoked generation remained selectable before async reconcile")
	}
	f.runtime.reconcile()
	eventually(t, func() bool {
		f.runtime.mu.Lock()
		defer f.runtime.mu.Unlock()
		return f.runtime.sessions[workerA] == nil
	})
}
