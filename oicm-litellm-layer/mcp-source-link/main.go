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
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/auth"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/setup"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: mcp-source-link worker|connector|init|reinit")
	}
	switch os.Args[1] {
	case "worker", "connector", "init", "reinit":
	default:
		log.Fatal("usage: mcp-source-link worker|connector|init|reinit")
	}
	if err := run(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}

func run(mode string) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch mode {
	case "init":
		return runInit(ctx)
	case "reinit":
		return runReinit(ctx)
	case "connector":
		return runConnector(ctx)
	default:
		return runWorker(ctx, stop)
	}
}

// runInit performs the interactive first-use setup: authenticate the ordinary
// user, read the OpenAPI document, enroll the source, register this worker, and
// persist everything owner-only. It is idempotent: a valid identity is never
// overwritten.
func runInit(ctx context.Context) error {
	if !setup.IsInteractive() {
		return errors.New("source-link init needs a terminal; run it interactively on the source host")
	}
	base := setupBase()
	if err := os.MkdirAll(base, 0o700); err != nil {
		return err
	}
	prompt := setup.Terminal{In: os.Stdin, Out: os.Stdout}
	httpClient, err := config.HTTPSClient(config.Environment("MCP_SOURCE_LINK_CA_FILE"))
	if err != nil {
		return err
	}
	identity, err := setup.Initialize(ctx, setup.PathsFor(base), prompt, auth.PasswordLogin{HTTP: httpClient}, nil)
	if err != nil {
		return err
	}
	fmt.Printf("\nSource connector initialized.\n  worker:  %s\n  source:  %s\n  target:  %s\n"+
		"Start it with: systemctl --user enable --now source-agent.service\n",
		identity.WorkerID, identity.SourceID, identity.Target)
	return nil
}

// setupBase is where initialization writes its owner-only state.
func setupBase() string {
	if explicit := config.Environment("MCP_SOURCE_LINK_STATE_DIR"); explicit != "" {
		return explicit
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".config/source-agent"
	}
	return filepath.Join(home, ".config", "source-agent")
}

// runConnector starts the tunnel from a persisted identity. If none exists it
// must not silently re-bootstrap: an interactive terminal is offered init, and a
// non-interactive run (systemd) fails with instructions rather than blocking on
// stdin forever.
func runConnector(ctx context.Context) error {
	base := setupBase()
	identityPath := filepath.Join(base, "identity.json")
	if explicit := config.Environment("MCP_SOURCE_LINK_CONNECTOR_CONFIG"); explicit != "" {
		identityPath = explicit
	}
	identity, err := config.ReadConnector(identityPath)
	if err != nil {
		if !setup.IsInteractive() {
			return errors.New("no connector identity found; run: source-link init")
		}
		fmt.Println("This connector is not initialized yet; starting setup.")
		if initErr := runInit(ctx); initErr != nil {
			return initErr
		}
		identity, err = config.ReadConnector(identityPath)
		if err != nil {
			return err
		}
	}
	return connector.Run(ctx, identity, base)
}

// runReinit replaces a connector identity that can no longer be used, which is
// the supported recovery for a revoked worker or a permanently lost local
// identity.
//
// Revocation is terminal by design, so the old worker cannot be revived and its
// label stays claimed. Replacement therefore needs a new label, and this keeps
// the old identity rather than deleting it: it is archived beside the new one, so
// the fenced old generation stays on disk for an operator to inspect and nothing
// silently pretends the revoked worker became valid again.
func runReinit(ctx context.Context) error {
	if !setup.IsInteractive() {
		return errors.New("source-link reinit needs a terminal; run it interactively on the source host")
	}
	base := setupBase()
	if err := os.MkdirAll(base, 0o700); err != nil {
		return err
	}
	paths := setup.PathsFor(base)
	stamp := time.Now().UTC().Format("20060102T150405Z")
	if _, err := os.Stat(paths.Identity); err == nil {
		archive := paths.Identity + ".revoked-" + stamp
		if err := os.Rename(paths.Identity, archive); err != nil {
			return fmt.Errorf("archive the old identity: %w", err)
		}
		fmt.Printf("Old identity archived at %s (its generation stays fenced).\n", archive)
	}
	if _, err := os.Stat(paths.Bootstrap); err == nil {
		if err := os.Rename(paths.Bootstrap, paths.Bootstrap+".revoked-"+stamp); err != nil {
			return fmt.Errorf("archive the old bootstrap input: %w", err)
		}
	}
	fmt.Println("Registering a replacement connector. Use a NEW source name: the old one is still registered.")
	prompt := setup.Terminal{In: os.Stdin, Out: os.Stdout}
	httpClient, err := config.HTTPSClient(config.Environment("MCP_SOURCE_LINK_CA_FILE"))
	if err != nil {
		return err
	}
	identity, err := setup.Initialize(ctx, paths, prompt, auth.PasswordLogin{HTTP: httpClient}, nil)
	if err != nil {
		return err
	}
	fmt.Printf("\nReplacement connector initialized.\n  worker:  %s\n  source:  %s\n  target:  %s\n"+
		"Restart it with: systemctl --user restart source-agent.service\n",
		identity.WorkerID, identity.SourceID, identity.Target)
	return nil
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
