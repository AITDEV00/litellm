package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"

	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
)

func reverseFixture(t *testing.T) (*fixture, *httptest.Server) {
	t.Helper()
	f := setupFixture(t)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /mcp-remotedialer-reverse", f.runtime.reverseRegister)
	mux.HandleFunc("POST /mcp-remotedialer-hold", f.runtime.reverseHold)
	mux.HandleFunc("POST /mcp-remotedialer-dispatch", f.runtime.reverseDispatch)
	mux.HandleFunc("POST /mcp-remotedialer-respond", f.runtime.reverseRespond)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return f, server
}

func reverseHeaders(p peer, credential string) http.Header {
	return http.Header{
		"X-Source-Member":     {p.WorkerID},
		"X-Source-Generation":  {strconv.FormatUint(p.Generation, 10)},
		"Authorization":        {"Bearer " + credential},
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
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	return resp, nil
}

func TestReverseRegistrationRejectsBadCredentials(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, server := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	base := server.URL
	client := server.Client()
	probe := func(member, generation, credential string) *http.Response {
		resp, err := reverseCall(t, client, base+"/mcp-remotedialer-reverse", http.Header{
			"X-Source-Member":     {member},
			"X-Source-Generation":  {generation},
			"Authorization":        {"Bearer " + credential},
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
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("duplicate session accepted: %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
	f.runtime.mu.Lock()
	session := f.runtime.reverse[workerA]
	f.runtime.mu.Unlock()
	if session == nil {
		t.Fatal("session not stored")
	}
	session.close()
	resp = probe(workerA, "1", notSecret)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("re-registration after close rejected: %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
}

// reverseConnector drives the connector side of the dispatch protocol
// against the fixture server with a real origin behind it.
func reverseConnector(ctx context.Context, t *testing.T, server *httptest.Server, p peer) {
	t.Helper()
	client := server.Client()
	base := server.URL
	headers := reverseHeaders(p, notSecret)
	resp, err := reverseCall(t, client, base+"/mcp-remotedialer-reverse", headers, nil)
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
		resp, err := reverseCall(t, client, base+"/mcp-remotedialer-hold", headers, nil)
		if err == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
		}
		holdCancel()
	}()
	const dispatchers = 4
	var workers sync.WaitGroup
	lost := make(chan struct{}, 1)
	for range dispatchers {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for {
				select {
				case <-holdCtx.Done():
					return
				case <-lost:
					return
				default:
				}
				resp, err := reverseCall(t, client, base+"/mcp-remotedialer-dispatch", headers, nil)
				if err != nil || resp.StatusCode != http.StatusOK {
					if resp != nil {
						_, _ = io.Copy(io.Discard, resp.Body)
						_ = resp.Body.Close()
					}
					return
				}
				var item dispatchRequest
				_ = json.NewDecoder(resp.Body).Decode(&item)
				_ = resp.Body.Close()
				payload, err := reverseExecute(t, p.Target, item)
				if err != nil {
					payload = dispatchResponse{ID: item.ID, Error: err.Error()}
				}
				encoded, _ := json.Marshal(payload)
				done, err := reverseCall(t, client, base+"/mcp-remotedialer-respond", headers, encoded)
				if err != nil {
					return
				}
				_, _ = io.Copy(io.Discard, done.Body)
				_ = done.Body.Close()
			}
		}()
	}
	workers.Wait()
}

func reverseExecute(t *testing.T, target string, item dispatchRequest) (dispatchResponse, error) {
	t.Helper()
	var reader io.Reader
	if item.Body != nil {
		reader = strings.NewReader(string(item.Body))
	}
	req, err := http.NewRequest(item.Method, "http://"+target+item.Path, reader)
	if err != nil {
		return dispatchResponse{}, err
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
		return dispatchResponse{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, reverseMaxBody))
	headers := make(map[string][]string, len(resp.Header))
	for name, values := range resp.Header {
		headers[name] = values
	}
	return dispatchResponse{ID: item.ID, Status: resp.StatusCode, Headers: headers, Body: body}, nil
}

func TestReverseForwardingAndRevoke(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, server := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go reverseConnector(ctx, t, server, p)
	eventually(t, func() bool {
		f.runtime.mu.Lock()
		defer f.runtime.mu.Unlock()
		return f.runtime.reverse[workerA] != nil
	})
	f.runtime.mu.Lock()
	session := f.runtime.reverse[workerA]
	f.runtime.mu.Unlock()
	go func() { f.runtime.reverseProbeLoop(ctx, session) }()
	eventually(t, func() bool { return f.runtime.hasActive(sourceID) })
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	status, body := read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("reverse read got %d %s", status, body)
	}
	f.revokePeer(workerA)
	f.runtime.reconcile()
	eventually(t, func() bool { return !f.runtime.hasActive(sourceID) })
}

func TestReverseHoldGatesDispatchOnWorkerCredential(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, server := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	base := server.URL
	client := server.Client()
	resp, err := reverseCall(t, client, base+"/mcp-remotedialer-hold", reverseHeaders(p, notSecret), nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("hold without registration accepted: %d", resp.StatusCode)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	resp, err = reverseCall(t, client, base+"/mcp-remotedialer-reverse", reverseHeaders(p, notSecret), nil)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("registration rejected: %d", resp.StatusCode)
	}
	bad := reverseHeaders(p, "wrong-credential")
	resp, err = reverseCall(t, client, base+"/mcp-remotedialer-dispatch", bad, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("dispatch with bad credential accepted: %d", resp.StatusCode)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	f.runtime.mu.Lock()
	session := f.runtime.reverse[workerA]
	f.runtime.mu.Unlock()
	session.close()
}

func TestReverseDispatchBoundAreConservative(t *testing.T) {
	config := reverseHTTP2Config()
	if config.MaxReceiveBufferPerStream != 64<<10 {
		t.Fatalf("stream window drifted: %d", config.MaxReceiveBufferPerStream)
	}
	if config.MaxConcurrentStreams < 1<<17 {
		t.Fatalf("stream limit too low: %d", config.MaxConcurrentStreams)
	}
}





