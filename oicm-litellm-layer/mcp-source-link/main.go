// Command mcp-source-link is the MCP source broker's sidecar and the source-host
// agent. Both modes are the same binary.
//
// In worker mode it serves the tunnel data plane beside the Python control app. In
// agent mode it runs on a source host, holds a local account store, and serves
// every registered source across every account in one long-lived process.
//
// This file is wiring only. Behavior lives in the internal slices:
//
//	state       the on-disk account store and source registry
//	setup       interactive account and source commands
//	auth        the gateway password-to-key exchange
//	bootstrap   source enrollment and worker issuance
//	config      environment, TLS, and log-level settings
//	connector   the source-host tunnel runtime
//	control     worker peer state and health reports
//	pool        which connector serves a request
//	catalog     catalog canonicalization and fingerprinting
//	listen      protocol and sizing choices for the two listeners
//	tunnel      the grpctunnel data plane
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
	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/connector"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/setup"
	"oicm-litellm-layer/mcp-source-link/internal/state"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

const usage = `usage: adma <command> [options]

The agent keeps a local set of accounts and sources, so a second source never
asks for your password again.

account commands:
  account add <alias>          add an account (gateway URL, username, password)
  account list                 list stored accounts and the selected one
  account select <alias>       choose the account new sources are added to

source commands:
  source add                   register a source under the selected account
  source add --account <alias> register a source under a specific account
  source add --kind openapi --target <host:port> [--spec <path>]
                               register an HTTP service; the origin's own
                               /openapi.json is fetched unless --spec pins a file
  source add --kind mcp --target <host:port> [--path /mcp]
                               register a standard MCP server (no OpenAPI doc)
  source list                  list registered sources
  source remove <name>         forget one source locally

agent commands:
  connector                    serve every registered source in one process
  worker                       run the worker beside the control app

flags:
  --insecure                   skip TLS certificate verification (dev gateways
                               with a self-signed certificate; never use against
                               production)
  -h, --help                   show this message
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		log.Fatal(err)
	}
}

func run(args []string) error {
	mode, options, err := parse(args)
	if err != nil {
		return err
	}
	if mode == help {
		fmt.Print(usage)
		return nil
	}
	if options.insecure {
		if err := os.Setenv("MCP_SOURCE_LINK_INSECURE", "1"); err != nil {
			return err
		}
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch mode {
	case accountAdd:
		return runAccountAdd(ctx, options)
	case accountList:
		return runAccountList()
	case accountSelect:
		return runAccountSelect(options)
	case sourceAdd:
		return runSourceAdd(ctx, options)
	case sourceList:
		return runSourceList()
	case sourceRemove:
		return runSourceRemove(options)
	case connectorMode:
		return runConnector(ctx)
	default:
		return runWorker(ctx, stop)
	}
}

// command is the parsed top-level command.
type command int

const (
	help command = iota
	accountAdd
	accountList
	accountSelect
	sourceAdd
	sourceList
	sourceRemove
	connectorMode
	workerMode
)

// options carries the flags and positional values a command may need.
type options struct {
	insecure bool
	alias    string
	account  string
	target   string
	name     string
	spec     string
	kind     string
	path     string
}

func parse(args []string) (command, options, error) {
	var opts options
	positional := make([]string, 0, len(args))
	for index := 0; index < len(args); index++ {
		arg := args[index]
		value := func() (string, error) {
			if index+1 >= len(args) {
				return "", fmt.Errorf("%s needs a value", arg)
			}
			index++
			return args[index], nil
		}
		switch arg {
		case "-h", "--help":
			return help, opts, nil
		case "--insecure":
			opts.insecure = true
		case "--account":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.account = parsed
		case "--target":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.target = parsed
		case "--name":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.name = parsed
		case "--spec":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.spec = parsed
		case "--kind":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.kind = parsed
		case "--path":
			parsed, err := value()
			if err != nil {
				return help, opts, err
			}
			opts.path = parsed
		default:
			positional = append(positional, arg)
		}
	}
	if len(positional) == 0 {
		return help, opts, nil
	}
	first := positional[0]
	rest := positional[1:]
	switch first {
	case "account":
		return parseAccount(rest, opts)
	case "source":
		return parseSource(rest, opts)
	case "connector":
		return connectorMode, opts, nil
	case "worker":
		return workerMode, opts, nil
	default:
		fmt.Print(usage)
		return help, opts, fmt.Errorf("unknown command %q", first)
	}
}

func parseAccount(rest []string, opts options) (command, options, error) {
	if len(rest) == 0 {
		return help, opts, errors.New("account needs a subcommand: add, list or select")
	}
	switch rest[0] {
	case "add":
		if len(rest) > 1 {
			opts.alias = rest[1]
		}
		return accountAdd, opts, nil
	case "list":
		return accountList, opts, nil
	case "select":
		if len(rest) < 2 {
			return help, opts, errors.New("account select needs an alias")
		}
		opts.alias = rest[1]
		return accountSelect, opts, nil
	default:
		return help, opts, fmt.Errorf("unknown account subcommand %q", rest[0])
	}
}

func parseSource(rest []string, opts options) (command, options, error) {
	if len(rest) == 0 {
		return help, opts, errors.New("source needs a subcommand: add, list or remove")
	}
	switch rest[0] {
	case "add":
		return sourceAdd, opts, nil
	case "list":
		return sourceList, opts, nil
	case "remove":
		if len(rest) < 2 {
			return help, opts, errors.New("source remove needs a source name")
		}
		opts.name = rest[1]
		return sourceRemove, opts, nil
	default:
		return help, opts, fmt.Errorf("unknown source subcommand %q", rest[0])
	}
}

func runAccountAdd(ctx context.Context, opts options) error {
	if !setup.IsInteractive() {
		return errors.New("account add needs a terminal; run it interactively")
	}
	store, err := openStore()
	if err != nil {
		return err
	}
	httpClient, err := config.HTTPSClient(config.Environment("MCP_SOURCE_LINK_CA_FILE"))
	if err != nil {
		return err
	}
	account, err := setup.AddAccount(ctx, store, terminal(), auth.PasswordLogin{HTTP: httpClient}, opts.alias)
	if err != nil {
		return err
	}
	fmt.Printf("\nAdded account %q (user %s at %s).\n", account.Alias, account.Username, account.GatewayURL)
	return nil
}

func runAccountList() error {
	store, err := openStore()
	if err != nil {
		return err
	}
	listing, err := setup.ListAccounts(store)
	if err != nil {
		return err
	}
	fmt.Print(listing)
	return nil
}

func runAccountSelect(opts options) error {
	store, err := openStore()
	if err != nil {
		return err
	}
	if err := store.Select(opts.alias); err != nil {
		return err
	}
	fmt.Printf("Selected account %q.\n", opts.alias)
	return nil
}

func runSourceAdd(ctx context.Context, opts options) error {
	if !setup.IsInteractive() {
		return errors.New("source add needs a terminal; run it interactively")
	}
	store, err := openStore()
	if err != nil {
		return err
	}
	source, err := setup.AddSource(ctx, store, terminal(), nil, setup.AddSourceInput{
		Account:  opts.account,
		Name:     opts.name,
		Kind:     opts.kind,
		Path:     opts.path,
		Target:   opts.target,
		SpecPath: opts.spec,
	})
	if err != nil {
		var setupExists setup.AlreadyExists
		if errors.As(err, &setupExists) {
			fmt.Printf("\nMCP source already exists as %s; nothing new was created.\n", setupExists.Source.Name)
			return nil
		}
		var gatewayExists bootstrap.ErrAlreadyExists
		if errors.As(err, &gatewayExists) {
			fmt.Printf("\nMCP source already exists as %s; nothing new was created.\n", gatewayExists.Identity.SourceID)
			return nil
		}
		return err
	}
	fmt.Printf("\nAdded source %s (origin %s, worker %s).\nRestart the agent to serve it.\n",
		source.Name, source.Target, source.WorkerID)
	return nil
}

func runSourceList() error {
	store, err := openStore()
	if err != nil {
		return err
	}
	listing, err := setup.ListSources(store)
	if err != nil {
		return err
	}
	fmt.Print(listing)
	return nil
}

func runSourceRemove(opts options) error {
	store, err := openStore()
	if err != nil {
		return err
	}
	if _, found, err := store.Source(opts.name); err != nil {
		return err
	} else if !found {
		return fmt.Errorf("source %q is not stored", opts.name)
	}
	if err := store.RemoveSource(opts.name); err != nil {
		return err
	}
	fmt.Printf("Removed source %q from local state. Its gateway registration is unchanged.\n", opts.name)
	return nil
}

func terminal() setup.Terminal {
	return setup.Terminal{In: os.Stdin, Out: os.Stdout}
}

func openStore() (*state.Store, error) {
	return state.Open(setupBase())
}

// setupBase is where accounts and sources are stored.
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

// runConnector serves every registered source across every account in this one
// process. It never prompts; an unregistered source is skipped with a clear log
// line rather than blocking for input under systemd.
func runConnector(ctx context.Context) error {
	store, err := openStore()
	if err != nil {
		return err
	}
	return connector.RunAll(ctx, store)
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
	controlState, err := control.New(ctx, cfg, client)
	if err != nil {
		return err
	}
	sessions := pool.New(controlState)
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
	go reconcileLoop(ctx, controlState, sessions)
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
