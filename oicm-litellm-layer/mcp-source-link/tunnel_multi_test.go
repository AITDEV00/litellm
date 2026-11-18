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
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

// TestTunnelMultiChannelStress drives many concurrent channels through one
// broker worker and one agent client: each channel is an independent mock
// MCP source (own source ID, proof key, origin, and reverse tunnel), and the
// agent fires the same number of simultaneous requests down every channel
// at one shared release barrier. The full matrix is channels x per-channel
// concurrency requests in flight at the same instant. Skipped unless
// MCP_TUNNEL_MULTI=1.
func TestTunnelMultiChannelStress(t *testing.T) {
	if os.Getenv("MCP_TUNNEL_MULTI") != "1" {
		t.Skip("set MCP_TUNNEL_MULTI=1 to run the multi-channel stress")
	}
	channels, _ := strconv.Atoi(envOr("MCP_TUNNEL_MULTI_CHANNELS", "16"))
	perChannel, _ := strconv.Atoi(envOr("MCP_TUNNEL_MULTI_N", "32768"))
	rounds, _ := strconv.Atoi(envOr("MCP_TUNNEL_MULTI_ROUNDS", "1"))
	delay, _ := time.ParseDuration(envOr("MCP_TUNNEL_MULTI_DELAY", "2s"))
	total := channels * perChannel
	t.Logf("channels=%d per-channel=%d total-simultaneous=%d rounds=%d origin-delay=%s", channels, perChannel, total, rounds, delay)

	type channel struct {
		fixture  *fixture
		worker   string
		sourceID string
		url      string
		marker   string
	}
	var chans []channel
	connectors := make([]context.CancelFunc, 0, channels)
	originForPeer := func(p peer) *http.Transport {
		transport := &http.Transport{
			MaxIdleConns:          65536,
			MaxIdleConnsPerHost:   65536,
			IdleConnTimeout:       90 * time.Second,
			DisableCompression:    true,
			ResponseHeaderTimeout: 30 * time.Second,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", p.Target)
			},
		}
		t.Cleanup(transport.CloseIdleConnections)
		return transport
	}
	for index := range channels {
		f, _, target := tunnelFixture(t)
		id := fmt.Sprintf("44444444-4444-4444-4444-%012d", index)
		marker := fmt.Sprintf("channel-%02d", index)
		_, signing, _ := ed25519.GenerateKey(rand.Reader)
		origin, catalog := multiChannelOrigin(t, id, marker, signing, delay)
		p := multiChannelPeer(id, marker, signing.Public().(ed25519.PublicKey))
		p.Target = strings.TrimPrefix(origin.URL, "http://")
		p.ExpectedCatalog = catalog
		f.setPeer(p, "")
		ctx, cancel := context.WithCancel(context.Background())
		connectors = append(connectors, cancel)
		go tunnelConnectorWithOrigin(ctx, t, target, p, originForPeer(p))
		chans = append(chans, channel{fixture: f, worker: id, sourceID: id, url: f.dataURL + "/sources/" + id + "/read", marker: marker})
	}
	t.Cleanup(func() {
		for _, cancel := range connectors {
			cancel()
		}
	})
	for _, ch := range chans {
		ch := ch
		readyProbe := func() bool {
			f := ch.fixture
			f.runtime.mu.Lock()
			session := f.runtime.sessions[ch.worker]
			f.runtime.mu.Unlock()
			if session == nil || !session.ready(ch.sourceID) {
				return false
			}
			p, ok := f.control.peer(ch.worker)
			return ok && p.CatalogFingerprint != nil && *p.CatalogFingerprint == p.ExpectedCatalog
		}
		eventually(t, readyProbe)
	}
	t.Logf("all %d channels admitted and healthy", channels)

	agent := &http.Client{Transport: &http2.Transport{
		StrictMaxConcurrentStreams: false,
		AllowHTTP:                  true,
		DialTLSContext: func(ctx context.Context, network, address string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, address)
		},
	}}
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	release := make(chan struct{})
	var ready, done sync.WaitGroup
	var successes, failures atomic.Int64
	var firstFailures struct {
		mu   sync.Mutex
		seen []string
	}

	for round := 1; round <= rounds; round++ {
		successes.Store(0)
		failures.Store(0)
		started := time.Now()
		ready.Add(total)
		for _, ch := range chans {
			for range perChannel {
				done.Add(1)
				go func(ch channel) {
					defer done.Done()
					ready.Done()
					<-release
					req, err := http.NewRequest(http.MethodGet, ch.url, nil)
					if err != nil {
						failures.Add(1)
						return
					}
					for name, values := range headers {
						req.Header[name] = values
					}
					resp, err := agent.Do(req)
					if err != nil {
						recordFailure(&firstFailures, err.Error())
						failures.Add(1)
						return
					}
					body, _ := io.ReadAll(io.LimitReader(resp.Body, 128))
					resp.Body.Close()
					if resp.ProtoMajor != 2 {
						recordFailure(&firstFailures, "unexpected load protocol: "+resp.Proto)
						failures.Add(1)
						return
					}
					if resp.StatusCode != http.StatusOK || string(body) != ch.marker {
						recordFailure(&firstFailures, fmt.Sprintf("status=%d body=%s", resp.StatusCode, body))
						failures.Add(1)
						return
					}
					successes.Add(1)
				}(ch)
			}
		}
		ready.Wait()
		close(release)
		done.Wait()
		release = make(chan struct{})
		ok, fail := successes.Load(), failures.Load()
		t.Logf("round=%d channels=%d per-channel=%d total=%d wall=%s ok=%d failures=%d",
			round, channels, perChannel, total, time.Since(started).Round(time.Millisecond), ok, fail)
		if fail > 0 {
			firstFailures.mu.Lock()
			sample := append([]string(nil), firstFailures.seen...)
			firstFailures.mu.Unlock()
			if len(sample) > 5 {
				sample = sample[:5]
			}
			t.Errorf("round %d failed: %d/%d succeeded; first failures: %s", round, ok, total, strings.Join(sample, " | "))
		}
		time.Sleep(2 * time.Second)
	}
}

