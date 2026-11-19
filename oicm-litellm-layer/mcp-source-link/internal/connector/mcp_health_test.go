package connector

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"oicm-litellm-layer/mcp-source-link/dispatchpb"
)

// mcpFixture is a protocol-conformant Streamable HTTP MCP server, small enough to
// reason about but strict about the parts that matter: initialize allocates a
// session and returns its id, a follow-up initialize without a session leak would
// allocate a new one, notifications/initialized must arrive before ping, a ping
// on a live session is answered, and DELETE terminates the session. It fails the
// test if a request carries a session id it never issued.
type mcpFixture struct {
	t        *testing.T
	mu       sync.Mutex
	sessions map[string]bool
	nextID   int
	// failPing makes a live ping answer 404 so the session is dropped.
	failPing bool
	// sseHold, when set, keeps the initialize response stream open after the
	// reply event so the connector must parse incrementally rather than wait EOF.
	sseHold <-chan struct{}
	// replyID overrides the initialize reply id to test id correlation.
	replyID string
}

func newMCPFixture(t *testing.T) *mcpFixture {
	t.Helper()
	return &mcpFixture{t: t, sessions: map[string]bool{}}
}

func (f *mcpFixture) RoundTrip(req *http.Request) (*http.Response, error) {
	body := []byte(nil)
	if req.Body != nil {
		read, err := io.ReadAll(req.Body)
		if err != nil {
			return nil, err
		}
		body = read
	}
	var message struct {
		ID     json.RawMessage `json:"id"`
		Method string          `json:"method"`
	}
	if req.Method != http.MethodDelete && len(body) > 0 {
		if err := json.Unmarshal(body, &message); err != nil {
			return nil, err
		}
	}
	session := req.Header.Get("Mcp-Session-Id")
	switch {
	case req.Method == http.MethodDelete:
		f.mu.Lock()
		if !f.sessions[session] {
			f.mu.Unlock()
			f.t.Errorf("DELETE for an unknown session %q", session)
			return response(http.StatusNotFound, "text/plain", "", ""), nil
		}
		delete(f.sessions, session)
		f.mu.Unlock()
		return response(http.StatusNoContent, "", "", ""), nil
	case message.Method == "initialize":
		return f.initialize(req, session, message.ID)
	case message.Method == "notifications/initialized":
		f.mu.Lock()
		known := f.sessions[session]
		f.mu.Unlock()
		if !known {
			f.t.Errorf("initialized notification for an unknown session %q", session)
		}
		return response(http.StatusAccepted, "", "", ""), nil
	case message.Method == "ping":
		f.mu.Lock()
		known := f.sessions[session]
		failPing := f.failPing
		f.mu.Unlock()
		if !known {
			f.t.Errorf("ping for an unknown session %q", session)
			return response(http.StatusNotFound, "text/plain", "", ""), nil
		}
		if failPing {
			return response(http.StatusNotFound, "text/plain", "", ""), nil
		}
		return jsonReply("source-link-ping", `{}`), nil
	default:
		return response(http.StatusNotFound, "text/plain", "", ""), nil
	}
}

func (f *mcpFixture) initialize(req *http.Request, session string, rawID json.RawMessage) (*http.Response, error) {
	f.mu.Lock()
	if session != "" && !f.sessions[session] {
		f.mu.Unlock()
		f.t.Errorf("initialize reused an unknown session id %q", session)
		return response(http.StatusNotFound, "text/plain", "", ""), nil
	}
	f.nextID++
	id := fmt.Sprintf("sess-%d", f.nextID)
	f.sessions[id] = true
	f.mu.Unlock()

	replyID := healthProbeID
	if f.replyID != "" {
		replyID = f.replyID
	}
	result := `{"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"fixture"}}`
	payload := fmt.Sprintf(`{"jsonrpc":"2.0","id":%q,"result":%s}`, replyID, result)
	event := "event: message\ndata: " + payload + "\n\n"

	body := io.Reader(strings.NewReader(event))
	if f.sseHold != nil {
		body = &heldStream{prefix: []byte(event), hold: f.sseHold}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"text/event-stream"}, "Mcp-Session-Id": {id}},
		Body:       io.NopCloser(body),
	}, nil
}

// heldStream emits the reply event, then blocks until hold is closed before EOF.
// A correct incremental parser returns as soon as the event is complete; a parser
// that reads to EOF would hang here.
type heldStream struct {
	prefix []byte
	hold   <-chan struct{}
	done   bool
}

