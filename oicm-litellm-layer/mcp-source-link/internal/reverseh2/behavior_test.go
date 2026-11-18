package reverseh2_test

import (
	"io"
	"net/http"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/harness"
)

func TestRegistrationRejectsBadCredentials(t *testing.T) {
	pub, signing := harness.Key(t)
	origin, digest := harness.SignedOrigin(t, "A", signing)
	f := harness.New(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Target = strings.TrimPrefix(origin.URL, "http://")
	p.ExpectedCatalog = digest
	f.SetPeer(p, "")
	server := f.ReverseServer(t)

	probe := func(member, generation, credential string) *http.Response {
		resp, err := harness.ReverseCall(t, server.Client(), server.URL+"/mcp-source-link-reverse", http.Header{
			"X-Source-Member":     {member},
			"X-Source-Generation": {generation},
			"Authorization":       {"Bearer " + credential},
		}, nil)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}

	if resp := probe(harness.WorkerA, "1", "wrong"); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad credential accepted: %d", resp.StatusCode)
	}
	if resp := probe(harness.WorkerA, "9", harness.NotSecret); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("stale generation accepted: %d", resp.StatusCode)
	}
	if resp := probe("missing", "1", harness.NotSecret); resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown member accepted: %d", resp.StatusCode)
	}

	resp := probe(harness.WorkerA, "1", harness.NotSecret)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("valid registration rejected: %d", resp.StatusCode)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	_ = resp.Body.Close()
	if !strings.Contains(string(body), harness.WorkerA) {
		t.Fatalf("registration body missing worker id: %s", body)
	}

	// A duplicate must be 409, not 401, so a live session is never misreported
	// as a credential failure.
	resp = probe(harness.WorkerA, "1", harness.NotSecret)
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("duplicate session reported %d, want 409", resp.StatusCode)
	}
	_ = resp.Body.Close()

	// Closing must free the identity for re-registration.
	session := f.Runtime.Existing(harness.WorkerA)
	if session == nil {
		t.Fatal("session not stored")
	}
	session.Close()
	resp = probe(harness.WorkerA, "1", harness.NotSecret)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("re-registration after close rejected: %d", resp.StatusCode)
	}
	_ = resp.Body.Close()
}

// Two healthy members on one source must share traffic, and never leak gateway
// credentials to the origin. A member whose proof key is wrong must be excluded.
func TestPeerReadSharingAndRevoke(t *testing.T) {
	pub, signing := harness.Key(t)
	originA, digest := harness.SignedOrigin(t, "A", signing)
	originB, _ := harness.SignedOrigin(t, "B", signing)
	_, wrongSigning := harness.Key(t)
	wrongOrigin, _ := harness.SignedOrigin(t, "C", wrongSigning)
	f := harness.New(t)

	a := harness.Peer(harness.WorkerA, 1, pub)
	a.Target = strings.TrimPrefix(originA.URL, "http://")
	a.ExpectedCatalog = digest
	b := harness.Peer(harness.WorkerB, 1, pub)
	b.Target = strings.TrimPrefix(originB.URL, "http://")
	b.ExpectedCatalog = digest
	c := harness.Peer("44444444-4444-4444-4444-444444444444", 1, pub)
	c.Target = strings.TrimPrefix(wrongOrigin.URL, "http://")
	c.ExpectedCatalog = digest

	for _, p := range []config.Peer{a, b, c} {
		f.SetPeer(p, "")
		f.StartSession(t, p)
	}
	harness.Eventually(t, func() bool { return f.ReadyReverse(t, a.WorkerID) && f.ReadyReverse(t, b.WorkerID) })

	got := map[string]int{}
	requestHeaders := http.Header{"Authorization": {"Bearer " + harness.ServiceKey}, "Cookie": {"private=not-forwarded"}}
	for range 12 {
		status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, requestHeaders)
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
	if status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/write", http.MethodPost, requestHeaders); status != http.StatusConflict {
		t.Fatalf("unreviewed write got %d %s", status, body)
	}

	requestHeaders.Set("Mcp-Session-Id", "any")
	if status, _ := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet, requestHeaders); status != http.StatusBadRequest {
		t.Fatalf("stateful read status %d", status)
	}
	requestHeaders.Del("Mcp-Session-Id")

	request, _ := http.NewRequest(http.MethodPost, f.DataURL+"/v1/source-link/revoke", nil)
	request.Header.Set("Authorization", "Bearer "+harness.ServiceKey)
	request.Header.Set("X-Source-Member", b.WorkerID)
	request.Header.Set("X-Source-Id", harness.SourceID)
	f.RevokePeer(b.WorkerID)
	response, err := harness.HTTP2Client(t).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	harness.Eventually(t, func() bool { return f.Runtime.Existing(b.WorkerID) == nil })

	for range 4 {
		status, body := harness.Read(t, f.DataURL+"/sources/"+harness.SourceID+"/read", http.MethodGet,
			http.Header{"Authorization": {"Bearer " + harness.ServiceKey}})
		if status != http.StatusOK || body != "A" {
			t.Fatalf("single member after revoke got %d %s", status, body)
		}
	}
}
