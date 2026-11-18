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
	}
	return RunTunnel(ctx, identity, gateway, tlsConfig, transport, origin.Authority())
}

// RunAll serves every registered, enabled source concurrently and returns when
// ctx ends or no source can be served.
//
// Each source gets its own supervised goroutine. A source that fails to dial
// retries inside RunTunnel; a source whose account is missing is logged and
// skipped rather than aborting the whole agent, so one bad registration cannot
// stop every other source from serving.
func RunAll(ctx context.Context, store *state.Store) error {
	sources, err := store.Sources()
	if err != nil {
		return err
	}
	started := 0
	accounts := make(map[string]struct{})
	for _, source := range sources {
		if !source.Enabled || !source.Registered() {
			continue
		}
		account, found, err := store.Account(source.Account)
		if err != nil {
			return err
		}
		if !found {
			log.Printf("source %s names unknown account %s; skipping", source.Name, source.Account)
			continue
		}
		gateway, err := resolveGateway(account.GatewayURL)
		if err != nil {
			return err
		}
		started++
		accounts[source.Account] = struct{}{}
		go func(source state.Source, gateway string) {
			if err := Run(ctx, source, gateway); err != nil && ctx.Err() == nil {
				log.Printf("source %s stopped: %v", source.Name, err)
			}
		}(source, gateway)
	}
	if started == 0 {
		return fmt.Errorf("no registered sources to serve; run: adma source add")
	}
	log.Printf("serving %d source(s) across %d account(s)", started, len(accounts))
	<-ctx.Done()
	return nil
}

// resolveGateway returns an explicit tunnel URL override when set, otherwise the
// account's gateway URL.
func resolveGateway(accountGateway string) (string, error) {
	if explicit := config.Environment("MCP_SOURCE_LINK_TUNNEL_URL"); explicit != "" {
		return config.ValidateGatewayURL(explicit)
	}
	return config.ValidateGatewayURL(accountGateway)
}