func (h *heldStream) Read(p []byte) (int, error) {
	if len(h.prefix) > 0 {
		n := copy(p, h.prefix)
		h.prefix = h.prefix[n:]
		return n, nil
	}
	if h.done {
		return 0, io.EOF
	}
	<-h.hold
	h.done = true
	return 0, io.EOF
}

func (f *mcpFixture) live() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sessions)
}

func response(status int, contentType, body, session string) *http.Response {
	header := http.Header{}
	if contentType != "" {
		header.Set("Content-Type", contentType)
	}
	if session != "" {
		header.Set("Mcp-Session-Id", session)
	}
	return &http.Response{StatusCode: status, Header: header, Body: io.NopCloser(strings.NewReader(body))}
}

func jsonReply(id, result string) *http.Response {
	body := fmt.Sprintf(`{"jsonrpc":"2.0","id":%q,"result":%s}`, id, result)
	return response(http.StatusOK, "application/json", body, "")
}

// The core leak regression: a long-lived source is probed many times, and the
// upstream session count must stay at exactly one. Before the health client, every
// probe posted a fresh initialize and dropped the session id, so this count grew
// without bound (~one leaked session per probe interval).
func TestHealthReusesOneSessionAcrossProbes(t *testing.T) {
	fixture := newMCPFixture(t)
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	ctx := context.Background()

	for probe := 0; probe < 12; probe++ {
		if _, err := server.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
			t.Fatalf("probe %d failed: %v", probe, err)
		}
		if got := fixture.live(); got != 1 {
			t.Fatalf("after probe %d there are %d live sessions, want exactly 1", probe, got)
		}
	}

	server.CloseHealthSession(ctx)
	if got := fixture.live(); got != 0 {
		t.Fatalf("after close there are %d live sessions, want 0", got)
	}
}

// A stale session (server answers 404 to the ping) must be dropped so the next
// interval re-initializes cleanly rather than looping on a dead id, and must not
// leak the replacement.
func TestHealthReinitializesAfterStaleSession(t *testing.T) {
	fixture := newMCPFixture(t)
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	ctx := context.Background()

	if _, err := server.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
		t.Fatalf("first probe failed: %v", err)
	}
	fixture.mu.Lock()
	fixture.failPing = true
	fixture.mu.Unlock()
	if _, err := server.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("a stale-session ping was accepted as healthy")
	}
	fixture.mu.Lock()
	fixture.failPing = false
	fixture.mu.Unlock()
	if _, err := server.Probe(ctx, &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
		t.Fatalf("probe after stale session did not recover: %v", err)
	}
	if got := fixture.live(); got != 1 {
		t.Fatalf("after recovery there are %d live sessions, want 1", got)
	}
}

// An initialize reply that arrives in a complete SSE event while the stream stays
// open must be accepted before EOF. A parser that reads to EOF hangs here.
func TestHealthAcceptsCompleteEventBeforeEOF(t *testing.T) {
	hold := make(chan struct{})
	defer close(hold)
	fixture := newMCPFixture(t)
	fixture.sseHold = hold
	server := NewServer(fixture, "127.0.0.1", "mcp", "")

	done := make(chan error, 1)
	go func() {
		_, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"})
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("held-open SSE initialize was rejected: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("probe blocked on a held-open SSE stream instead of returning at the complete event")
	}
}

// A reply whose id does not match the probe must not go healthy: a delayed or
// reordered event from another request is not proof of a live initialize.
func TestHealthRejectsMismatchedReplyID(t *testing.T) {
	fixture := newMCPFixture(t)
	fixture.replyID = "not-the-probe-id"
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("an initialize reply with a foreign id was accepted")
	}
}

// sseData and the incremental parser must agree on multi-event streams: only the
// event carrying the matching result counts, a leading unrelated event does not
// short-circuit to healthy or to a false negative.
func TestReadSSEInitializeSkipsUnrelatedEvents(t *testing.T) {
	stream := "event: ping\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"other\",\"result\":{}}\n\n" +
		"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"source-link-health\",\"result\":{\"protocolVersion\":\"2025-06-18\"}}\n\n"
	if err := readSSEInitialize(bufio.NewReader(bytes.NewBufferString(stream))); err != nil {
		t.Fatalf("matching event after an unrelated one was rejected: %v", err)
	}
}
