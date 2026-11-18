package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

// tunnelFixture stands up the worker's tunnel plane exactly as production
// wires it: one h2c listener multiplexing the tunnel gRPC service, plus the
// shared control/data fixture.
func tunnelFixture(t *testing.T) (*fixture, *tunnelRuntime, string) {
	t.Helper()
	f := setupFixture(t)
	tr := newTunnelRuntime(f.runtime)
	grpcServer := tr.grpcServer()
	server := reverseServer()
	server.Handler = tunnelHTTPHandler(grpcServer, http.NewServeMux())
	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	protocols.SetHTTP1(false)
	server.Protocols = protocols
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	t.Cleanup(tr.closeAll)
	go func() { _ = server.Serve(listener) }()
	return f, tr, listener.Addr().String()
}

// tunnelConnector connects one connector to the tunnel fixture and serves
// pushed RPCs until the context ends, mirroring runTunnelConnector's
// session loop: every session end is followed by the capped backoff, so a
// rejected re-registration never races the test's completion.
func tunnelConnector(ctx context.Context, t *testing.T, target string, p peer) {
	t.Helper()
	tunnelConnectorWithOrigin(ctx, t, target, p, nil)
}

func tunnelConnectorWithOrigin(ctx context.Context, t *testing.T, target string, p peer, origin *http.Transport) {
	t.Helper()
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Errorf("connector dial failed: %v", err)
		return
	}
	t.Cleanup(func() { _ = conn.Close() })
	if origin == nil {
		origin = &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", p.Target)
			},
			DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
		}
		t.Cleanup(origin.CloseIdleConnections)
	}
	serverImpl := &dispatchServer{origin: origin}
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, serverImpl)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", notSecret,
	)
	for ctx.Err() == nil {
		_, _ = tunnelServer.Serve(sessionCtx)
		if ctx.Err() != nil {
			return
		}
		timer := time.NewTimer(300 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
}

func readyTunnel(t *testing.T, f *fixture, id string) bool {
	t.Helper()
	return readyReverse(t, f, id)
}

func TestTunnelForwardingAndRevoke(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, tr, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go tunnelConnector(ctx, t, target, p)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}, "Cookie": {"private=not-forwarded"}}
	status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("tunnel read got %d %s", status, body)
	}
	f.revokePeer(workerA)
	f.runtime.reconcile()
	eventually(t, func() bool { return !f.runtime.hasActive(sourceID) })
	eventually(t, func() bool {
		tr.mu.Lock()
		defer tr.mu.Unlock()
		return len(tr.members) == 0
	})
}

func TestTunnelRejectsBadCredentials(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	f.setPeer(p, "")
	for _, tc := range []struct {
		name       string
		member     string
		generation string
		credential string
	}{
		{"wrong credential", workerA, "1", "wrong-credential"},
		{"unknown member", "unknown", "1", notSecret},
		{"stale generation", workerA, "2", notSecret},
	} {
		t.Run(tc.name, func(t *testing.T) {
			conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = conn.Close() }()
			tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
			dispatchpb.RegisterDispatchServiceServer(tunnelServer, &dispatchServer{})
			ctx := metadata.AppendToOutgoingContext(context.Background(),
				"x-source-member", tc.member,
				"x-source-generation", tc.generation,
				"authorization", tc.credential,
			)
			started, err := tunnelServer.Serve(ctx)
			if err == nil || status.Code(err) != codes.Unauthenticated {
				t.Fatalf("bad credential admitted: started=%v err=%v", started, err)
			}
		})
	}
	f.runtime.mu.Lock()
	count := len(f.runtime.sessions)
	f.runtime.mu.Unlock()
	if count != 0 {
		t.Fatalf("unauthorized tunnel registered a session: %d", count)
	}
}

func TestTunnelDuplicateRegistrationRejected(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go tunnelConnector(ctx, t, target, p)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, &dispatchServer{})
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", notSecret,
	)
	started, err := tunnelServer.Serve(sessionCtx)
	if err == nil {
		t.Fatalf("duplicate tunnel admitted: started=%v err=%v", started, err)
	}
	if !readyTunnel(t, f, workerA) {
		t.Fatal("duplicate attempt displaced the original session")
	}
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("original session stopped serving after duplicate attempt: %d %s", status, body)
	}
}

