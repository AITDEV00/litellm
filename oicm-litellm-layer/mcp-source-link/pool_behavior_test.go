package main

import (
	"net/http"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/control"
)

// The digest comparison every plane authenticates through must reject near
// misses, and must not be fooled by a prefix or an empty value.
func TestSyntheticCredentials(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1/mcp-source-link-reverse", nil)
	req.Header.Set("Authorization", "Bearer "+notSecret)
	if !control.SameSecret(secretDigest(notSecret), control.Bearer(req)) {
		t.Fatalf("digest contract changed: supplied-length=%d", len(control.Bearer(req)))
	}
	if control.SameSecret(secretDigest(notSecret), "wrong"+notSecret) {
		t.Fatal("digest comparison accepted a different credential")
	}
	if control.SameSecret(secretDigest(notSecret), "") {
		t.Fatal("digest comparison accepted an empty credential")
	}
}

// Two members on the same source must share traffic and never leak gateway
// credentials to the origin. A third member whose proof key is wrong must be
// excluded entirely.
func TestReversePeerReadSharingAndRevoke(t *testing.T) {
	pub, signing := generateKey(t)
	originA, digest := signedOrigin(t, "A", signing)
	originB, _ := signedOrigin(t, "B", signing)
	_, wrongSigning := generateKey(t)
	wrongOrigin, _ := signedOrigin(t, "C", wrongSigning)
	f, _ := reverseFixture(t)

	a := testPeer(workerA, 1, pub)
	a.Target = strings.TrimPrefix(originA.URL, "http://")
	a.ExpectedCatalog = digest
	b := testPeer(workerB, 1, pub)
	b.Target = strings.TrimPrefix(originB.URL, "http://")
	b.ExpectedCatalog = digest
	c := testPeer("44444444-4444-4444-4444-444444444444", 1, pub)
	c.Target = strings.TrimPrefix(wrongOrigin.URL, "http://")
	c.ExpectedCatalog = digest
	for _, p := range []config.Peer{a, b, c} {
		f.setPeer(p, "")
		startSession(t, f, p)
	}
	eventually(t, func() bool { return readyReverse(t, f, a.WorkerID) && readyReverse(t, f, b.WorkerID) })

	got := map[string]int{}
	requestHeaders := http.Header{"Authorization": {"Bearer " + serviceKey}, "Cookie": {"private=not-forwarded"}}
	for range 12 {
		status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, requestHeaders)
		if status != http.StatusOK {
			t.Fatalf("read status %d %s", status, body)
		}
		got[body]++
	}
	if got["A"] != 6 || got["B"] != 6 || got["C"] != 0 {
		t.Fatalf("unexpected distribution: %v", got)
	}

	// An unreviewed write to a multi-member source must be refused rather than
	// silently duplicated across members.
	if status, body := read(t, f.dataURL+"/sources/"+sourceID+"/write", http.MethodPost, requestHeaders); status != http.StatusConflict {
		t.Fatalf("unreviewed write got %d %s", status, body)
	}
	requestHeaders.Set("Mcp-Session-Id", "any")
	if status, _ := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, requestHeaders); status != http.StatusBadRequest {
		t.Fatalf("stateful read status %d", status)
	}
	requestHeaders.Del("Mcp-Session-Id")

	request, _ := http.NewRequest(http.MethodPost, f.dataURL+"/v1/source-link/revoke", nil)
	request.Header.Set("Authorization", "Bearer "+serviceKey)
	request.Header.Set("X-Source-Member", workerB)
	request.Header.Set("X-Source-Id", sourceID)
	f.revokePeer(workerB)
	response, err := fixtureHTTP2Client(t).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	eventually(t, func() bool { return f.runtime.Existing(workerB) == nil })
	for range 4 {
		status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, http.Header{"Authorization": {"Bearer " + serviceKey}})
		if status != http.StatusOK || body != "A" {
			t.Fatalf("single member after revoke got %d %s", status, body)
		}
	}
}

// Draining must stop new dispatches while keeping the session itself alive, so
// an operator can drain without killing in-flight work.
func TestDrainExcludesDispatchButKeepsSession(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, _ := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")
	startSession(t, f, p)
	eventually(t, func() bool { return readyReverse(t, f, workerA) })

	headers := http.Header{"Authorization": {"Bearer " + serviceKey}}
	if status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers); status != http.StatusOK || body != "A" {
		t.Fatalf("pre-drain read got %d %s", status, body)
	}

	f.setPhase(workerA, "draining")
	f.runtime.Reconcile()
	eventually(t, func() bool {
		status, _ := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
		return status == http.StatusServiceUnavailable
	})
	if f.runtime.Existing(workerA) == nil {
		t.Fatal("drain closed the live session")
	}

	f.setPhase(workerA, "active")
	f.runtime.Reconcile()
	eventually(t, func() bool {
		status, body := read(t, f.dataURL+"/sources/"+sourceID+"/read", http.MethodGet, headers)
		return status == http.StatusOK && body == "A"
	})
}

// A revoked connector must not be able to re-register with its old
// credential or its old generation.
func TestRevokedConnectorCannotReconnect(t *testing.T) {
	pub, signing := generateKey(t)
	origin, digest := signedOrigin(t, "A", signing)
	f, server := reverseFixture(t)
	p := testPeer(workerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.setPeer(p, "")
	startSession(t, f, p)
	eventually(t, func() bool { return readyReverse(t, f, workerA) })

	f.revokePeer(workerA)
	f.runtime.Reconcile()
	eventually(t, func() bool { return f.runtime.Existing(workerA) == nil })

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
