package tunnel_test

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"oicm-litellm-layer/mcp-source-link/internal/harness"
	"oicm-litellm-layer/mcp-source-link/internal/tunnel"
)

// streamingOrigin returns a transport whose single response is an SSE body that
// writes one event, waits for release, then writes a second and closes. A
// streaming read surfaces the first event while the origin is still open; a
// buffered read returns nothing until release fires.
type streamingOrigin struct {
	first   chan struct{}
	release chan struct{}
}

func (o *streamingOrigin) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Path != "/events" {
		// The probe path answers a valid MCP initialize so the source goes ready;
		// anything that is not the streaming endpoint is treated as that handshake.
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"jsonrpc":"2.0","id":"source-link-probe","result":{}}`)),
		}, nil
	}
	reader, writer := io.Pipe()
	go func() {
		_, _ = writer.Write([]byte("data: first\n\n"))
		o.first <- struct{}{}
		select {
		case <-o.release:
		case <-req.Context().Done():
		}
		_, _ = writer.Write([]byte("data: second\n\n"))
		_ = writer.Close()
	}()
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"text/event-stream"}},
		Body:       reader,
	}, nil
}

// A streamed origin reaches the relay client incrementally: the first event is
// readable before the origin writes its second, proving the response is not
// buffered end to end.
func TestStreamDeliversFirstChunkBeforeOriginCloses(t *testing.T) {
	pub, _ := harness.Key(t)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Kind = "mcp"
	f.SetPeer(p, "")

	origin := &streamingOrigin{first: make(chan struct{}, 16), release: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnectorWithTransport(ctx, t, target, p, origin)
	harness.Eventually(t, func() bool {
		session := f.Runtime.Existing(harness.WorkerA)
		return session != nil && session.Ready(harness.SourceID)
	})

	resp, err := harness.HTTP2Client(t).Do(authedGet(t, f.DataURL+"/sources/"+harness.SourceID+"/events"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()

	firstLine := readLineOrTimeout(t, resp.Body, 5*time.Second)
	if !strings.HasPrefix(firstLine, "data: first") {
		t.Fatalf("first chunk = %q, want data: first (response was buffered until origin closed)", firstLine)
	}

	close(origin.release)
	rest, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(rest), "data: second") {
		t.Fatalf("tail = %q, want data: second", rest)
	}
}

// authedGet builds a GET carrying the relay service secret, so Forward
// authorizes the request.
func authedGet(t *testing.T, url string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+harness.ServiceKey)
	return req
}

// readLineOrTimeout reads one line, failing the test if it does not arrive in
// the window, so a buffered response cannot hang the run.
func readLineOrTimeout(t *testing.T, body io.Reader, window time.Duration) string {
	t.Helper()
	result := make(chan string, 1)
	go func() {
		buffer := make([]byte, 64)
		count, _ := body.Read(buffer)
		result <- string(buffer[:count])
	}()
	select {
	case line := <-result:
		return line
	case <-time.After(window):
		t.Fatal("no chunk arrived before the window; response was buffered")
		return ""
	}
}

// delayedBodyOrigin answers the MCP probe, then for /slow writes headers at once
// and the body only after gate is closed. The caller can hold the stream open
// arbitrarily long.
type delayedBodyOrigin struct{ gate chan struct{} }

func (o *delayedBodyOrigin) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Path != "/slow" {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"jsonrpc":"2.0","id":"source-link-probe","result":{}}`)),
		}, nil
	}
	reader, writer := io.Pipe()
	go func() {
		<-o.gate
		_, _ = writer.Write([]byte("late event"))
		_ = writer.Close()
	}()
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"text/event-stream"}},
		Body:       reader,
	}, nil
}

// An SSE stream must not be cut merely for staying open past the response
// deadline. The deadline bounds time-to-headers only; once headers arrive the
// body flows as long as the caller keeps it open.
func TestStreamSurvivesPastResponseDeadline(t *testing.T) {
	previous := tunnel.ResponseDeadline
	tunnel.ResponseDeadline = 200 * time.Millisecond
	t.Cleanup(func() { tunnel.ResponseDeadline = previous })

	pub, _ := harness.Key(t)
	f := harness.New(t)
	target := f.TunnelServer(t)
	p := harness.Peer(harness.WorkerA, 1, pub)
	p.Kind = "mcp"
	f.SetPeer(p, "")

	origin := &delayedBodyOrigin{gate: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go harness.RunTunnelConnectorWithTransport(ctx, t, target, p, origin)
	harness.Eventually(t, func() bool {
		session := f.Runtime.Existing(harness.WorkerA)
		return session != nil && session.Ready(harness.SourceID)
	})

	resp, err := harness.HTTP2Client(t).Do(authedGet(t, f.DataURL+"/sources/"+harness.SourceID+"/slow"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()

	// Hold the stream open well past the 200ms response deadline before releasing.
	time.Sleep(700 * time.Millisecond)
	close(origin.gate)
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("stream was cut after the deadline: %v", err)
	}
	if string(body) != "late event" {
		t.Fatalf("body = %q, want %q", body, "late event")
	}
}
