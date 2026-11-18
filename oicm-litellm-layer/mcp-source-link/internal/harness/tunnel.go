package harness

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"github.com/jhump/grpctunnel"
	"github.com/jhump/grpctunnel/tunnelpb"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

// TunnelServer stands up the tunnel plane exactly as production wires it: one
// h2c listener serving the tunnel gRPC service. It returns the listener address.
func (f *Fixture) TunnelServer(t *testing.T) string {
	t.Helper()
	server := listen.Server()
	server.Handler = tunnel.HTTPHandler(f.Tunnels.GRPCServer())
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	t.Cleanup(f.Tunnels.CloseAll)
	go func() { _ = server.Serve(listener) }()
	return listener.Addr().String()
}

// RunTunnelConnector connects one connector and serves pushed RPCs until the
// context ends, mirroring the shipped reconnect loop.
func RunTunnelConnector(ctx context.Context, t *testing.T, target string, p config.Peer, origin *http.Transport) {
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
	server := connector.NewServer(origin)
	tunnelServer := grpctunnel.NewReverseTunnelServer(tunnelpb.NewTunnelServiceClient(conn))
	dispatchpb.RegisterDispatchServiceServer(tunnelServer, server)
	sessionCtx := metadata.AppendToOutgoingContext(ctx,
		"x-source-member", p.WorkerID,
		"x-source-generation", fmt.Sprint(p.Generation),
		"authorization", NotSecret,
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

// ReadyTunnel reports whether a member is admitted and its catalog verified.
func (f *Fixture) ReadyTunnel(t *testing.T, id string) bool {
	t.Helper()
	session := f.Runtime.Existing(id)
	if session == nil || !session.Ready(SourceID) {
		return false
	}
	f.RefreshPeers()
	peer, ok := f.Control.Peer(id)
	return ok && peer.CatalogFingerprint != nil && *peer.CatalogFingerprint == peer.ExpectedCatalog
}
