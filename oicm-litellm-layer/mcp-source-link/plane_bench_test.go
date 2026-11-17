package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestSpikePlaneComparison is the step-9 head-to-head benchmark: the same
// fixture, the same origin, both transports, identical offered load. It is
// skipped unless MCP_TUNNEL_BENCH=1 so normal CI runs stay fast.
func TestSpikePlaneComparison(t *testing.T) {
	if os.Getenv("MCP_TUNNEL_BENCH") != "1" {
		t.Skip("set MCP_TUNNEL_BENCH=1 to run the plane comparison")
	}
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "bench", signing)
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}

	for _, plane := range []struct {
		name   string
		setUp  func(t *testing.T) (*fixture, string)
		client func(ctx context.Context, t *testing.T, target string, p peer)
	}{
		{"reverse-h2", reverseBenchFixture, reverseBenchClient},
		{"tunnel", tunnelBenchFixture, tunnelConnector},
	} {
		for _, rps := range []int{50, 100, 250, 500, 1000} {
			t.Run(fmt.Sprintf("%s-%drps", plane.name, rps), func(t *testing.T) {
				f, target := plane.setUp(t)
				p := testPeer(workerA, 1, pub)
				p.Target = strings.TrimPrefix(origin.URL, "http://")
				p.ExpectedCatalog = catalog
				f.setPeer(p, "")
				ctx, cancel := context.WithCancel(context.Background())
				t.Cleanup(cancel)
				go plane.client(ctx, t, target, p)
				eventually(t, func() bool { return readyReverse(t, f, workerA) })

				url := f.data.URL + "/sources/" + sourceID + "/read"
				var wg sync.WaitGroup
				ticker := time.NewTicker(time.Second / time.Duration(rps))
				defer ticker.Stop()
				deadline := time.Now().Add(5 * time.Second)
				var mu sync.Mutex
				latencies := make([]time.Duration, 0, 5*rps)
				var failures atomic.Int64
				for i := range rps {
					wg.Add(1)
					go func(seq int) {
						defer wg.Done()
						wait := time.Duration(seq) * time.Second / time.Duration(rps)
						time.Sleep(wait)
						for time.Now().Before(deadline) {
							<-ticker.C
							start := time.Now()
							status, _ := readBench(t, url, headers)
							elapsed := time.Since(start)
							mu.Lock()
							if status != http.StatusOK {
								failures.Add(1)
							}
							latencies = append(latencies, elapsed)
							mu.Unlock()
						}
					}(i)
					if i == 0 {
						ticker.Reset(time.Second / time.Duration(rps))
					}
				}
				wg.Wait()

				mu.Lock()
				sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
				sampled := len(latencies)
				p50 := percentile(latencies, 50)
				p95 := percentile(latencies, 95)
				mu.Unlock()
				t.Logf("plane=%s offered=%drps completed=%d failures=%d p50=%s p95=%s",
					plane.name, rps, sampled, failures.Load(), p50, p95)
			})
		}
	}
}

func percentile(sorted []time.Duration, pct int) time.Duration {
	if len(sorted) == 0 {
		return 0
	}
	index := len(sorted) * pct / 100
	if index >= len(sorted) {
		index = len(sorted) - 1
	}
	return sorted[index]
}

func readBench(t *testing.T, url string, headers http.Header) (int, string) {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Error(err)
		return 0, ""
	}
	for name, values := range headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, ""
	}
	defer resp.Body.Close()
	return resp.StatusCode, ""
}

// reverseBenchClient adapts the reverse-h2 test connector to the benchmark's
// string-target interface. The target is the fixture server's URL, and the
// adapter reconstructs the client through the fixture helper so it talks to
// the same server the measurement drives.
func reverseBenchClient(ctx context.Context, t *testing.T, target string, p peer) {
	t.Helper()
	client := &http.Client{Transport: &http.Transport{ForceAttemptHTTP2: true}}
	reverseConnectorClient(ctx, t, client, target, p)
}

// reverseBenchFixture mirrors reverseFixture but returns the connector
// target, sharing the fixture helper shape with the tunnel plane.
func reverseBenchFixture(t *testing.T) (*fixture, string) {
	t.Helper()
	f, server := reverseFixture(t)
	return f, server.URL
}

// tunnelBenchFixture adapts tunnelFixture's three-value return for the
// benchmark's uniform fixture interface.
func tunnelBenchFixture(t *testing.T) (*fixture, string) {
	t.Helper()
	f, _, target := tunnelFixture(t)
	return f, target
}
