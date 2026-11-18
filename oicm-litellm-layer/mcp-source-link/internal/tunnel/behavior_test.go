package tunnel_test

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
	"oicm-litellm-layer/mcp-source-link/internal/harness"
)

func TestTunnelForwardingAndRevoke(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, p, nil)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	headers := http.Header{"Authorization": {"Bearer " + harness.ServiceKey}, "Cookie": {"private=not-forwarded"}}
	status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("tunnel read got %d %s", status, body)
	}

	f.RevokePeer(harness.WorkerA)
	f.Runtime.Reconcile()
	harness.Eventually(t, func() bool { return f.Runtime.Existing(harness.WorkerA) == nil })
}

func TestTunnelRejectsBadCredentials(t *testing.T) {
	pub, _ := harness.Key(t)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	f.SetPeer(p, "")

	for _, testCase := range []struct {
		name       string
		member     string
		generation string
		credential string
	}{
		{"wrong credential", harness.WorkerA, "1", "wrong-credential"},
		{"unknown member", "unknown", "1", harness.NotSecret},
		{"stale generation", harness.WorkerA, "2", harness.NotSecret},
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
	if sessions := f.Runtime.Sessions(); len(sessions) != 0 {
		t.Fatalf("unauthorized tunnel registered a session: %d", len(sessions))
	}
}

// One connector cannot hold two tunnels, so a leaked or duplicated connector
// process cannot silently own the same identity.
func TestTunnelDuplicateRegistrationRejected(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, p, nil)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, connector.NewServer(http.DefaultTransport))
	dupCtx := metadata.AppendToOutgoingContext(context.Background(),
		"x-source-member", harness.WorkerA,
		"x-source-generation", "1",
		"authorization", harness.NotSecret,
	)
	started, err := tunnelServer.Serve(dupCtx)
	if err == nil && started {
		t.Fatal("a duplicate tunnel was admitted for the same worker")
	}
	if sessions := f.Runtime.Sessions(); len(sessions) > 1 {
		t.Fatalf("duplicate registration produced %d sessions", len(sessions))
	}
}

// A revoked generation must fence the live tunnel even if it is otherwise
// healthy, which is what makes revoke immediate.
func TestTunnelProbeFencesRevokedGeneration(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, p, nil)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	f.RevokePeer(harness.WorkerA)
	harness.Eventually(t, func() bool { return f.Runtime.Existing(harness.WorkerA) == nil })
}
