// Package control owns the worker's view of the control plane: the peer
// state it refreshes, the health decisions it reports, and the constant-time
// credential comparison every plane authenticates against.
package control

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

// Client is the control-plane client and peer cache.
type Client struct {
	base        string
	token       string
	tokenDigest string
	client      *http.Client
	mu          sync.RWMutex
	workers     map[string]config.Peer
}

// DigestValue is the hex SHA-256 of a secret, the form the control plane
// stores and the form bearer tokens are compared in.
func DigestValue(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// New builds the client and blocks until peer state loads, retrying for up to
// two minutes so a worker can start before the control plane is ready.
func New(ctx context.Context, cfg config.Config, client *http.Client) (*Client, error) {
	parsed, err := url.Parse(cfg.ControlURL)
	if err != nil || (parsed.Scheme != "https" && parsed.Hostname() != "127.0.0.1") || parsed.Host == "" {
		return nil, errors.New("control URL must use an internal HTTP loopback or HTTPS endpoint")
	}
	server := &Client{base: strings.TrimSuffix(cfg.ControlURL, "/"), token: cfg.ServiceToken,
		tokenDigest: DigestValue(cfg.ServiceToken), client: client, workers: make(map[string]config.Peer)}
	for deadline := time.Now().Add(2 * time.Minute); ; {
		if err := server.Refresh(ctx); err == nil {
			return server, nil
		} else if time.Now().After(deadline) {
			return nil, fmt.Errorf("load PostgreSQL worker state: %w", err)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

// TokenDigest is the digest of the service token, for local request auth.
func (c *Client) TokenDigest() string { return c.tokenDigest }

// Report posts a probe result and returns whether the peer was admitted.
func (c *Client) Report(ctx context.Context, payload config.HealthReport) (bool, error) {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/v1/source-link/report", bytes.NewReader(encoded))
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return false, fmt.Errorf("control returned HTTP %d", resp.StatusCode)
	}
	var decision struct {
		Admitted bool `json:"admitted"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&decision); err != nil {
		return false, err
	}
	return decision.Admitted, nil
}

// Refresh replaces the cached peer state.
func (c *Client) Refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/v1/source-link/peers", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("peer state returned HTTP %d", resp.StatusCode)
	}
	var workers []config.Peer
	if err := json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&workers); err != nil {
		return err
	}
	state := make(map[string]config.Peer, len(workers))
	for _, worker := range workers {
		if _, ok := state[worker.WorkerID]; ok || worker.Generation == 0 || len(worker.CredentialDigest) != 64 {
			return errors.New("invalid peer state")
		}
		state[worker.WorkerID] = worker
	}
	c.mu.Lock()
	c.workers = state
	c.mu.Unlock()
	return nil
}

// Peer returns the cached record for one connector.
func (c *Client) Peer(workerID string) (config.Peer, bool) {
	c.mu.RLock()
	worker, ok := c.workers[workerID]
	c.mu.RUnlock()
	return worker, ok
}

// Peers returns a snapshot of the cached peer state, for assertions that
// must not reach into the private map.
func (c *Client) Peers() []config.Peer {
	c.mu.RLock()
	defer c.mu.RUnlock()
	peers := make([]config.Peer, 0, len(c.workers))
	for _, worker := range c.workers {
		peers = append(peers, worker)
	}
	return peers
}

// SameSecret reports whether supplied hashes to expected, in constant time.
func SameSecret(expected, supplied string) bool {
	return len(supplied) > 0 && subtle.ConstantTimeCompare([]byte(expected), []byte(DigestValue(supplied))) == 1
}

// Bearer extracts the header token from an Authorization: Bearer request.
func Bearer(req *http.Request) string {
	value, ok := strings.CutPrefix(req.Header.Get("Authorization"), "Bearer ")
	if !ok {
		return ""
	}
	return value
}
