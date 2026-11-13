package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/rancher/remotedialer"
)

const (
	sourceID   = "11111111-1111-1111-1111-111111111111"
	datasetID  = "postgres-experiment"
	workerA    = "22222222-2222-2222-2222-222222222222"
	workerB    = "33333333-3333-3333-3333-333333333333"
	serviceKey = "service-auth-not-secret-for-local-tests"
)

var notSecret = fmt.Sprintf("%064d", 0)

type fixture struct {
	control *control
	runtime *runtime
	worker  *httptest.Server
	data    *httptest.Server
}

func testPeer(id string, generation uint64, key ed25519.PublicKey) peer {
	return peer{
		WorkerID: id, SourceID: sourceID, Label: strings.Split(id, "-")[0][:4], Target: "127.0.0.1:18081",
		DatasetID: datasetID, ProofKey: base64.StdEncoding.EncodeToString(key), Generation: generation,
		Phase: "active", CredentialDigest: string(secretDigest(notSecret)), ExpectedCatalog: "",
	}
}

func secretDigest(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func setupFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{}
	workerMux := http.NewServeMux()
	state := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer "+serviceKey {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if req.URL.Path == "/v1/remotedialer/peers" {
			f.control.mu.RLock()
			peers := make([]peer, 0, len(f.control.workers))
			for _, p := range f.control.workers {
				peers = append(peers, p)
			}
			f.control.mu.RUnlock()
			_ = json.NewEncoder(w).Encode(peers)
			return
		}
		if req.URL.Path == "/v1/remotedialer/report" {
			var report healthReport
			if err := json.NewDecoder(req.Body).Decode(&report); err != nil {
				t.Error(err)
				return
			}
			f.control.mu.Lock()
			member, ok := f.control.workers[report.WorkerID]
			if ok && member.Generation == report.Generation && member.Phase == "active" {
				member.CatalogFingerprint = report.CatalogFingerprint
				f.control.workers[report.WorkerID] = member
			}
			f.control.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"admitted":true}`))
			return
		}
		http.NotFound(w, req)
	}))
	f.control = &control{base: state.URL, token: serviceKey, tokenDigest: secretDigest(serviceKey), client: state.Client(), workers: make(map[string]peer)}
	f.runtime = newRuntime(f.control)
	workerMux.HandleFunc("GET /mcp-remotedialer-worker", f.runtime.worker)
	f.worker = httptest.NewServer(workerMux)
	dataMux := http.NewServeMux()
	dataMux.HandleFunc("/sources/{source}/{path...}", f.runtime.forward)
	dataMux.HandleFunc("POST /v1/remotedialer/revoke", f.runtime.revokeHTTP)
	f.data = httptest.NewServer(dataMux)
	t.Cleanup(func() {
		for _, client := range []*httptest.Server{state, f.worker, f.data} {
			client.CloseClientConnections()
			client.Close()
		}
	})
	return f
}

func (f *fixture) setPeer(p peer, fingerprint string) {
	f.control.mu.Lock()
	p.CatalogFingerprint = &fingerprint
	f.control.workers[p.WorkerID] = p
	f.control.mu.Unlock()
}

func (f *fixture) revokePeer(id string) {
	f.control.mu.Lock()
	member := f.control.workers[id]
	member.Phase = "revoked"
	member.Generation++
	member.CredentialDigest = secretDigest("revoked")
	f.control.workers[id] = member
	f.control.mu.Unlock()
}

func signedOrigin(t *testing.T, name string, signingKey ed25519.PrivateKey) (*httptest.Server, string) {
	t.Helper()
	catalog, err := canonicalCatalog([]byte(`{"openapi":"3.1.0","info":{"title":"experiment","version":"1"},"paths":{"/read":{"get":{"operationId":"read_source"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	privateKey := signingKey
	mux := http.NewServeMux()
	mux.HandleFunc("/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		content, _ := json.Marshal(map[string]any{"openapi": "3.1.0", "info": map[string]any{"title": "experiment", "version": "1"}, "paths": map[string]any{"/read": map[string]any{"get": map[string]any{"operationId": "read_source"}}}})
		nonce := r.Header.Get("X-Source-Nonce")
		sig := ed25519.Sign(privateKey, []byte(nonce+"\n"+sourceID+"\n"+datasetID+"\n"+catalog))
		w.Header().Set("X-Source-Signature", base64.StdEncoding.EncodeToString(sig))
		_, _ = w.Write(content)
	})
	mux.HandleFunc("/read", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" || r.Header.Get("X-Litellm-Api-Key") != "" {
			http.Error(w, "gateway credentials leaked", http.StatusBadRequest)
			return
		}
		_, _ = w.Write([]byte(name))
	})
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server, catalog
}

