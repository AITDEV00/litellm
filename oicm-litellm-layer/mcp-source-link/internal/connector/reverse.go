// Package connector owns the source-host side. A connector is the mirror
// image of a plane: it dials out to the gateway, keeps the channel open, and
// executes dispatched requests against a loopback origin it cannot otherwise
// expose.
package connector

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// originTransport builds the loopback transport every connector shares: all
// destinations collapse to the configured target, which ReadConnector has
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

// Execute runs one dispatched request against the origin and builds the
// response envelope.
func Execute(ctx context.Context, origin *http.Transport, item wire.DispatchRequest) (wire.DispatchResponse, error) {
	target := url.URL{Scheme: "http", Host: "target.source.invalid", Path: item.Path, RawQuery: item.Query}
	req, err := http.NewRequestWithContext(ctx, item.Method, target.String(), bytes.NewReader(item.Body))
	if err != nil {
		return wire.DispatchResponse{}, err
	}
	for name, values := range item.Headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := origin.RoundTrip(req)
	if err != nil {
		return wire.DispatchResponse{}, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, wire.MaxOriginBody+1))
	if err != nil {
		return wire.DispatchResponse{}, err
	}
	if len(body) > wire.MaxOriginBody {
		return wire.DispatchResponse{}, errors.New("origin response exceeds the dispatch body limit")
	}
	headers := make(map[string][]string, len(resp.Header))
	for name, values := range resp.Header {
		headers[name] = values
	}
	return wire.DispatchResponse{ID: item.ID, Status: resp.StatusCode, Headers: headers, Body: body}, nil
}

// Run picks the connector transport from the environment and dials out.
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
	if config.Environment("MCP_SOURCE_LINK_TRANSPORT") == config.TransportTunnel {
		origin := originTransport(cfg.Target, 65536)
		defer origin.CloseIdleConnections()
		return RunTunnel(ctx, cfg, gateway, tlsConfig, origin)
	}
	headers := http.Header{
		"X-Source-Member":     {cfg.WorkerID},
		"X-Source-Generation": {fmt.Sprint(cfg.Generation)},
		"Authorization":       {"Bearer " + cfg.Credential},
	}
	return RunReverse(ctx, cfg, gateway, tlsConfig, headers)
}

// RunReverse keeps the long-poll session alive, reconnecting with capped
// exponential backoff until the context ends.
func RunReverse(ctx context.Context, cfg config.ConnectorConfig, gateway string, tlsConfig *tls.Config, headers http.Header) error {
	base := strings.TrimSuffix(gateway, "/")
	client := &http.Client{
		Transport: &http.Transport{
			TLSClientConfig:     tlsConfig,
			ForceAttemptHTTP2:   true,
			TLSHandshakeTimeout: 8 * time.Second,
			IdleConnTimeout:     90 * time.Second,
			MaxIdleConns:        4,
			DisableCompression:  true,
		},
	}
	origin := originTransport(cfg.Target, 0)
	defer origin.CloseIdleConnections()
	delay := 300 * time.Millisecond
	for ctx.Err() == nil {
		started := time.Now()
		err := ReverseSession(ctx, client, base, headers, origin)
		if ctx.Err() != nil {
			return nil
		}
		if time.Since(started) > 30*time.Second {
			delay = 300 * time.Millisecond
		}
		log.Printf("reverse connector %s disconnected: %v", cfg.WorkerID, err)
		timer := time.NewTimer(delay + time.Duration(rand.Int64N(int64(delay/2))))
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
		delay = min(15*time.Second, delay*2)
	}
	return nil
}

// Post sends one JSON control message to the gateway with the connector
// identity headers attached.
func Post(ctx context.Context, client *http.Client, target string, headers http.Header, body []byte) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, reader)
	if err != nil {
		return nil, err
	}
	req.Header = headers.Clone()
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

// Dispatchers is how many concurrent long-polls one connector maintains.
const Dispatchers = 8

// ReverseSession registers, holds, and services dispatch streams until any
// one of them fails, then returns so the caller can reconnect.
func ReverseSession(ctx context.Context, client *http.Client, base string, headers http.Header, origin *http.Transport) error {
	response, err := Post(ctx, client, base+"/mcp-source-link-reverse", headers, nil)
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("reverse registration returned HTTP %d", response.StatusCode)
	}
	holdCtx, holdCancel := context.WithCancel(ctx)
	defer holdCancel()
	go func() {
		resp, err := Post(holdCtx, client, base+"/mcp-source-link-hold", headers, nil)
		if err == nil {
			_ = resp.Body.Close()
		}
		holdCancel()
	}()
	var workers sync.WaitGroup
	lost := make(chan struct{}, 1)
	for range Dispatchers {
		workers.Add(1)
		go func() {
			defer workers.Done()
			dispatchLoop(holdCtx, client, base, headers, origin, lost)
		}()
	}
	workers.Wait()
	return nil
}

func dispatchLoop(ctx context.Context, client *http.Client, base string, headers http.Header, origin *http.Transport, lost chan struct{}) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-lost:
			return
		default:
		}
		resp, err := Post(ctx, client, base+"/mcp-source-link-dispatch", headers, nil)
		if err != nil || resp.StatusCode != http.StatusOK {
			if resp != nil {
				_ = resp.Body.Close()
			}
			signalLost(lost)
			return
		}
		var item wire.DispatchRequest
		if err := json.NewDecoder(resp.Body).Decode(&item); err != nil {
			_ = resp.Body.Close()
			continue
		}
		_ = resp.Body.Close()
		payload, err := Execute(ctx, origin, item)
		if err != nil {
			payload = wire.DispatchResponse{ID: item.ID, Error: err.Error()}
		}
		encoded, err := json.Marshal(payload)
		if err != nil {
			continue
		}
		done, err := Post(ctx, client, base+"/mcp-source-link-respond", headers, encoded)
		if err != nil {
			signalLost(lost)
			return
		}
		status := done.StatusCode
		_, _ = io.Copy(io.Discard, io.LimitReader(done.Body, 4096))
		_ = done.Body.Close()
		if status != http.StatusNoContent {
			signalLost(lost)
			return
		}
	}
}

func signalLost(lost chan struct{}) {
	select {
	case lost <- struct{}{}:
	default:
	}
}
