// Package connector owns the source-host side. A connector dials out to the
// gateway, keeps the tunnel open, and executes dispatched requests against a
// loopback origin it cannot otherwise expose.
package connector

import (
	"context"
	"net"
	"net/http"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// originTransport builds the loopback transport the connector uses: every
// destination collapses to the configured target, which ReadConnector has
// already validated as a numeric loopback address.
func originTransport(target string, maxIdle int) *http.Transport {
	local := &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second}
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return local.DialContext(ctx, "tcp", target)
		},
		MaxIdleConns: maxIdle, MaxIdleConnsPerHost: maxIdle,
		IdleConnTimeout: 90 * time.Second, DisableCompression: true, ResponseHeaderTimeout: 30 * time.Second,
	}
}

// Run dials out and serves the tunnel until the context ends.
//
// The gateway URL comes from ResolveGateway, so an installed service uses the URL
// persisted during initialization unless an explicit override is set. The CA file
// is honoured here too, which is what makes a private-CA dev gateway reachable.
func Run(ctx context.Context, cfg config.ConnectorConfig, stateDir string) error {
	gateway, err := config.ResolveGateway(stateDir)
	if err != nil {
		return err
	}
	ca := config.Environment("MCP_SOURCE_LINK_CA_FILE")
	tlsConfig, err := config.TLS(ca)
	if err != nil {
		return err
	}
	origin := originTransport(cfg.Target, 65536)
	defer origin.CloseIdleConnections()
	return RunTunnel(ctx, cfg, gateway, tlsConfig, origin)
}
