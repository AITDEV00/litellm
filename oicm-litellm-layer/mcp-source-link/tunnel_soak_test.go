package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
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

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

// TestTunnelSoakReproducesPublicFailure runs the same traffic shape as the
// 2026-11-18 public batches, fully in-process: many concurrent nested
// Executes multiplexed over one local tunnel, repeated, against an origin
// with a fixed delay. It is skipped unless MCP_TUNNEL_SOAK=1.
//
// The public failures had this signature: a subset of responses turned 502
// with "rpc error: code = Canceled", while the outer tunnel stream stayed
// established; in the two catastrophic runs the outer stream later died
// with "RST_STREAM with error code: PROTOCOL_ERROR". This test records,
// per round, the response code mix and every distinct error string, so a
// local reproduction is directly comparable to the saved public logs.
func TestTunnelSoakReproducesPublicFailure(t *testing.T) {
	if os.Getenv("MCP_TUNNEL_SOAK") != "1" {
		t.Skip("set MCP_TUNNEL_SOAK=1 to run the tunnel soak")
	}
	rounds, _ := strconv.Atoi(envOr("MCP_TUNNEL_SOAK_ROUNDS", "8"))
	concurrency, _ := strconv.Atoi(envOr("MCP_TUNNEL_SOAK_N", "16384"))
	delay, _ := time.ParseDuration(envOr("MCP_TUNNEL_SOAK_DELAY", "2s"))
	t.Logf("rounds=%d concurrency=%d origin-delay=%s", rounds, concurrency, delay)

	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	origin, catalog := signedOrigin(t, "soak", signing)
	f, _, target := tunnelFixture(t)
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/read" {
			time.Sleep(delay)
		}
		proxyReq, err := http.NewRequestWithContext(r.Context(), r.Method, origin.URL+r.URL.RequestURI(), r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		proxyReq.Header = r.Header.Clone()
		proxyResp, err := http.DefaultClient.Do(proxyReq)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		defer proxyResp.Body.Close()
		for name, values := range proxyResp.Header {
			for _, value := range values {
				w.Header().Add(name, value)
			}
		}
		w.WriteHeader(proxyResp.StatusCode)
		_, _ = io.Copy(w, proxyResp.Body)
	}))
	t.Cleanup(slow.Close)
	p := testPeer(workerA, 1, signing.Public().(ed25519.PublicKey))
	p.Target = strings.TrimPrefix(slow.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go tunnelConnector(ctx, t, target, p)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })

	url := f.data.URL + "/sources/" + sourceID + "/read"
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	var outerFailures atomic.Int64
	t.Cleanup(func() {
		if outerFailures.Load() > 0 {
			t.Errorf("outer tunnel torn down during soak: %d events", outerFailures.Load())
		}
	})
	h2 := &http2.Transport{
		StrictMaxConcurrentStreams: false,
		AllowHTTP:                  true,
		DialTLSContext: func(ctx context.Context, network, address string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, address)
		},
	}
	defer h2.CloseIdleConnections()
	loadClient := &http.Client{Transport: h2}
	release := make(chan struct{})
	var ready sync.WaitGroup
	ready.Add(concurrency)

	for round := 1; round <= rounds; round++ {
		started := time.Now()
		var wg sync.WaitGroup
		codes := make(chan int, concurrency)
		results := make(chan error, concurrency)
		for range concurrency {
			wg.Add(1)
			go func() {
				defer wg.Done()
				ready.Done()
				<-release
				req, err := http.NewRequest(http.MethodGet, url, nil)
				if err != nil {
					codes <- 0
					results <- err
					return
				}
				for name, values := range headers {
					req.Header[name] = values
				}
				resp, err := loadClient.Do(req)
				if err != nil {
					codes <- 0
					results <- err
					return
				}
				body, _ := io.ReadAll(io.LimitReader(resp.Body, 128))
				resp.Body.Close()
				codes <- resp.StatusCode
				if resp.StatusCode != http.StatusOK || string(body) != "soak" {
					results <- fmt.Errorf("status=%d body=%s", resp.StatusCode, body)
				}
			}()
		}
		ready.Wait()
		close(release)
		wg.Wait()
		close(codes)
		close(results)
		counts := map[int]int{}
		for code := range codes {
			counts[code]++
		}
		var firstErrors []string
		for err := range results {
			if len(firstErrors) < 5 {
				firstErrors = append(firstErrors, err.Error())
			}
		}
		ok := counts[http.StatusOK]
		failureText := strings.Join(firstErrors, " | ")
		t.Logf("round=%d n=%d wall=%s ok=%d failures=%d%v",
			round, concurrency, time.Since(started).Round(time.Millisecond), ok, concurrency-ok, failureText)
		if ok != concurrency {
			t.Errorf("round %d: %d/%d succeeded", round, ok, concurrency)
		}
		if counts[http.StatusBadGateway] > 0 {
			t.Logf("round %d: saw 502 responses, the public failure signature", round)
		}
		time.Sleep(2 * time.Second)
		release = make(chan struct{})
		ready.Add(concurrency)
	}
}
