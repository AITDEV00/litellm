package connector

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"

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

// An MCP source must never fetch /openapi.json: it serves JSON-RPC at its path.
// The probe sends a real initialize to the configured path and accepts the
// JSON-RPC result, returning no catalog for the broker to fingerprint.
func TestProbeMCPInitializesAtMCPPathAndReturnsNoCatalog(t *testing.T) {
	var seenMethod, seenPath string
	var seenBody []byte
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		seenMethod = req.Method
		seenPath = req.URL.Path
		seenBody, _ = io.ReadAll(req.Body)
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(
				`{"jsonrpc":"2.0","id":"source-link-health","result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"mock"}}}`,
			)),
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
	if seenMethod != http.MethodPost {
		t.Fatalf("probe used %s, want a JSON-RPC POST", seenMethod)
	}
	if !strings.Contains(string(seenBody), `"initialize"`) {
		t.Fatalf("probe body %q does not carry an initialize", seenBody)
	}
	if len(resp.CatalogJson) != 0 {
		t.Fatalf("MCP probe returned a catalog (%q), want none", resp.CatalogJson)
	}
}

// An SSE-framed initialize result is the Streamable HTTP transport's other
// documented shape and must also count as healthy.
func TestProbeMCPAcceptsSSEInitializeResult(t *testing.T) {
	server := NewServer(originRoundTripFunc(func(*http.Request) (*http.Response, error) {
		body := "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"source-link-health\",\"result\":{\"protocolVersion\":\"2025-06-18\"}}\n\n"
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"text/event-stream"}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	}), "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
		t.Fatalf("a valid SSE initialize was rejected: %v", err)
	}
}

// A backend that answers, but not with an MCP initialize result, must not go
// healthy. Otherwise a 401/403/redirect/HTML 200 from an unrelated service would
// be dispatchable as if it spoke MCP.
func TestProbeMCPRejectsNonMCPResponses(t *testing.T) {
	for _, testCase := range []struct {
		name        string
		status      int
		contentType string
		body        string
	}{
		{"unauthorized", http.StatusUnauthorized, "application/json", `{"error":"no"}`},
		{"forbidden", http.StatusForbidden, "application/json", `{"error":"no"}`},
		{"redirect", http.StatusMovedPermanently, "text/html", `<a href="http://elsewhere">moved</a>`},
		{"html-200", http.StatusOK, "text/html", `<html><body>hello</body></html>`},
		{"jsonrpc-error", http.StatusOK, "application/json", `{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"bad"}}`},
		{"empty-200", http.StatusOK, "application/json", ``},
		{"server-error", http.StatusBadGateway, "text/plain", `down`},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			server := NewServer(originRoundTripFunc(func(*http.Request) (*http.Response, error) {
				return &http.Response{
					StatusCode: testCase.status,
					Header:     http.Header{"Content-Type": {testCase.contentType}},
					Body:       io.NopCloser(strings.NewReader(testCase.body)),
				}, nil
			}), "127.0.0.1", "mcp", "")
			if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
				t.Fatalf("%s response was accepted as a healthy MCP backend", testCase.name)
			}
		})
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
	for _, required := range []string{"execute", "execute_stream", "probe"} {
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

// fakeStream collects the chunks ExecuteStream sends and signals each one so a
// test can observe incremental delivery.
type fakeStream struct {
	grpc.ServerStream
	sent chan *dispatchpb.DispatchResponseChunk
}

func (s *fakeStream) Send(chunk *dispatchpb.DispatchResponseChunk) error {
	s.sent <- chunk
	return nil
}

func (s *fakeStream) Context() context.Context { return context.Background() }

// ExecuteStream must send status and headers first, then the body in chunks as
// the origin produces them, so a client sees the first bytes before the origin
// closes its body.
func TestExecuteStreamSendsHeadersThenChunksIncrementally(t *testing.T) {
	releaseSecond := make(chan struct{})
	origin := originRoundTripFunc(func(*http.Request) (*http.Response, error) {
		reader, writer := io.Pipe()
		go func() {
			_, _ = writer.Write([]byte("first"))
			<-releaseSecond
			_, _ = writer.Write([]byte("second"))
			_ = writer.Close()
		}()
		return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"X-Test": {"1"}}, Body: reader}, nil
	})
	server := NewServer(origin, "127.0.0.1", "mcp", "")
	stream := &fakeStream{sent: make(chan *dispatchpb.DispatchResponseChunk, 8)}

	done := make(chan error, 1)
	go func() {
		done <- server.ExecuteStream(&dispatchpb.DispatchRequest{Method: http.MethodGet, Path: "/events"}, stream)
	}()

	head := receiveChunk(t, stream.sent)
	if head.Status != http.StatusOK || len(head.Headers) == 0 {
		t.Fatalf("first chunk must carry status and headers, got status=%d headers=%d", head.Status, len(head.Headers))
	}
	first := receiveChunk(t, stream.sent)
	if string(first.Body) != "first" {
		t.Fatalf("first body chunk = %q, want %q before the origin closes", first.Body, "first")
	}

	close(releaseSecond)
	second := receiveChunk(t, stream.sent)
	if string(second.Body) != "second" {
		t.Fatalf("second body chunk = %q, want %q", second.Body, "second")
	}
	if err := <-done; err != nil {
		t.Fatalf("ExecuteStream returned %v, want nil after body close", err)
	}
}

// receiveChunk waits for the next streamed chunk, failing if none arrives.
func receiveChunk(t *testing.T, sent <-chan *dispatchpb.DispatchResponseChunk) *dispatchpb.DispatchResponseChunk {
	t.Helper()
	select {
	case chunk := <-sent:
		return chunk
	case <-time.After(5 * time.Second):
		t.Fatal("no chunk arrived")
		return nil
	}
}