func connect(ctx context.Context, f *fixture, p peer, target string) error {
	wsURL := "ws" + strings.TrimPrefix(f.worker.URL, "http") + "/mcp-remotedialer-worker"
	headers := http.Header{"X-Source-Member": {p.WorkerID}, "X-Source-Generation": {strconv.FormatUint(p.Generation, 10)}, "Authorization": {"Bearer " + notSecret}}
	dialer := &websocket.Dialer{HandshakeTimeout: 2 * time.Second}
	allowed := func(network, address string) bool { return network == "tcp" && address == slot(sourceID) }
	dial := func(ctx context.Context, network, address string) (net.Conn, error) {
		if !allowed(network, address) {
			return nil, fmt.Errorf("denied")
		}
		return (&net.Dialer{Timeout: time.Second}).DialContext(ctx, "tcp", target)
	}
	return remotedialer.ConnectToProxyWithDialer(ctx, wsURL, headers, allowed, dialer, dial, nil)
}

func ready(t *testing.T, f *fixture, id string) bool {
	f.runtime.mu.Lock()
	member := f.runtime.sessions[id]
	f.runtime.mu.Unlock()
	if member == nil || !f.runtime.relay.HasSession(id) || !member.ready(sourceID) {
		return false
	}
	p, _ := f.control.peer(id)
	return p.CatalogFingerprint != nil && *p.CatalogFingerprint == p.ExpectedCatalog
}

func eventually(t *testing.T, check func() bool) {
	t.Helper()
	until := time.Now().Add(6 * time.Second)
	for time.Now().Before(until) {
		if check() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("condition not reached")
}

func read(t *testing.T, url string, method string, headers http.Header) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	for name, values := range headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

func TestSyntheticCredentials(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1/mcp-remotedialer-worker", nil)
	req.Header.Set("Authorization", "Bearer "+notSecret)
	if !sameSecret(secretDigest(notSecret), bearer(req)) {
		t.Fatalf("digest contract changed: supplied-length=%d", len(bearer(req)))
	}
	if sameSecret(secretDigest(notSecret), "wrong"+notSecret) {
		t.Fatal("digest comparison accepted a different credential")
	}
}

func TestSignedPeerReadSharingAndRevoke(t *testing.T) { 
	pub, signing, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	originA, catalog := signedOrigin(t, "A", signing)
	originB, _ := signedOrigin(t, "B", signing)
	_, wrongSigning, _ := ed25519.GenerateKey(rand.Reader)
	wrongPeer, _ := signedOrigin(t, "C", wrongSigning)
	f := setupFixture(t)
	a := testPeer(workerA, 1, pub)
	a.Generation = 1
	a.Target = strings.TrimPrefix(originA.URL, "http://")
	a.ExpectedCatalog = catalog
	b := testPeer(workerB, 1, pub)
	b.Target = strings.TrimPrefix(originB.URL, "http://")
	b.ExpectedCatalog = catalog
	c := testPeer("44444444-4444-4444-4444-444444444444", 1, pub)
	c.Target = strings.TrimPrefix(wrongPeer.URL, "http://")
	c.ExpectedCatalog = catalog
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	for _, p := range []peer{a, b, c} {
		f.setPeer(p, "")
		go func() {
			if err := connect(ctx, f, p, p.Target); err != nil {
				t.Logf("connector stopped: %v", err)
			}
		}()
	}
	eventually(t, func() bool { return ready(t, f, a.WorkerID) && ready(t, f, b.WorkerID) })
	got := map[string]int{}
	requestHeaders := http.Header{"Authorization": {"Bearer " + serviceKey}, "Cookie": {"private=not-forwarded"}}
	for range 12 {
		status, body := read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, requestHeaders)
		if status != http.StatusOK {
			t.Fatalf("read status %d %s", status, body)
		}
		got[body]++
	}
	if got["A"] != 6 || got["B"] != 6 || got["C"] != 0 {
		t.Fatalf("unexpected distribution: %v", got)
	}
	status, body := read(t, f.data.URL+"/sources/"+sourceID+"/write", http.MethodPost, requestHeaders)
	if status != http.StatusConflict {
		t.Fatalf("unreviewed write got %d %s", status, body)
	}
	requestHeaders.Set("Mcp-Session-Id", "any")
	status, _ = read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, requestHeaders)
	if status != http.StatusBadRequest {
		t.Fatalf("stateful read status %d", status)
	}
	request, _ := http.NewRequest(http.MethodPost, f.data.URL+"/v1/remotedialer/revoke", nil)
	request.Header.Set("Authorization", "Bearer "+serviceKey)
	request.Header.Set("X-Source-Member", workerB)
	request.Header.Set("X-Source-Id", sourceID)
	f.revokePeer(workerB)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	eventually(t, func() bool { return !f.runtime.relay.HasSession(workerB) })
	for range 4 {
		status, body = read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, http.Header{"Authorization": {"Bearer " + serviceKey}})
		if status != http.StatusOK || body != "A" {
			t.Fatalf("single member after revoke got %d %s", status, body)
		}
	}
}

