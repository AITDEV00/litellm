package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestTunnelHighChurnStability reproduces the production failure seen on
// 2026-11-17: at 64 in-flight dispatches against a fast origin, the tunnel
// stream died with RST_STREAM INTERNAL_ERROR. Slow-origin runs held the same
// 64 in-flight depth without one reset, while fast-origin runs churned through
// thousands of short-lived pushed RPCs, so the suspect is stream churn on the
// tunnel, not concurrency depth. This drives that churn in-process: if the
// tunnel resets, dispatches start failing and the test fails.
func TestTunnelHighChurnStability(t *testing.T) {
	_, signing, _ := ed25519.GenerateKey(rand.Reader)
	pub := signing.Public().(ed25519.PublicKey)
	origin, catalog := signedOrigin(t, "A", signing)
	f, _, target := tunnelFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = catalog
	f.setPeer(p, "")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go tunnelConnector(ctx, t, target, p)
	eventually(t, func() bool { return readyTunnel(t, f, workerA) })

	url := f.data.URL + "/sources/" + sourceID + "/read"
	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	deadline := time.Now().Add(5 * time.Second)
	var mu sync.Mutex
	failed := 0
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Now().Before(deadline) {
				if status, _ := read(t, url, http.MethodGet, headers); status != http.StatusOK {
					mu.Lock()
					failed++
					mu.Unlock()
					return
				}
			}
		}()
	}
	wg.Wait()
	if failed > 0 {
		t.Fatalf("dispatch failed under churn: %d of 64 readers hit a non-200 (tunnel reset)", failed)
	}
}
