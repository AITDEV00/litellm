package main

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
)

func TestTunnelForwardingAndRevoke(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, tunnels, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go runTunnelConnector(ctx, t, target, p, nil)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })

	headers := http.Header{"Authorization": {"Bearer " + serviceKey}, "Cookie": {"private=not-forwarded"}}
	status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("tunnel read got %d %s", status, body)
	}

	f.revokePeer(workerA)
	f.runtime.Reconcile()
	eventually(t, func() bool {
		return f.runtime.Existing(workerA) == nil
	})
	tunnels.CloseAll()
}

func TestTunnelRejectsBadCredentials(t *testing.T) {
	pub, _ := generateKey(t)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	f.setPeer(p, "")
	for _, testCase := range []struct {
		name       string
		member     string
		generation string
		credential string
	}{
		{"wrong credential", workerA, "1", "wrong-credential"},
		{"unknown member", "unknown", "1", notSecret},
		{"stale generation", workerA, "2", notSecret},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = conn.Close() }()
			tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
			dispatchpb.RegisterDispatchServiceServer(tunnelServer, connector.NewServer(http.DefaultTransport))
			ctx := metadata.AppendToOutgoingContext(context.Background(),
				"x-source-member", testCase.member,
				"x-source-generation", testCase.generation,
				"authorization", testCase.credential,
			)
			started, err := tunnelServer.Serve(ctx)
			if err == nil || status.Code(err) != codes.Unauthenticated {
				t.Fatalf("bad credential admitted: started=%v err=%v", started, err)
			}
		})
	}
	if sessions := f.runtime.Sessions(); len(sessions) != 0 {
		t.Fatalf("unauthorized tunnel registered a session: %d", len(sessions))
	}
}

func TestTunnelDuplicateRegistrationRejected(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go runTunnelConnector(ctx, t, target, p, nil)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })

	// A second tunnel for the same worker must be refused, so one connector
	// cannot silently own two members.
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, connector.NewServer(http.DefaultTransport))
	dupCtx := metadata.AppendToOutgoingContext(context.Background(),
		"x-source-member", workerA,
		"x-source-generation", "1",
		"authorization", notSecret,
	)
	started, err := tunnelServer.Serve(dupCtx)
	if err == nil && started {
		t.Fatal("a duplicate tunnel was admitted for the same worker")
	}
	if sessions := f.runtime.Sessions(); len(sessions) > 1 {
		t.Fatalf("duplicate registration produced %d sessions", len(sessions))
	}
}

// A revoked generation must fence the live tunnel even if the session is
// otherwise healthy, which is what makes revoke immediate.
func TestTunnelProbeFencesRevokedGeneration(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go runTunnelConnector(ctx, t, target, p, nil)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })

	f.revokePeer(workerA)
	eventually(t, func() bool { return f.runtime.Existing(workerA) == nil })
}
