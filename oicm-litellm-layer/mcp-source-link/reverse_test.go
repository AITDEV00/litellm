package main

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

// reverseFixture mounts the long-poll plane on its own listener, which is how
// a connector reaches it in production.
func reverseFixture(t *testing.T) (*fixture, *httptest.Server) {
	t.Helper()
	f := setupFixture(t)
	server := httptest.NewServer(f.reverse.Routes())
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	return f, server
}

func reverseHeaders(p config.Peer, credential string) http.Header {
	return http.Header{
		"X-Source-Member":     {p.WorkerID},
		"X-Source-Generation": {strconv.FormatUint(p.Generation, 10)},
		"Authorization":       {"Bearer " + credential},
	}
}

func reverseCall(t *testing.T, client *http.Client, url string, headers http.Header, body []byte) (*http.Response, error) {
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

// runConnector drives the connector side of the dispatch protocol against a
// live plane, standing in for a source host.
func runConnector(ctx context.Context, t *testing.T, client *http.Client, base string, p config.Peer) {
	t.Helper()
	headers := reverseHeaders(p, notSecret)
	resp, err := reverseCall(t, client, base+"/mcp-source-link-reverse", headers, nil)
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
		resp, err := reverseCall(t, client, base+"/mcp-source-link-hold", headers, nil)
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
		resp, err := reverseCall(t, client, base+"/mcp-source-link-dispatch", headers, nil)
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
		payload, err := executeAgainstOrigin(item, p.Target)
		if err != nil {
			payload = wire.DispatchResponse{ID: item.ID, Error: err.Error()}
		}
		encoded, _ := json.Marshal(payload)
		done, err := reverseCall(t, client, base+"/mcp-source-link-respond", headers, encoded)
		if err != nil {
			return
		}
		_, _ = io.Copy(io.Discard, done.Body)
		_ = done.Body.Close()
	}
}

// executeAgainstOrigin performs the origin round trip the connector would do.
func executeAgainstOrigin(item wire.DispatchRequest, target string) (wire.DispatchResponse, error) {
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

func TestReverseRegistrationRejectsBadCredentials(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, server := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")
	probe := func(member, generation, credential string) *http.Response {
		resp, err := reverseCall(t, server.Client(), server.URL+"/mcp-source-link-reverse", http.Header{
			"X-Source-Member":     {member},
			"X-Source-Generation": {generation},
			"Authorization":       {"Bearer " + credential},
		}, nil)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	if resp := probe(workerA, "1", "wrong"); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad credential accepted: %d", resp.StatusCode)
	}
	if resp := probe(workerA, "9", notSecret); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("stale generation accepted: %d", resp.StatusCode)
	}
	if resp := probe("missing", "1", notSecret); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown member accepted: %d", resp.StatusCode)
	}
	resp := probe(workerA, "1", notSecret)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("valid registration rejected: %d", resp.StatusCode)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	_ = resp.Body.Close()
	if !strings.Contains(string(body), workerA) {
		t.Fatalf("registration body missing worker id: %s", body)
	}
	resp = probe(workerA, "1", notSecret)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("duplicate session reported %d, want 409 so a live session is not misreported as a bad credential", resp.StatusCode)
	}
	_ = resp.Body.Close()

	session := f.runtime.Existing(workerA)
	if session == nil {
		t.Fatal("session not stored")
	}
	session.Close()
	resp = probe(workerA, "1", notSecret)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("re-registration after close rejected: %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
}

// readyReverse reports whether a session is registered and has proven its
// catalog, which is the condition forwarding depends on. The client's peer
// cache is refreshed from the fake control plane first, because in production
// the reconcile loop does exactly that every two seconds.
func readyReverse(t *testing.T, f *fixture, id string) bool {
	t.Helper()
	session := f.runtime.Existing(id)
	if session == nil || !session.Ready(sourceID) {
		return false
	}
	f.refreshPeers()
	p, ok := f.control.Peer(id)
	return ok && p.CatalogFingerprint != nil && *p.CatalogFingerprint == p.ExpectedCatalog
}

// startSession boots a connector against the plane and waits for readiness.
func startSession(t *testing.T, f *fixture, p config.Peer) {
	t.Helper()
	server := httptest.NewServer(f.reverse.Routes())
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go runConnector(ctx, t, server.Client(), server.URL, p)
	eventually(t, func() bool { return f.runtime.Existing(p.WorkerID) != nil })
}
