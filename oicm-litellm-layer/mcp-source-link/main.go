// Command mcp-source-link is the MCP source broker's sidecar. In worker mode
// it serves the tunnel data plane beside the Python control app; in connector
// mode it runs on a source host and dials out. Both modes are the same binary.
//
// This file is wiring only. Behavior lives in the internal slices:
//
//	config      environment, identity, and peer record validation
//	control     peer state, health reports, credential comparison
//	pool        which connector serves a request
//	catalog     catalog proof and verification
//	listen      protocol and sizing choices for the two listeners
//	tunnel      the grpctunnel data plane
//	connector   the source-host side
//	wire        envelope types and shared size limits
package main

import (
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

func main() {
	if len(os.Args) != 2 || (os.Args[1] != "worker" && os.Args[1] != "connector") {
		log.Fatal("usage: mcp-source-link worker|connector")
	}
	if err := run(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}

func run(mode string) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if mode == "connector" {
		cfg, err := config.ReadConnector(config.Environment("MCP_SOURCE_LINK_CONNECTOR_CONFIG"))
		if err != nil {
			return err
		}
		return connector.Run(ctx, cfg)
	}
	return runWorker(ctx, stop)
}

func runWorker(ctx context.Context, stop context.CancelFunc) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	client, err := config.ControlClient(cfg)
	if err != nil {
		return err
	}
	defer client.CloseIdleConnections()
	state, err := control.New(ctx, cfg, client)
	if err != nil {
		return err
	}
	sessions := pool.New(state)
	tunnels := tunnel.New(sessions)

	data := http.NewServeMux()
	data.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	data.HandleFunc("POST /v1/source-link/revoke", sessions.RevokeHTTP)
	data.HandleFunc("/sources/{source}/{path...}", sessions.Forward)

	reverse := listen.Server()
	reverse.Addr = cfg.ReverseListen
	reverse.Handler = tunnel.HTTPHandler(tunnels.GRPCServer())

	servers := []*http.Server{
		{Addr: cfg.DataListen, Handler: data, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second,
			MaxHeaderBytes: 32 << 10, HTTP2: listen.HTTP2Config(), Protocols: listen.Data()},
		reverse,
	}
	for _, server := range servers {
		listener, err := net.Listen("tcp", server.Addr)
		if err != nil {
			return err
		}
		go func(server *http.Server) {
			if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("listener stopped: %v", err)
				stop()
			}
		}(server)
	}
	go reconcileLoop(ctx, state, sessions)
	log.Printf("tunnel worker; private dispatch on %s; tunnel ingress on %s", cfg.DataListen, cfg.ReverseListen)
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tunnels.CloseAll()
	for _, server := range servers {
		_ = server.Shutdown(shutdown)
	}
	return nil
}

// reconcileLoop refreshes peer state and drops any tunnel the control plane has
// moved past.
func reconcileLoop(ctx context.Context, state *control.Client, sessions *pool.Runtime) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = state.Refresh(ctx)
			sessions.Reconcile()
		}
	}
}
