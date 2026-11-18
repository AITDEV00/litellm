// Package main's integration suite drives the real HTTP surfaces of every
// slice together: a fake control plane, the long-poll and tunnel planes
// mounted on a live listener, and the shipped connector code dialing in.
//
// Tests stay in package main because they exercise the whole binary, not one
// slice in isolation. Slice-level units live beside their package instead.
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/net/http2"

	"oicm-litellm-layer/mcp-source-link/internal/catalog"
	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/reverseh2"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

const (
	sourceID   = "11111111-1111-1111-1111-111111111111"
	datasetID  = "postgres-experiment"
	workerA    = "22222222-2222-2222-2222-222222222222"
	workerB    = "33333333-3333-3333-3333-333333333333"
	serviceKey = "service-auth-not-secret-for-local-tests"
)

var notSecret = fmt.Sprintf("%064d", 0)

func fixtureHTTP2Client(t *testing.T) *http.Client {
	t.Helper()
	transport := &http2.Transport{
		AllowHTTP: true,
		DialTLSContext: func(ctx context.Context, network, address string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, address)
		},
	}
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport}
}

type fixture struct {
	control    *control.Client
	runtime    *pool.Runtime
	tunnels    *tunnel.Runtime
	reverse    *reverseh2.Plane
	data       net.Listener
	dataURL    string
	dataServer *http.Server
	lock       sync.Mutex
	peers      map[string]config.Peer
}

func secretDigest(value string) string { return control.DigestValue(value) }

func testPeer(id string, generation uint64, key ed25519.PublicKey) config.Peer {
	return config.Peer{
		WorkerID: id, SourceID: sourceID, Label: strings.Split(id, "-")[0][:4], Target: "127.0.0.1:18081",
		DatasetID: datasetID, ProofKey: base64.StdEncoding.EncodeToString(key), Generation: generation,
		Phase: "active", CredentialDigest: secretDigest(notSecret), ExpectedCatalog: "",
	}
}

// setupFixture stands up a fake control plane and the worker's data listener.
func setupFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{peers: map[string]config.Peer{}}
	state := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer "+serviceKey {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch req.URL.Path {
		case "/v1/source-link/peers":
			f.mu()
			peers := make([]config.Peer, 0, len(f.peers))
			for _, p := range f.peers {
				peers = append(peers, p)
			}
			f.unmu()
			_ = json.NewEncoder(w).Encode(peers)
		case "/v1/source-link/report":
			var report config.HealthReport
			if err := json.NewDecoder(req.Body).Decode(&report); err != nil {
				t.Error(err)
				return
			}
			f.mu()
			member, ok := f.peers[report.WorkerID]
			if ok && member.Generation == report.Generation && member.Phase == "active" {
				member.CatalogFingerprint = report.CatalogFingerprint
				f.peers[report.WorkerID] = member
			}
			f.unmu()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"admitted":true}`))
		default:
			http.NotFound(w, req)
		}
	}))
	stateClient := state.Client()
	// Rebuild through the production constructor path, which is what a worker
	// does at boot, so the fixture exercises the real client rather than a stub.
	cfg := config.Config{ControlURL: state.URL, ServiceToken: serviceKey}
	client, err := control.New(context.Background(), cfg, stateClient)
	if err != nil {
		t.Fatal(err)
	}
	f.control = client
	f.runtime = pool.New(client)
	f.tunnels = tunnel.New(f.runtime)
	f.reverse = reverseh2.New(f.runtime)

	dataMux := http.NewServeMux()
	dataMux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	dataMux.HandleFunc("/sources/{source}/{path...}", f.runtime.Forward)
	dataMux.HandleFunc("POST /v1/source-link/revoke", f.runtime.RevokeHTTP)
	dataServer := &http.Server{Handler: dataMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second,
		MaxHeaderBytes: 32 << 10, HTTP2: reverseh2.HTTP2Config(), Protocols: reverseh2.DataProtocols()}
	dataListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f.data = dataListener
	f.dataServer = dataServer
	f.dataURL = "http://" + dataListener.Addr().String()
	go func() { _ = dataServer.Serve(dataListener) }()
	t.Cleanup(func() {
		_ = dataServer.Close()
		state.CloseClientConnections()
		state.Close()
	})
	return f
}

// mu/unmu serialize access to the fixture's peer mirror, which stands in for
// the control plane's database. The client caches its own copy, refreshed via
// refreshPeers after each mutation, so tests exercise the real fetch path.
func (f *fixture) mu()   { f.lock.Lock() }
func (f *fixture) unmu() { f.lock.Unlock() }

func (f *fixture) refreshPeers() {
	if err := f.control.Refresh(context.Background()); err != nil {
		panic("fixture refresh failed: " + err.Error())
	}
}

func (f *fixture) setPeer(p config.Peer, fingerprint string) {
	f.mu()
	if fingerprint != "" {
		p.CatalogFingerprint = &fingerprint
	}
	f.peers[p.WorkerID] = p
	f.unmu()
	f.refreshPeers()
}

func (f *fixture) revokePeer(id string) {
	f.mu()
	member := f.peers[id]
	member.Phase = "revoked"
	member.Generation++
	member.CredentialDigest = secretDigest("revoked")
	f.peers[id] = member
	f.unmu()
	f.refreshPeers()
}

// setPhase updates a peer's phase in the fixture mirror.
func (f *fixture) setPhase(id string, phase string) {
	f.mu()
	member := f.peers[id]
	member.Phase = phase
	f.peers[id] = member
	f.unmu()
	f.refreshPeers()
}

func signedOrigin(t *testing.T, name string, signingKey ed25519.PrivateKey) (*httptest.Server, string) {
	t.Helper()
	digest, err := catalog.Canonical([]byte(`{"openapi":"3.1.0","info":{"title":"experiment","version":"1"},"paths":{"/read":{"get":{"operationId":"read_source"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		content, _ := json.Marshal(map[string]any{"openapi": "3.1.0", "info": map[string]any{"title": "experiment", "version": "1"}, "paths": map[string]any{"/read": map[string]any{"get": map[string]any{"operationId": "read_source"}}}})
		nonce := r.Header.Get("X-Source-Nonce")
		sig := ed25519.Sign(signingKey, []byte(nonce+"\n"+sourceID+"\n"+datasetID+"\n"+digest))
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
	return server, digest
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
	resp, err := fixtureHTTP2Client(t).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

func generateKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}
