package main

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
)

type control struct {
	base        string
	token       string
	tokenDigest string
	client      *http.Client
	mu          sync.RWMutex
	workers     map[string]peer
}

func digestValue(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func newControl(cfg config, client *http.Client) (*control, error) {
	parsed, err := url.Parse(cfg.ControlURL)
	if err != nil || (parsed.Scheme != "https" && parsed.Hostname() != "127.0.0.1") || parsed.Host == "" {
		return nil, errors.New("control URL must use an internal HTTP loopback or HTTPS endpoint")
	}
	server := &control{base: strings.TrimSuffix(cfg.ControlURL, "/"), token: cfg.ServiceToken,
		tokenDigest: digestValue(cfg.ServiceToken), client: client, workers: make(map[string]peer)}
	if err := server.refresh(context.Background()); err != nil {
		return nil, fmt.Errorf("load PostgreSQL worker state: %w", err)
	}
	return server, nil
}

func (c *control) request(ctx context.Context, method, path string, payload any) error {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("control returned HTTP %d", resp.StatusCode)
	}
	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	return err
}

func (c *control) report(ctx context.Context, payload healthReport) (bool, error) {
	var body io.Reader
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false, err
	}
	body = bytes.NewReader(encoded)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/v1/remotedialer/report", body)
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

func (c *control) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/v1/remotedialer/peers", nil)
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
	var workers []peer
	if err := json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&workers); err != nil {
		return err
	}
	state := make(map[string]peer, len(workers))
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

func (c *control) peer(workerID string) (peer, bool) {
	c.mu.RLock()
	worker, ok := c.workers[workerID]
	c.mu.RUnlock()
	return worker, ok
}

func sameSecret(expected, supplied string) bool {
	return len(supplied) > 0 && subtle.ConstantTimeCompare([]byte(expected), []byte(digestValue(supplied))) == 1
}

func bearer(req *http.Request) string {
	value, ok := strings.CutPrefix(req.Header.Get("Authorization"), "Bearer ")
	if !ok {
		return ""
	}
	return value
}
