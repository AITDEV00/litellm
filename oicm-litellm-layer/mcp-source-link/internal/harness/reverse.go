package harness

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

// ReverseServer mounts the long-poll plane on its own listener, which is how a
// connector reaches it in production.
func (f *Fixture) ReverseServer(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(f.Reverse.Routes())
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	return server
}

// ReverseHeaders builds the identity headers a connector sends.
func ReverseHeaders(p config.Peer, credential string) http.Header {
	return http.Header{
		"X-Source-Member":     {p.WorkerID},
		"X-Source-Generation": {strconv.FormatUint(p.Generation, 10)},
		"Authorization":       {"Bearer " + credential},
	}
}

// ReverseCall posts to a long-poll route.
func ReverseCall(t *testing.T, client *http.Client, url string, headers http.Header, body []byte) (*http.Response, error) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		reader = strings.NewReader(string(body))
	}
	req, err := http.NewRequest(http.MethodPost, url, reader)
	if err != nil {
		return nil, err
	}
	req.Header = headers
	return client.Do(req)
}

// RunReverseConnector drives the connector side of the dispatch protocol
// against a live plane, standing in for a source host.
func RunReverseConnector(ctx context.Context, t *testing.T, client *http.Client, base string, p config.Peer) {
	t.Helper()
	headers := ReverseHeaders(p, NotSecret)
	resp, err := ReverseCall(t, client, base+"/mcp-source-link-reverse", headers, nil)
	if err != nil {
		t.Errorf("connector registration failed: %v", err)
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("connector registration failed: %d", resp.StatusCode)
		return
	}
	holdCtx, holdCancel := context.WithCancel(ctx)
	defer holdCancel()
	go func() {
		resp, err := ReverseCall(t, client, base+"/mcp-source-link-hold", headers, nil)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
		}
		holdCancel()
	}()
	var workers sync.WaitGroup
	for range 4 {
		workers.Add(1)
		go func() {
			defer workers.Done()
			dispatchWorker(holdCtx, t, client, base, headers, p)
		}()
	}
	workers.Wait()
}

func dispatchWorker(ctx context.Context, t *testing.T, client *http.Client, base string, headers http.Header, p config.Peer) {
	for ctx.Err() == nil {
		resp, err := ReverseCall(t, client, base+"/mcp-source-link-dispatch", headers, nil)
		if err != nil || resp.StatusCode != http.StatusOK {
			if resp != nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				_ = resp.Body.Close()
			}
			return
		}
		var item wire.DispatchRequest
		_ = json.NewDecoder(resp.Body).Decode(&item)
		_ = resp.Body.Close()
		payload, err := ExecuteAgainstOrigin(item, p.Target)
		if err != nil {
			payload = wire.DispatchResponse{ID: item.ID, Error: err.Error()}
		}
		encoded, _ := json.Marshal(payload)
		done, err := ReverseCall(t, client, base+"/mcp-source-link-respond", headers, encoded)
		if err != nil {
			return
		}
		_, _ = io.Copy(io.Discard, done.Body)
		_ = done.Body.Close()
	}
}

// ExecuteAgainstOrigin performs the origin round trip the connector would do.
func ExecuteAgainstOrigin(item wire.DispatchRequest, target string) (wire.DispatchResponse, error) {
	var reader io.Reader
	if item.Body != nil {
		reader = strings.NewReader(string(item.Body))
	}
	req, err := http.NewRequest(item.Method, "http://"+target+item.Path, reader)
	if err != nil {
		return wire.DispatchResponse{}, err
	}
	if item.Query != "" {
		req.URL.RawQuery = item.Query
	}
	for name, values := range item.Headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return wire.DispatchResponse{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, wire.MaxBody))
	headers := make(map[string][]string, len(resp.Header))
	for name, values := range resp.Header {
		headers[name] = values
	}
	return wire.DispatchResponse{ID: item.ID, Status: resp.StatusCode, Headers: headers, Body: body}, nil
}

// ReadyReverse reports whether a session is registered and has proven its
// catalog, which is the condition forwarding depends on. The client's peer
// cache is refreshed first, because in production the reconcile loop does that
// every two seconds.
func (f *Fixture) ReadyReverse(t *testing.T, id string) bool {
	t.Helper()
	session := f.Runtime.Existing(id)
	if session == nil || !session.Ready(SourceID) {
		return false
	}
	f.RefreshPeers()
	p, ok := f.Control.Peer(id)
	return ok && p.CatalogFingerprint != nil && *p.CatalogFingerprint == p.ExpectedCatalog
}

// StartSession boots a connector against the plane and waits for registration.
func (f *Fixture) StartSession(t *testing.T, p config.Peer) {
	t.Helper()
	server := f.ReverseServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go RunReverseConnector(ctx, t, server.Client(), server.URL, p)
	Eventually(t, func() bool { return f.Runtime.Existing(p.WorkerID) != nil })
}
