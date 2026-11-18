package connector

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"testing"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

// The gateway URL is HTTPS with no port in practice, but gRPC needs host:port.
// Passing the raw URL leaves the scheme in the address and every dial times
// out, so this contract matters.
func TestDialTargetStripsSchemeAndDefaultsPort(t *testing.T) {
	for _, testCase := range []struct {
		gateway string
		want    string
	}{
		{"https://litellm.example.invalid", "litellm.example.invalid:443"},
		{"https://litellm.example.invalid/", "litellm.example.invalid:443"},
		{"https://litellm.example.invalid:8443", "litellm.example.invalid:8443"},
		{"https://litellm.example.invalid/base/path", "litellm.example.invalid:443"},
	} {
		got, err := DialTarget(testCase.gateway)
		if err != nil {
			t.Fatalf("%s: %v", testCase.gateway, err)
		}
		if got != testCase.want {
			t.Fatalf("%s: got %q want %q", testCase.gateway, got, testCase.want)
		}
	}
	if _, err := DialTarget("not a url"); err == nil {
		t.Fatal("expected an error for an unparsable gateway URL")
	}
}

type originRoundTripFunc func(*http.Request) (*http.Response, error)

func (f originRoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

// An OpenAPI source must be probed at /openapi.json and its body carried back as
// the catalog the broker fingerprints.
func TestProbeFetchesOpenAPICatalog(t *testing.T) {
	document := []byte(`{"openapi":"3.1.0"}`)
	var seenPath string
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		seenPath = req.URL.Path
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{},
			Body:       io.NopCloser(bytes.NewReader(document)),
		}, nil
	}), "127.0.0.1", "openapi", "")
	resp, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"})
	if err != nil {
		t.Fatal(err)
	}
	if seenPath != "/openapi.json" {
		t.Fatalf("probed %q, want /openapi.json", seenPath)
	}
	if !bytes.Equal(resp.CatalogJson, document) {
		t.Fatalf("catalog = %q, want the origin document", resp.CatalogJson)
	}
}

// An MCP source must never fetch /openapi.json: it serves JSON-RPC at its path,
// so probing the OpenAPI document 404s and the source can never go healthy.
func TestProbeMCPHitsMCPPathAndReturnsNoCatalog(t *testing.T) {
	var seenPath string
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		seenPath = req.URL.Path
		return &http.Response{
			StatusCode: http.StatusMethodNotAllowed,
			Header:     http.Header{},
			Body:       io.NopCloser(bytes.NewReader(nil)),
		}, nil
	}), "127.0.0.1", "mcp", "")
	resp, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"})
	if err != nil {
		t.Fatal(err)
	}
	if seenPath == "/openapi.json" {
		t.Fatal("MCP source was probed at /openapi.json")
	}
	if seenPath != "/mcp" {
		t.Fatalf("probed %q, want /mcp", seenPath)
	}
	if len(resp.CatalogJson) != 0 {
		t.Fatalf("MCP probe returned a catalog (%q), want none", resp.CatalogJson)
	}
}

// An MCP origin that answers 5xx is down, so the probe must fail rather than
// mark the source healthy.
func TestProbeMCPFailsOnServerError(t *testing.T) {
	server := NewServer(originRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusBadGateway,
			Header:     http.Header{},
			Body:       io.NopCloser(bytes.NewReader(nil)),
		}, nil
	}), "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("5xx origin was reported healthy")
	}
}

// A cancelled caller must reach the origin transport, otherwise a cancelled
// gateway request would keep occupying a connector.
func TestExecuteCancellationReachesOriginTransport(t *testing.T) {
	var calls int
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		calls++
		return nil, req.Context().Err()
	}), "127.0.0.1", "openapi", "")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := server.Execute(ctx, &dispatchpb.DispatchRequest{Method: http.MethodGet, Path: "/read"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("origin cancellation not propagated: %v", err)
	}
	if calls != 1 {
		t.Fatalf("origin was called %d times, want 1", calls)
	}
}

// Admit must report the capabilities this build implements, because the broker
// gates pushed RPCs on that set.
func TestAdmitReportsImplementedCapabilities(t *testing.T) {
	server := NewServer(http.DefaultTransport, "127.0.0.1", "openapi", "")
	resp, err := server.Admit(context.Background(), &dispatchpb.AdmitRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.Capabilities) != len(Capabilities) {
		t.Fatalf("advertised %d capabilities, want %d", len(resp.Capabilities), len(Capabilities))
	}
	advertised := map[string]bool{}
	for _, capability := range resp.Capabilities {
		advertised[capability] = true
	}
	for _, required := range []string{"execute", "probe"} {
		if !advertised[required] {
			t.Fatalf("missing capability %q", required)
		}
	}
}

// An oversize request body must be refused before it reaches the origin.
func TestExecuteRejectsOversizeBody(t *testing.T) {
	called := false
	server := NewServer(originRoundTripFunc(func(*http.Request) (*http.Response, error) {
		called = true
		return nil, errors.New("should not be called")
	}), "127.0.0.1", "openapi", "")
	oversize := make([]byte, 4<<20)
	if _, err := server.Execute(context.Background(), &dispatchpb.DispatchRequest{Method: http.MethodPost, Path: "/read", Body: oversize}); err == nil {
		t.Fatal("oversize body was accepted")
	}
	if called {
		t.Fatal("origin was reached with an oversize body")
	}
}

// The origin must see the target's host:port in its Host header. A stateful MCP
// server validates Host against host:port allowlist patterns, so a bare host
// without the port is rejected as a misdirected request.
func TestExecuteSendsHostPortAsOriginHostHeader(t *testing.T) {
	var seenHost string
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		seenHost = req.Host
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(nil))}, nil
	}), "127.0.0.1:18203", "mcp", "")
	if _, err := server.Execute(context.Background(), &dispatchpb.DispatchRequest{Method: http.MethodPost, Path: "/mcp"}); err != nil {
		t.Fatal(err)
	}
	if seenHost != "127.0.0.1:18203" {
		t.Fatalf("origin saw Host %q, want 127.0.0.1:18203", seenHost)
	}
}
