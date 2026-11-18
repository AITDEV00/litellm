package reverseh2

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/config"
	"oicm-litellm-layer/mcp-source-link/internal/wire"
)

func mustMarshal(t *testing.T, value any) []byte {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}

func testSession(deadline time.Duration) *Session {
	return NewSession(config.Peer{WorkerID: "w1", SourceID: "s1", Generation: 1}, deadline)
}

// The reverse listener must keep a stream window small enough to bound memory
// and a stream limit high enough for the measured concurrency.
func TestDispatchBoundsAreConservative(t *testing.T) {
	protocols := HTTP2Config()
	if protocols.MaxReceiveBufferPerStream != 64<<10 {
		t.Fatalf("stream window drifted: %d", protocols.MaxReceiveBufferPerStream)
	}
	if protocols.MaxConcurrentStreams < 1<<17 {
		t.Fatalf("stream limit too low: %d", protocols.MaxConcurrentStreams)
	}
}

// A maximum-size origin payload must still fit inside one envelope, which is
// what keeps the wire limit honest rather than aspirational.
func TestEnvelopeForMaxPayloadFitsWireLimit(t *testing.T) {
	payload := bytes.Repeat([]byte{0x78}, wire.MaxOriginBody)
	envelope := wire.DispatchResponse{
		ID:      strings.Repeat("i", 64),
		Status:  200,
		Headers: map[string][]string{"Content-Type": {"application/json"}},
		Body:    payload,
	}
	encoded := mustMarshal(t, envelope)
	if len(encoded) > wire.MaxBody {
		t.Fatalf("envelope for a maximum payload exceeds the wire limit: %d > %d", len(encoded), wire.MaxBody)
	}
}

func TestResponseStripsFramingHeaders(t *testing.T) {
	headers := map[string][]string{
		"Content-Length":    {"9999"},
		"Transfer-Encoding": {"chunked"},
		"Connection":        {"keep-alive"},
		"Content-Type":      {"application/json"},
	}
	resp, err := wire.DispatchResponse{ID: "1", Status: 200, Headers: headers, Body: []byte(`{}`)}.HTTPResponse()
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	for _, name := range []string{"Content-Length", "Transfer-Encoding", "Connection"} {
		if resp.Header.Get(name) != "" {
			t.Fatalf("framing header %s survived the reverse response", name)
		}
	}
	if resp.ContentLength != int64(len(`{}`)) || resp.Header.Get("Content-Type") != "application/json" {
		t.Fatal("reverse response lost its real payload description")
	}
}

func TestResponseErrorBecomesTransportError(t *testing.T) {
	resp, err := wire.DispatchResponse{ID: "1", Error: "origin exploded"}.HTTPResponse()
	if err == nil || resp != nil {
		t.Fatalf("error envelope produced a response: %v %v", resp, err)
	}
	if !strings.Contains(err.Error(), "origin exploded") {
		t.Fatalf("error reason lost: %v", err)
	}
}

func TestOversizeRequestBodyIsRejected(t *testing.T) {
	session := testSession(time.Second)
	oversize := make([]byte, wire.MaxOriginBody+1)
	req, err := http.NewRequest(http.MethodPost, "http://relay.invalid/sources/x/y", bytes.NewReader(oversize))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := session.RoundTrip(req); err == nil || !strings.Contains(err.Error(), "body limit") {
		t.Fatalf("oversize body was not rejected: %v", err)
	}
}

func TestUnansweredDispatchTimesOut(t *testing.T) {
	session := testSession(50 * time.Millisecond)
	req, err := http.NewRequest(http.MethodGet, "http://relay.invalid/sources/x/y", nil)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if _, err := session.RoundTrip(req); err == nil || !strings.Contains(err.Error(), "deadline") {
		t.Fatalf("unanswered dispatch did not time out: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("deadline was not honored, took %s", elapsed)
	}
}

// A caller that cancels before admission must not be dispatched at all.
func TestCallerCancellationBeforeAdmission(t *testing.T) {
	session := testSession(time.Minute)
	session.pending <- wire.DispatchRequest{ID: "block-admission"}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://relay.invalid/sources/x/y", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := session.RoundTrip(req); !errors.Is(err, context.Canceled) {
		t.Fatalf("a caller that had already cancelled was still dispatched: %v", err)
	}
}

// Cancelling while waiting for the response must stop the wait and leave no
// waiter behind, or a cancelled request would leak into the next correlation.
func TestCallerCancellationAfterAdmission(t *testing.T) {
	session := testSession(time.Minute)
	ctx, cancel := context.WithCancel(context.Background())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://relay.invalid/sources/x/y", nil)
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	if _, err := session.RoundTrip(req); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled call did not stop waiting: %v", err)
	}
	session.mu.Lock()
	pending := len(session.inflight)
	session.mu.Unlock()
	if pending != 0 {
		t.Fatalf("cancelled call left a pending waiter: %d", pending)
	}
}

// Closing must fail every waiter immediately, not let them hit the deadline.
func TestCloseFailsWaitersImmediately(t *testing.T) {
	session := testSession(time.Minute)
	done := make(chan error, 1)
	go func() {
		req, _ := http.NewRequest(http.MethodGet, "http://relay.invalid/sources/x/y", nil)
		_, err := session.RoundTrip(req)
		done <- err
	}()
	// Let the goroutine register its waiter before closing.
	for {
		session.mu.Lock()
		pending := len(session.inflight)
		session.mu.Unlock()
		if pending > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	session.Close()
	select {
	case err := <-done:
		if !errors.Is(err, ErrSessionClosed) {
			t.Fatalf("waiter got %v, want ErrSessionClosed", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("closing the session did not release the waiter")
	}
}

func TestDeliverRejectsUnknownAndConsumesKnown(t *testing.T) {
	session := testSession(time.Minute)
	if session.Deliver(wire.DispatchResponse{ID: "never-issued"}) {
		t.Fatal("delivered a response nobody was waiting for")
	}
	waiter := make(chan wire.DispatchResponse, 1)
	session.mu.Lock()
	session.inflight["issued"] = waiter
	session.mu.Unlock()
	if !session.Deliver(wire.DispatchResponse{ID: "issued", Status: 200}) {
		t.Fatal("known response was not delivered")
	}
	select {
	case got := <-waiter:
		if got.Status != 200 {
			t.Fatalf("wrong response delivered: %d", got.Status)
		}
	case <-time.After(time.Second):
		t.Fatal("delivered response never reached the waiter")
	}
	// A second delivery for the same ID must be rejected, so a replay cannot
	// double-complete one request.
	if session.Deliver(wire.DispatchResponse{ID: "issued"}) {
		t.Fatal("delivered the same response twice")
	}
}

// Ready must require a recent probe for the same source, which is what stops
// a never-probed or stale session from absorbing traffic.
func TestReadyRequiresRecentProbeForSameSource(t *testing.T) {
	session := testSession(time.Minute)
	if session.Ready("s1") {
		t.Fatal("a session that never probed is ready")
	}
	session.MarkProbed()
	if !session.Ready("s1") {
		t.Fatal("a freshly probed session is not ready")
	}
	if session.Ready("other-source") {
		t.Fatal("ready for a source the session does not serve")
	}
	session.mu.Lock()
	session.lastProbe = time.Now().Add(-3 * ProbeInterval)
	session.mu.Unlock()
	if session.Ready("s1") {
		t.Fatal("a stale probe still counted as ready")
	}
}
