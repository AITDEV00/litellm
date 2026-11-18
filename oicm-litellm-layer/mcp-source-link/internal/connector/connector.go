// Package connector owns the source-host side. A connector dials out to the
// gateway, keeps the tunnel open, and executes dispatched requests against a
// loopback origin it cannot otherwise expose.
package connector

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/bootstrap"
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
func Run(ctx context.Context, cfg config.ConnectorConfig) error {
	gateway := config.Environment("MCP_SOURCE_LINK_PUBLIC_URL")
	ca := config.Environment("MCP_SOURCE_LINK_CA_FILE")
	if !strings.HasPrefix(gateway, "https://") {
		return errors.New("worker URL must use approved HTTPS")
	}
	tlsConfig, err := config.TLS(ca)
	if err != nil {
		return err
	}
	origin := originTransport(cfg.Target, 65536)
	defer origin.CloseIdleConnections()
	return RunTunnel(ctx, cfg, gateway, tlsConfig, origin)
}

// identityPath is where the issued connector identity is persisted. It sits
// beside the operator-supplied input so one directory holds the connector's whole
// durable state.
func identityPath() string {
	if explicit := config.Environment("MCP_SOURCE_LINK_CONNECTOR_CONFIG"); explicit != "" {
		return explicit
	}
	return filepath.Join(filepath.Dir(bootstrapPath()), "identity.json")
}

func bootstrapPath() string {
	if explicit := config.Environment("MCP_SOURCE_LINK_BOOTSTRAP_INPUT"); explicit != "" {
		return explicit
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".config/source-agent/bootstrap.json"
	}
	return filepath.Join(home, ".config", "source-agent", "bootstrap.json")
}

// LoadIdentity returns the persisted connector identity, bootstrapping it from the
// gateway on first use. An existing valid identity is always preferred, so a
// restart never re-registers the worker. First-use bootstrap retries with bounded
// backoff until the gateway and the user key are both available, then persists the
// issued identity atomically at an owner-only path.
func LoadIdentity(ctx context.Context, gateway string, httpClient *http.Client) (config.ConnectorConfig, error) {
	path := identityPath()
	if identity, err := config.ReadConnector(path); err == nil {
		return identity, nil
	}
	input, target, err := config.ReadBootstrapInput(bootstrapPath())
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	client, err := bootstrap.New(gateway, config.Environment("MCP_SOURCE_LINK_USER_KEY"), httpClient)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	delay := 500 * time.Millisecond
	for ctx.Err() == nil {
		issued, enrollErr := enrollOnce(ctx, client, input, target)
		if enrollErr == nil {
			if writeErr := config.WriteConnectorIdentity(path, issued); writeErr != nil {
				return config.ConnectorConfig{}, writeErr
			}
			return issued, nil
		}
		if !errors.Is(enrollErr, bootstrap.ErrNotYetAvailable) {
			return config.ConnectorConfig{}, enrollErr
		}
		select {
		case <-ctx.Done():
			return config.ConnectorConfig{}, ctx.Err()
		case <-time.After(delay):
		}
		delay = min(delay*2, 30*time.Second)
	}
	return config.ConnectorConfig{}, ctx.Err()
}

func enrollOnce(
	ctx context.Context, client *bootstrap.Client, input config.BootstrapInput, target config.ConnectorTarget,
) (config.ConnectorConfig, error) {
	enrolled, err := client.Enroll(ctx, bootstrap.EnrollmentInput{
		Alias: input.Alias, DatasetID: input.DatasetID, ProofKey: input.ProofKey, Spec: input.Spec,
	}, target)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	issued, err := client.IssueWorker(ctx, enrolled.SourceID, bootstrap.EnrollmentInput{
		Alias: input.Alias, DatasetID: input.DatasetID, ProofKey: input.ProofKey, Spec: input.Spec,
	}, target, input.Alias)
	if err != nil {
		return config.ConnectorConfig{}, err
	}
	return config.ConnectorConfig{
		WorkerID:   issued.WorkerID,
		SourceID:   issued.SourceID,
		Target:     issued.Target,
		Generation: issued.Generation,
		Credential: issued.Credential,
	}, nil
}
