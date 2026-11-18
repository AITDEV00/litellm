package control

import (
	"net/http"
	"strings"
	"testing"

	"oicm-litellm-layer/mcp-source-link/internal/config"
)

func TestDigestValueIsStableHexSHA256(t *testing.T) {
	got := DigestValue("abc")
	const want = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	if got != want {
		t.Fatalf("digest drifted from SHA-256 hex: %q", got)
	}
	if DigestValue("abc") != got {
		t.Fatal("digest is not deterministic")
	}
}

func TestSameSecretRejectsNearMissesAndEmpty(t *testing.T) {
	expected := DigestValue("the-real-secret")
	if !SameSecret(expected, "the-real-secret") {
		t.Fatal("correct secret rejected")
	}
	for _, supplied := range []string{"", "the-real-secre", "the-real-secrets", "THE-REAL-SECRET", "x"} {
		if SameSecret(expected, supplied) {
			t.Fatalf("accepted a wrong credential %q", supplied)
		}
	}
}

func TestBearerExtractsOnlyBearerScheme(t *testing.T) {
	for _, testCase := range []struct {
		header string
		want   string
	}{
		{"Bearer abc", "abc"},
		{"Bearer ", ""},
		{"", ""},
		{"Basic abc", ""},
		{"bearer abc", ""},
	} {
		req, _ := http.NewRequest(http.MethodGet, "http://example.invalid/", nil)
		if testCase.header != "" {
			req.Header.Set("Authorization", testCase.header)
		}
		if got := Bearer(req); got != testCase.want {
			t.Fatalf("Bearer(%q) = %q, want %q", testCase.header, got, testCase.want)
		}
	}
}

// newForTest builds a client over caller-supplied peer state so a test can
// stand in for the control plane without an HTTP server.
func newForTest(t *testing.T, peers ...config.Peer) *Client {
	t.Helper()
	state := make(map[string]config.Peer, len(peers))
	for _, worker := range peers {
		state[worker.WorkerID] = worker
	}
	return &Client{tokenDigest: DigestValue(""), workers: state, client: http.DefaultClient}
}

func TestNewForTestIndexesPeersByWorkerID(t *testing.T) {
	client := newForTest(t,
		config.Peer{WorkerID: "w1", SourceID: "s1"},
		config.Peer{WorkerID: "w2", SourceID: "s2"},
	)
	if len(client.Peers()) != 2 {
		t.Fatalf("expected 2 peers, got %d", len(client.Peers()))
	}
	if _, ok := client.Peer("w1"); !ok {
		t.Fatal("w1 not indexed")
	}
	if _, ok := client.Peer("absent"); ok {
		t.Fatal("absent peer reported present")
	}
}

// The digest comparison every plane authenticates through must reject near
// misses, and must not be fooled by a prefix or an empty value.
func TestSameSecretRejectsPrefixAndEmpty(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "http://127.0.0.1/control", nil)
	req.Header.Set("Authorization", "Bearer "+strings.Repeat("0", 64))
	if !SameSecret(DigestValue(strings.Repeat("0", 64)), Bearer(req)) {
		t.Fatalf("digest contract changed: supplied-length=%d", len(Bearer(req)))
	}
	expected := DigestValue(strings.Repeat("0", 64))
	if SameSecret(expected, "wrong"+strings.Repeat("0", 64)) {
		t.Fatal("digest comparison accepted a different credential")
	}
	if SameSecret(expected, "") {
		t.Fatal("digest comparison accepted an empty credential")
	}
}
