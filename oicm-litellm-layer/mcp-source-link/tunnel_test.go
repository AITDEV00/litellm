package main

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
	"oicm-litellm-layer/mcp-source-link/internal/reverseh2"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

// tunnelFixture stands up the tunnel plane exactly as production wires it: one
// h2c listener multiplexing the tunnel gRPC service beside the long-poll mux.
func tunnelFixture(t *testing.T) (*fixture, *tunnel.Runtime, string) {
	t.Helper()
	f := setupFixture(t)
	server := reverseh2.Server()
	server.Handler = tunnel.HTTPHandler(f.tunnels.GRPCServer(), http.NewServeMux())
	server.Protocols = reverseh2.H2CProtocols()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	t.Cleanup(f.tunnels.CloseAll)
	go func() { _ = server.Serve(listener) }()
	return f, f.tunnels, listener.Addr().String()
}

// runTunnelConnector connects one connector and serves pushed RPCs until the
// context ends, mirroring the shipped reconnect loop.
func runTunnelConnector(ctx context.Context, t *testing.T, target string, p config.Peer, origin *http.Transport) {
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
