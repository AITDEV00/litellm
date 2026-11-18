// Package connector owns the source-host side. A connector dials out to the
// gateway, keeps a tunnel open per source, and executes dispatched requests
// against the origins it cannot otherwise expose.
//
// One agent process serves every enabled source, across every account, in its own
// goroutine. A revoked or failing source is restarted independently, so one bad
// origin never takes down the others.
package connector

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"sort"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/state"
	"oicm-litellm-layer/mcp-source-link/internal/target"
)

// originTransport builds a transport that dials the source's target directly.
//
// The dial address is the target's host:port. The Host header the origin receives
// is set by the dispatch server from the target's authority, so a Kubernetes
// service sees its own DNS name while an IP literal sees the IP. Collapsing every
// destination to one address, as the loopback-only version did, is wrong once a
// source may be a service in another namespace.
func originTransport(origin target.Target, maxIdle int) *http.Transport {
	dialer := &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second}
	address := origin.String()
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, "tcp", address)
		},
		MaxIdleConns: maxIdle, MaxIdleConnsPerHost: maxIdle,
		IdleConnTimeout: 90 * time.Second, DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
	}
}

// Run serves one source's tunnel until the context ends.
func Run(ctx context.Context, source state.Source, gateway string) error {
	origin, err := target.Parse(source.Target)
	if err != nil {
		return err
	}
	tlsConfig, err := config.TLS(config.Environment("MCP_SOURCE_LINK_CA_FILE"))
	if err != nil {
		return err
	}
	transport := originTransport(origin, 65536)
	defer transport.CloseIdleConnections()
	identity := config.ConnectorConfig{
		WorkerID:   source.WorkerID,
		SourceID:   source.SourceID,
		Target:     source.Target,
		Generation: source.Generation,
		Credential: source.Credential,
		Kind:       source.Kind,
		Path:       source.Path,
	}
	return RunTunnel(ctx, identity, gateway, tlsConfig, transport, origin.Authority())
}

// RunAll serves every registered, enabled source concurrently until ctx ends.
//
// It reconciles: each call to Sync reads the store and starts a source that is not
// already running, cancels one that was removed, and leaves an unchanged source
// alone, so a reload never drops a healthy tunnel for a source nobody touched.
// Reloads arrive on the reload channel, which is how `adma source add` makes a
// running agent pick up the new source without a restart.
func RunAll(ctx context.Context, store *state.Store, reload <-chan struct{}) error {
	run := newRunner(ctx, store)
	if err := run.sync(); err != nil {
		return err
	}
	if run.started() == 0 {
		return fmt.Errorf("no registered sources to serve; run: adma source add")
	}
	for {
		select {
		case <-ctx.Done():
			run.stopAll()
			return nil
		case <-reload:
			if err := run.sync(); err != nil {
				log.Printf("reload: %v", err)
				continue
			}
			log.Printf("reloaded; serving %d source(s)", run.started())
		}
	}
}

// runner holds the live tunnels keyed by source name, so a reload can diff the
// store against what is actually running.
type runner struct {
	ctx     context.Context
	store   *state.Store
	running map[string]*runningSource
}

type runningSource struct {
	cancel  context.CancelFunc
	source  state.Source
	gateway string
}

func newRunner(ctx context.Context, store *state.Store) *runner {
	return &runner{ctx: ctx, store: store, running: map[string]*runningSource{}}
}

func (r *runner) started() int {
	return len(r.running)
}

// sync diffs the store against the running set. A source present in both with the
// same identity is left to run; a new or changed one is (re)started; a vanished
// one is cancelled.
func (r *runner) sync() error {
	sources, err := r.store.Sources()
	if err != nil {
		return err
	}
	wanted := make(map[string]state.Source, len(sources))
	for _, source := range sources {
		if !source.Enabled || !source.Registered() {
			continue
		}
		wanted[source.Name] = source
	}
	running := make(map[string]state.Source, len(r.running))
	for name, current := range r.running {
		running[name] = current.source
	}
	stop, start := planChanges(running, wanted)
	for _, name := range stop {
		r.running[name].cancel()
		delete(r.running, name)
	}
	for _, source := range start {
		if err := r.start(source); err != nil {
			log.Printf("source %s not started: %v", source.Name, err)
		}
	}
	return nil
}

// planChanges is the reconcile decision, separated from the goroutine plumbing so
// it is a pure function a test can pin: which running sources to stop, and which
// wanted sources to start. A source in both maps with the same identity is in
// neither, so a reload never restarts a healthy tunnel.
func planChanges(running, wanted map[string]state.Source) (stop []string, start []state.Source) {
	toStop := make([]string, 0, len(running))
	for name, current := range running {
		next, stillWanted := wanted[name]
		if !stillWanted || !sameIdentity(current, next) {
			toStop = append(toStop, name)
		}
	}
	toStart := make([]state.Source, 0, len(wanted))
	for name, source := range wanted {
		current, alreadyRunning := running[name]
		if alreadyRunning && sameIdentity(current, source) {
			continue
		}
		toStart = append(toStart, source)
	}
	sort.Strings(toStop)
	sort.Slice(toStart, func(i, j int) bool { return toStart[i].Name < toStart[j].Name })
	return toStop, toStart
}

func (r *runner) start(source state.Source) error {
	account, found, err := r.store.Account(source.Account)
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("unknown account %s; skipping", source.Account)
	}
	gateway, err := resolveGateway(account.GatewayURL)
	if err != nil {
		return err
	}
	sourceCtx, cancel := context.WithCancel(r.ctx)
	r.running[source.Name] = &runningSource{cancel: cancel, source: source, gateway: gateway}
	go func() {
		if err := Run(sourceCtx, source, gateway); err != nil && sourceCtx.Err() == nil {
			log.Printf("source %s stopped: %v", source.Name, err)
		}
	}()
	log.Printf("serving source %s (%s) for account %s", source.Name, source.Target, source.Account)
	return nil
}

func (r *runner) stopAll() {
	for _, current := range r.running {
		current.cancel()
	}
}

// sameIdentity reports whether two records describe the same tunnel, so a reload
// keeps an unchanged source running instead of restarting it. The generation is
// the gateway's own change marker; worker and credential are what the tunnel
// authenticates with.
func sameIdentity(a, b state.Source) bool {
	return a.WorkerID == b.WorkerID &&
		a.Credential == b.Credential &&
		a.Generation == b.Generation &&
		a.Target == b.Target &&
		a.Account == b.Account
}

// resolveGateway returns an explicit tunnel URL override when set, otherwise the
// account's gateway URL.
func resolveGateway(accountGateway string) (string, error) {
	if explicit := config.Environment("MCP_SOURCE_LINK_TUNNEL_URL"); explicit != "" {
		return config.ValidateGatewayURL(explicit)
	}
	return config.ValidateGatewayURL(accountGateway)
}
