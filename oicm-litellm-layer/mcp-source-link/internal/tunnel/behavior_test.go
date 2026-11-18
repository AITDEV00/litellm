package tunnel_test

import (
	"context"
	"log"
	"net/http"
	"strings"
	"sync"
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
			dispatchpb.RegisterDispatchServiceServer(tunnelServer, connector.NewServer(http.DefaultTransport, "127.0.0.1"))
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
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, connector.NewServer(http.DefaultTransport, "127.0.0.1"))
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

// The admission log is the only place an operator can see which fleet versions
// are connected and what each one can do, so a tunnel that admits member A with
// capabilities {execute, probe} must say so, with the generation it admitted.
func TestAdmissionLogsWorkerGenerationAndCapabilities(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	target := f.TunnelServer(t)
	peer := harness.Peer(harness.WorkerA, 7, pub)
	peer.Target = strings.TrimPrefix(origin.URL, "http://")
	peer.ExpectedCatalog = digest
	f.SetPeer(peer, "")

	captured := &captureWriter{}
	previous := log.Writer()
	log.SetOutput(captured)
	t.Cleanup(func() { log.SetOutput(previous) })

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnector(ctx, t, target, peer, nil)
	harness.Eventually(t, func() bool { return f.ReadyTunnel(t, harness.WorkerA) })

	line := captured.lineContaining("tunnel admitted worker=" + harness.WorkerA)
	if line == "" {
		t.Fatalf("no admission log line for worker %s; captured:\n%s", harness.WorkerA, captured.String())
	}
	for _, want := range []string{"generation=7", "capabilities=", "execute", "probe"} {
		if !strings.Contains(line, want) {
			t.Fatalf("admission log line %q is missing %q", line, want)
		}
	}
}

// captureWriter collects log output so a test can assert on it without a race
// against the goroutines that write.
type captureWriter struct {
	mu    sync.Mutex
	lines []string
}

func (c *captureWriter) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lines = append(c.lines, strings.TrimSpace(string(p)))
	return len(p), nil
}

func (c *captureWriter) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return strings.Join(c.lines, "\n")
}

func (c *captureWriter) lineContaining(sub string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, line := range c.lines {
		if strings.Contains(line, sub) {
			return line
		}
	}
	return ""
}
