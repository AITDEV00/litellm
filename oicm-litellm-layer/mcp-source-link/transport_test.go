package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

const (
	sourceID   = "11111111-1111-1111-1111-111111111111"
	datasetID  = "postgres-experiment"
	workerA    = "22222222-2222-2222-2222-222222222222"
	workerB    = "33333333-3333-3333-3333-333333333333"
	serviceKey = "service-auth-not-secret-for-local-tests"
)

var notSecret = fmt.Sprintf("%064d", 0)

// fixtureHTTP2Client speaks the h2c protocol the data plane now requires; a
// plain HTTP/1.1 client is rejected by the fixture server, matching the
// product listener's protocol set.
var fixtureHTTP2Client = &http.Client{Transport: &http2.Transport{
	AllowHTTP: true,
	DialTLSContext: func(ctx context.Context, network, address string, _ *tls.Config) (net.Conn, error) {
		return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, address)
	},
}}

type fixture struct {
	control *control
	runtime *runtime
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
	state := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer "+serviceKey {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if req.URL.Path == "/v1/source-link/peers" {
			f.control.mu.RLock()
			peers := make([]peer, 0, len(f.control.workers))
			for _, p := range f.control.workers {
				peers = append(peers, p)
			}
			f.control.mu.RUnlock()
			_ = json.NewEncoder(w).Encode(peers)
			return
		}
		if req.URL.Path == "/v1/source-link/report" {
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
	dataMux := http.NewServeMux()
	dataMux.HandleFunc("/sources/{source}/{path...}", f.runtime.forward)
	dataMux.HandleFunc("POST /v1/source-link/revoke", f.runtime.revokeHTTP)
	dataServer := &http.Server{Handler: dataMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 32 << 10, HTTP2: reverseHTTP2Config(), Protocols: h2cProtocols()}
	dataListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f.data = httptest.NewUnstartedServer(dataMux)
	f.data.Config = dataServer
	f.data.Listener = dataListener
	f.data.Start()
	t.Cleanup(func() {
		for _, client := range []*httptest.Server{state, f.data} {
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
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	return server, catalog
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
	resp, err := fixtureHTTP2Client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

func TestSyntheticCredentials(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1/mcp-source-link-reverse", nil)
	req.Header.Set("Authorization", "Bearer "+notSecret)
	if !sameSecret(secretDigest(notSecret), bearer(req)) {
		t.Fatalf("digest contract changed: supplied-length=%d", len(bearer(req)))
	}
	if sameSecret(secretDigest(notSecret), "wrong"+notSecret) {
		t.Fatal("digest comparison accepted a different credential")
	}
}

func readyReverse(t *testing.T, f *fixture, id string) bool {
	t.Helper()
	f.runtime.mu.Lock()
	session := f.runtime.sessions[id]
	f.runtime.mu.Unlock()
	if session == nil || !session.ready(sourceID) {
		return false
	}
	p, _ := f.control.peer(id)
	return p.CatalogFingerprint != nil && *p.CatalogFingerprint == p.ExpectedCatalog
}

func startSession(t *testing.T, f *fixture, p peer) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /mcp-source-link-reverse", f.runtime.reverseRegister)
	mux.HandleFunc("POST /mcp-source-link-hold", f.runtime.reverseHold)
	mux.HandleFunc("POST /mcp-source-link-dispatch", f.runtime.reverseDispatch)
	mux.HandleFunc("POST /mcp-source-link-respond", f.runtime.reverseRespond)
	server := httptest.NewServer(mux)
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go reverseConnector(ctx, t, server, p)
	eventually(t, func() bool {
		f.runtime.mu.Lock()
		defer f.runtime.mu.Unlock()
		return f.runtime.sessions[p.WorkerID] != nil
	})
	f.runtime.mu.Lock()
	session := f.runtime.sessions[p.WorkerID].(*reverseSession)
	f.runtime.mu.Unlock()
	go f.runtime.reverseProbeLoop(ctx, session)
}

func TestReversePeerReadSharingAndRevoke(t *testing.T) {
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
	for _, p := range []peer{a, b, c} {
		f.setPeer(p, "")
		startSession(t, f, p)
	}
	eventually(t, func() bool { return readyReverse(t, f, a.WorkerID) && readyReverse(t, f, b.WorkerID) })
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
	request, _ := http.NewRequest(http.MethodPost, f.data.URL+"/v1/source-link/revoke", nil)
	request.Header.Set("Authorization", "Bearer "+serviceKey)
	request.Header.Set("X-Source-Member", workerB)
	request.Header.Set("X-Source-Id", sourceID)
	f.revokePeer(workerB)
	response, err := fixtureHTTP2Client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	eventually(t, func() bool {
		f.runtime.mu.Lock()
		defer f.runtime.mu.Unlock()
		return f.runtime.sessions[workerB] == nil
	})
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
	startSession(t, f, p)
	eventually(t, func() bool { return readyReverse(t, f, workerA) })
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
	f.runtime.mu.Lock()
	session := f.runtime.sessions[workerA]
	f.runtime.mu.Unlock()
	if session == nil {
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
	startSession(t, f, p)
	eventually(t, func() bool { return readyReverse(t, f, workerA) })
	f.control.mu.Lock()
	p.Phase = "revoked"
	p.Generation = 2
	p.CredentialDigest = secretDigest("stale")
	f.control.workers[workerA] = p
	f.control.mu.Unlock()
	f.runtime.reconcile()
	eventually(t, func() bool {
		f.runtime.mu.Lock()
		defer f.runtime.mu.Unlock()
		return f.runtime.sessions[workerA] == nil
	})
	_, server := reverseFixture(t)
	probe := func(generation, credential string) int {
		req, _ := http.NewRequest(http.MethodPost, server.URL+"/mcp-source-link-reverse", nil)
		req.Header.Set("X-Source-Member", workerA)
		req.Header.Set("X-Source-Generation", generation)
		req.Header.Set("Authorization", "Bearer "+credential)
		resp, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	if code := probe("2", "stale-credential"); code != http.StatusUnauthorized {
		t.Fatalf("revoked member re-registered: %d", code)
	}
	if code := probe("1", notSecret); code != http.StatusUnauthorized {
		t.Fatalf("stale generation re-registered: %d", code)
	}
}