func TestDrainExcludesDispatchButKeepsSession(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f := setupFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = connect(ctx, f, p, p.Target) }()
	eventually(t, func() bool { return ready(t, f, workerA) })
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	status, body := read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusOK || body != "A" {
		t.Fatalf("pre-drain read got %d %s", status, body)
	}
	f.control.mu.Lock()
	p.Phase = "draining"
	f.control.workers[workerA] = p
	f.control.mu.Unlock()
	f.runtime.reconcile()
	eventually(t, func() bool { return f.runtime.pick(sourceID, "") == nil })
	if !f.runtime.relay.HasSession(workerA) {
		t.Fatal("drain closed the live session")
	}
	status, _ = read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
	if status != http.StatusServiceUnavailable {
		t.Fatalf("drained member still received dispatch: %d", status)
	}
	f.control.mu.Lock()
	p.Phase = "active"
	f.control.workers[workerA] = p
	f.control.mu.Unlock()
	f.runtime.reconcile()
	eventually(t, func() bool {
		status, body = read(t, f.data.URL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
		return status == http.StatusOK && body == "A"
	})
}

func TestRevokedConnectorCannotReconnect(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f := setupFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = connect(ctx, f, p, p.Target) }()
	eventually(t, func() bool { return ready(t, f, workerA) })
	f.control.mu.Lock()
	p.Phase = "revoked"
	p.Generation = 2
	p.CredentialDigest = secretDigest("stale")
	f.control.workers[workerA] = p
	f.control.mu.Unlock()
	request, _ := http.NewRequest(http.MethodPost, f.data.URL+"/v1/remotedialer/revoke", nil)
	request.Header.Set("Authorization", "Bearer "+serviceKey)
	request.Header.Set("X-Source-Member", workerA)
	request.Header.Set("X-Source-Id", sourceID)
	resp, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	start := time.Now()
	for time.Since(start) < time.Second {
		dialer := websocket.Dialer{HandshakeTimeout: time.Second}
		ws, _, _ := dialer.DialContext(context.Background(), "ws"+strings.TrimPrefix(f.worker.URL, "http")+"/mcp-remotedialer-worker", http.Header{"X-Source-Member": {workerA}, "X-Source-Generation": {"1"}, "Authorization": {"Bearer " + notSecret}})
		if ws != nil {
			_ = ws.Close()
		}
	}
	if f.runtime.relay.HasSession(workerA) {
		t.Fatal("stale session resurrected")
	}
}