func TestTunnelProbeFencesRevokedGeneration(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, tr, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go tunnelConnector(ctx, t, target, p)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })
	f.revokePeer(workerA)
	eventually(t, func() bool {
		tr.mu.Lock()
		defer tr.mu.Unlock()
		return len(tr.members) == 0
	})
	f.runtime.mu.Lock()
	remaining := len(f.runtime.sessions)
	f.runtime.mu.Unlock()
	if remaining != 0 {
		t.Fatalf("fenced tunnel remained registered: %d", remaining)
	}
}

func TestTunnelCloseCancelsInFlightOriginRequests(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	f, tr, target := tunnelFixture(t)
	p := testPeer(workerA, 1, signing.Public().(ed25519.PublicKey))
	origin, catalog := signedOrigin(t, "A", signing)
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	originStarted := make(chan struct{})
	originCanceled := make(chan error, 1)
	handler := &stalledProbeDispatch{origin: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "tcp", strings.TrimPrefix(origin.URL, "http://"))
		},
	}, execute: func(ctx context.Context, _ *dispatchpb.DispatchRequest) (*dispatchpb.DispatchResponse, error) {
		close(originStarted)
		<-ctx.Done()
		originCanceled <- ctx.Err()
		return nil, ctx.Err()
	}}
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, handler)
	sessionCtx := metadata.AppendToOutgoingContext(context.Background(),
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", notSecret,
	)
	go func() { _, _ = tunnelServer.Serve(sessionCtx) }()
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })
	tr.mu.Lock()
	member := tr.members[workerA]
	tr.mu.Unlock()
	request, err := http.NewRequest(http.MethodGet, "http://target.source.invalid/read", nil)
	if err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() {
		_, err := member.roundTrip(request)
		result <- err
	}()
	select {
	case <-originStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("origin request did not arrive")
	}
	member.close()
	if err := <-result; !errors.Is(err, context.Canceled) && status.Code(err) != codes.Canceled {
		t.Fatalf("closing member did not cancel in-flight request: %v", err)
	}
	if err := <-originCanceled; !errors.Is(err, context.Canceled) {
		t.Fatalf("origin did not receive cancellation: %v", err)
	}
}

type originRoundTripFunc func(*http.Request) (*http.Response, error)

func (f originRoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestDispatchCancellationReachesOriginTransport(t *testing.T) {
	var calls int
	server := &dispatchServer{origin: originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		calls++
		return nil, req.Context().Err()
	})}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := server.Execute(ctx, &dispatchpb.DispatchRequest{Method: http.MethodGet, Path: "/read"})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("origin cancellation not propagated: %v", err)
	}
	if calls != 1 || server.roundTripErrors.Load() != 1 {
		t.Fatalf("unexpected cancellation counts: calls=%d errors=%d", calls, server.roundTripErrors.Load())
	}
}

func TestGRPCDialTargetStripsSchemeAndDefaultsPort(t *testing.T) {
	for _, tc := range []struct {
		gateway string
		want    string
	}{
		{"https://litellm.ecouncil.ae", "litellm.ecouncil.ae:443"},
		{"https://litellm.ecouncil.ae/", "litellm.ecouncil.ae:443"},
		{"https://litellm.ecouncil.ae:8443", "litellm.ecouncil.ae:8443"},
		{"https://litellm.ecouncil.ae/base/path", "litellm.ecouncil.ae:443"},
	} {
		got, err := grpcDialTarget(tc.gateway)
		if err != nil {
			t.Fatalf("%s: %v", tc.gateway, err)
		}
		if got != tc.want {
			t.Fatalf("%s: got %q want %q", tc.gateway, got, tc.want)
		}
	}
	if _, err := grpcDialTarget("not a url"); err == nil {
		t.Fatal("expected an error for an unparsable gateway URL")
	}
}
