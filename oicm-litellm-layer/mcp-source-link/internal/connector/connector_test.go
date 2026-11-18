package connector

import (
	"context"
	"errors"
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

// A cancelled caller must reach the origin transport, otherwise a cancelled
// gateway request would keep occupying a connector.
func TestExecuteCancellationReachesOriginTransport(t *testing.T) {
	var calls int
	server := NewServer(originRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		calls++
		return nil, req.Context().Err()
	}))
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
	server := NewServer(http.DefaultTransport)
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
	}))
	oversize := make([]byte, 4<<20)
	if _, err := server.Execute(context.Background(), &dispatchpb.DispatchRequest{Method: http.MethodPost, Path: "/read", Body: oversize}); err == nil {
		t.Fatal("oversize body was accepted")
	}
	if called {
		t.Fatal("origin was reached with an oversize body")
	}
}
