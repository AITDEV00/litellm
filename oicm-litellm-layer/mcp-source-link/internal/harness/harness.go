// Package harness is the shared integration fixture for the sidecar's tests.
//
// It stands up a fake control plane, the worker's data listener, and both
// dispatch planes on real listeners, then lets a test drive them through the
// exported API of every slice. It is a test-support package: nothing in the
// shipped binary imports it.
//
// It exists because the alternative, keeping the fixture in package main, meant
// every behavior test had to live in package main too, even when it exercised
// one slice. That put tests far from the code they describe.
package harness

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
	"oicm-litellm-layer/mcp-source-link/internal/listen"
	"oicm-litellm-layer/mcp-source-link/internal/pool"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

// Fixed identities every test shares, so failures are comparable across files.
const (
	SourceID   = "11111111-1111-1111-1111-111111111111"
	DatasetID  = "postgres-experiment"
	WorkerA    = "22222222-2222-2222-2222-222222222222"
	WorkerB    = "33333333-3333-3333-3333-333333333333"
	ServiceKey = "service-auth-not-secret-for-local-tests"
)

// NotSecret is a well-formed but non-secret credential used by fixtures.
var NotSecret = fmt.Sprintf("%064d", 0)

// Fixture is one running worker under test.
type Fixture struct {
	Control    *control.Client
	Runtime    *pool.Runtime
	Tunnels    *tunnel.Runtime
	DataURL    string
	dataServer *http.Server
	lock       sync.Mutex
	peers      map[string]config.Peer
}

// HTTP2Client returns a client that speaks cleartext HTTP/2 to the data plane.
func HTTP2Client(t *testing.T) *http.Client {
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

// SecretDigest is the digest form the control plane stores and compares.
func SecretDigest(value string) string { return control.DigestValue(value) }

// Peer builds an active peer record with a valid credential digest.
func Peer(id string, generation uint64, key ed25519.PublicKey) config.Peer {
	return config.Peer{
		WorkerID: id, SourceID: SourceID, Label: strings.Split(id, "-")[0][:4], Target: "127.0.0.1:18081",
		DatasetID: DatasetID, ProofKey: base64.StdEncoding.EncodeToString(key), Generation: generation,
		Phase: "active", CredentialDigest: SecretDigest(NotSecret), ExpectedCatalog: "",
	}
}

// New stands up the fake control plane and the worker's data listener.
func New(t *testing.T) *Fixture {
	t.Helper()
	f := &Fixture{peers: map[string]config.Peer{}}
	state := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer "+ServiceKey {
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
	cfg := config.Config{ControlURL: state.URL, ServiceToken: ServiceKey}
	client, err := control.New(context.Background(), cfg, stateClient)
	if err != nil {
		t.Fatal(err)
	}
	f.Control = client
	f.Runtime = pool.New(client)
	f.Tunnels = tunnel.New(f.Runtime)

	dataMux := http.NewServeMux()
	dataMux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	dataMux.HandleFunc("/sources/{source}/{path...}", f.Runtime.Forward)
	dataMux.HandleFunc("POST /v1/source-link/revoke", f.Runtime.RevokeHTTP)
	dataServer := &http.Server{Handler: dataMux, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 90 * time.Second,
		MaxHeaderBytes: 32 << 10, HTTP2: listen.HTTP2Config(), Protocols: listen.Data()}
	dataListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f.dataServer = dataServer
	f.DataURL = "http://" + dataListener.Addr().String()
	go func() { _ = dataServer.Serve(dataListener) }()
	t.Cleanup(func() {
		_ = dataServer.Close()
		state.CloseClientConnections()
		state.Close()
	})
	return f
}

func (f *Fixture) mu()   { f.lock.Lock() }
func (f *Fixture) unmu() { f.lock.Unlock() }

// RefreshPeers pulls the fixture's peer mirror into the client's cache, which
// the production reconcile loop does every two seconds.
func (f *Fixture) RefreshPeers() {
	if err := f.Control.Refresh(context.Background()); err != nil {
		panic("fixture refresh failed: " + err.Error())
	}
}

// SetPeer records a peer and refreshes the client cache.
func (f *Fixture) SetPeer(p config.Peer, fingerprint string) {
	f.mu()
	if fingerprint != "" {
		p.CatalogFingerprint = &fingerprint
	}
	f.peers[p.WorkerID] = p
	f.unmu()
	f.RefreshPeers()
}

// RevokePeer marks a peer revoked and advances its generation.
func (f *Fixture) RevokePeer(id string) {
	f.mu()
	member := f.peers[id]
	member.Phase = "revoked"
	member.Generation++
	member.CredentialDigest = SecretDigest("revoked")
	f.peers[id] = member
	f.unmu()
	f.RefreshPeers()
}

// SetPhase changes a peer's phase.
func (f *Fixture) SetPhase(id string, phase string) {
	f.mu()
	member := f.peers[id]
	member.Phase = phase
	f.peers[id] = member
	f.unmu()
	f.RefreshPeers()
}

// SignedOrigin starts an origin serving a correctly signed catalog, returning
// the server and the catalog digest the control plane must expect.
func SignedOrigin(t *testing.T, name string, signingKey ed25519.PrivateKey) (*httptest.Server, string) {
	t.Helper()
	digest, err := catalog.Canonical([]byte(`{"openapi":"3.1.0","info":{"title":"experiment","version":"1"},"paths":{"/read":{"get":{"operationId":"read_source"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		content, _ := json.Marshal(map[string]any{"openapi": "3.1.0", "info": map[string]any{"title": "experiment", "version": "1"}, "paths": map[string]any{"/read": map[string]any{"get": map[string]any{"operationId": "read_source"}}}})
		nonce := r.Header.Get("X-Source-Nonce")
		sig := ed25519.Sign(signingKey, []byte(nonce+"\n"+SourceID+"\n"+DatasetID+"\n"+digest))
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

// Eventually retries a condition until it holds or the deadline passes.
func Eventually(t *testing.T, check func() bool) {
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

// Read performs one data-plane request and returns status and body.
func Read(t *testing.T, url string, method string, headers http.Header) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	for name, values := range headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := HTTP2Client(t).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

// Key generates an ed25519 keypair.
func Key(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}