func recordFailure(store *struct {
	mu   sync.Mutex
	seen []string
}, message string,
) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if len(store.seen) < 10 {
		store.seen = append(store.seen, message)
	}
}

// multiChannelOrigin is a mock MCP server per channel: its own source ID,
// dataset, signing key, and a fixed per-request delay on /read, with an
// OpenAPI catalog and proof signature bound to this channel's identity.
func multiChannelOrigin(t *testing.T, id, marker string, signing ed25519.PrivateKey, delay time.Duration) (*httptest.Server, string) {
	t.Helper()
	dataset := "dataset-" + marker
	content, _ := json.Marshal(map[string]any{
		"openapi": "3.1.0",
		"info":    map[string]any{"title": marker, "version": "1"},
		"paths":   map[string]any{"/read": map[string]any{"get": map[string]any{"operationId": "read_" + marker}}},
	})
	catalog, err := canonicalCatalog(content)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		nonce := r.Header.Get("X-Source-Nonce")
		sig := ed25519.Sign(signing, []byte(nonce+"\n"+id+"\n"+dataset+"\n"+catalog))
		w.Header().Set("X-Source-Signature", base64.StdEncoding.EncodeToString(sig))
		_, _ = w.Write(content)
	})
	mux.HandleFunc("/read", func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(delay)
		_, _ = w.Write([]byte(marker))
	})
	server := httptest.NewServer(mux)
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
	})
	return server, catalog
}

func multiChannelPeer(id, marker string, key ed25519.PublicKey) peer {
	return peer{
		WorkerID: id, SourceID: id, Label: "chan-" + marker, Target: "127.0.0.1:18081",
		DatasetID: "dataset-" + marker, ProofKey: base64.StdEncoding.EncodeToString(key), Generation: 1,
		Phase: "active", CredentialDigest: string(secretDigest(notSecret)), ExpectedCatalog: "",
	}
}
