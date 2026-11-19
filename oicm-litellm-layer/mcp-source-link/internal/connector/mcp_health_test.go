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

// mcpFixture is a protocol-conformant Streamable HTTP MCP server, strict about the
// parts that matter: it rejects a request whose Accept header does not admit
// JSON-RPC replies, initialize allocates a session and returns its id, a
// notifications/initialized must name a live session, a ping on a live session is
// answered with a correlated reply, and DELETE terminates the session. It fails
// the test if a request carries a session id it never issued. Knobs let tests force
// each failure mode the health client must reject.
type mcpFixture struct {
	t        *testing.T
	mu       sync.Mutex
	sessions map[string]bool
	nextID   int
	// failPing makes a live ping answer a bad response so the session is dropped.
	failPing bool
	// pingMode selects a specific malformed ping reply (see pingReply).
	pingMode string
	// rejectNotification makes the initialized notification answer 406 as the SDK
	// does when the Accept header is missing.
	rejectNotification bool
	// sseHold, when set, keeps the initialize response stream open after the
	// reply event so the connector must parse incrementally rather than wait EOF.
	sseHold <-chan struct{}
	// replyID overrides the initialize reply id to test id correlation.
	replyID string
	// initMode selects a malformed initialize reply (see initialize).
	initMode string
}

func newMCPFixture(t *testing.T) *mcpFixture {
	t.Helper()
	return &mcpFixture{t: t, sessions: map[string]bool{}}
}

// acceptsJSONReply reports whether the Accept header admits a JSON-RPC/SSE reply.
// A real Streamable HTTP server returns 406 when the client does not accept a
// reply media type; the fixture enforces the same rule so a client that omits the
// header cannot be mistaken for protocol-conformant.
func acceptsReply(req *http.Request) bool {
	accept := req.Header.Get("Accept")
	return strings.Contains(accept, "application/json") || strings.Contains(accept, "text/event-stream")
}

func (f *mcpFixture) RoundTrip(req *http.Request) (*http.Response, error) {
	if err := req.Context().Err(); err != nil {
		return nil, err
	}
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
	// A JSON-RPC request (not a notification, not a DELETE) must accept a reply;
	// otherwise the server answers 406 like the SDK. This is the R1 defect: a
	// client that posts initialize without Accept must not be treated as healthy.
	if req.Method != http.MethodDelete && len(message.ID) > 0 && !acceptsReply(req) {
		return response(http.StatusNotAcceptable, "application/json", `{"error":"not acceptable"}`, ""), nil
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
		return f.initialize(session, message.ID)
	case message.Method == "notifications/initialized":
		if !acceptsReply(req) {
			return response(http.StatusNotAcceptable, "application/json", `{"error":"not acceptable"}`, ""), nil
		}
		f.mu.Lock()
		known := f.sessions[session]
		reject := f.rejectNotification
		f.mu.Unlock()
		if !known {
			f.t.Errorf("initialized notification for an unknown session %q", session)
		}
		if reject {
			return response(http.StatusNotAcceptable, "application/json", `{"error":"rejected"}`, ""), nil
		}
		return response(http.StatusAccepted, "", "", ""), nil
	case message.Method == "ping":
		f.mu.Lock()
		known := f.sessions[session]
		failPing := f.failPing
		mode := f.pingMode
		f.mu.Unlock()
		if !known {
			f.t.Errorf("ping for an unknown session %q", session)
			return response(http.StatusNotFound, "text/plain", "", ""), nil
		}
		if failPing {
			return response(http.StatusNotFound, "text/plain", "", ""), nil
		}
		return f.pingReply(mode), nil
	default:
		return response(http.StatusNotFound, "text/plain", "", ""), nil
	}
}

// pingReply returns the ping reply for a live session, varying by mode to test
// each malformed response the client must reject.
func (f *mcpFixture) pingReply(mode string) *http.Response {
	switch mode {
	case "redirect":
		return response(http.StatusTemporaryRedirect, "text/html", `<a href="http://elsewhere">moved</a>`, "")
	case "no-content":
		return response(http.StatusAccepted, "application/json", ``, "")
	case "html":
		return response(http.StatusOK, "text/html", `<html>hi</html>`, "")
	case "wrong-id":
		return jsonReplyRaw(`{"jsonrpc":"2.0","id":"not-the-ping","result":{}}`)
	case "no-id":
		return jsonReplyRaw(`{"jsonrpc":"2.0","result":{}}`)
	case "error":
		return jsonReplyRaw(`{"jsonrpc":"2.0","id":"source-link-ping","error":{"code":-32603,"message":"boom"}}`)
	case "held-sse":
		event := "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"source-link-ping\",\"result\":{}}\n\n"
		hold := make(chan struct{})
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"text/event-stream"}},
			Body:       io.NopCloser(&heldStream{prefix: []byte(event), hold: hold}),
		}
	default:
		return jsonReply(healthPingID, `{}`)
	}
}

func (f *mcpFixture) initialize(session string, rawID json.RawMessage) (*http.Response, error) {
	f.mu.Lock()
	if session != "" && !f.sessions[session] {
		f.mu.Unlock()
		f.t.Errorf("initialize reused an unknown session id %q", session)
		return response(http.StatusNotFound, "text/plain", "", ""), nil
	}
	f.nextID++
	id := fmt.Sprintf("sess-%d", f.nextID)
	f.sessions[id] = true
	mode := f.initMode
	f.mu.Unlock()

	replyID := healthProbeID
	if f.replyID != "" {
		replyID = f.replyID
	}
	result := `{"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"fixture"}}`
	switch mode {
	case "empty-result":
		result = `{}`
	case "unsupported-version":
		result = `{"protocolVersion":"1999-01-01","capabilities":{},"serverInfo":{"name":"fixture"}}`
	case "no-capabilities":
		result = `{"protocolVersion":"2025-06-18","serverInfo":{"name":"fixture"}}`
	case "jsonrpc-error":
		payload := fmt.Sprintf(`{"jsonrpc":"2.0","id":%q,"error":{"code":-32601,"message":"wrong id"}}`, replyID)
		return f.finish(id, payload), nil
	case "read-error":
		// Headers carry the allocated session; the body read fails as it would when
		// the caller's context is canceled mid-read. The client must still delete.
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": {"text/event-stream"}, "Mcp-Session-Id": {id}},
			Body:       errReader{err: context.Canceled},
		}, nil
	}
	payload := fmt.Sprintf(`{"jsonrpc":"2.0","id":%q,"result":%s}`, replyID, result)
	return f.finish(id, payload), nil
}

// errReader returns err on every Read, modeling a body read that fails after
// headers were received.
type errReader struct{ err error }

func (e errReader) Read([]byte) (int, error) { return 0, e.err }
func (e errReader) Close() error             { return nil }

func (f *mcpFixture) finish(id, payload string) *http.Response {
	event := "event: message\ndata: " + payload + "\n\n"
	var body io.Reader = strings.NewReader(event)
	if f.sseHold != nil {
		body = &heldStream{prefix: []byte(event), hold: f.sseHold}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"text/event-stream"}, "Mcp-Session-Id": {id}},
		Body:       io.NopCloser(body),
	}
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

func jsonReplyRaw(body string) *http.Response {
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

// A reply whose id does not match the probe must not go healthy, and the session
// the server allocated for that initialize must be deleted, not leaked.
func TestHealthRejectsMismatchedReplyIDWithoutLeaking(t *testing.T) {
	fixture := newMCPFixture(t)
	fixture.replyID = "not-the-probe-id"
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("an initialize reply with a foreign id was accepted")
	}
	if got := fixture.live(); got != 0 {
		t.Fatalf("a rejected initialize left %d live session(s), want 0", got)
	}
}

// A rejected initialized notification (SDK 406 when Accept is missing) must make
// the probe unhealthy and must not leave the allocated session behind. This is the
// R1 defect: the old client ignored the rejection and reported healthy. The default
// fixture enforces the media rule, so a client that omits Accept gets 406 here,
// which is exactly the defect this guards.
func TestHealthFailsWhenInitializedNotificationRejected(t *testing.T) {
	fixture := newMCPFixture(t)
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
		t.Fatalf("a valid initialized notification was rejected: %v", err)
	}
	// The default fixture answers 406 to a notification without Accept, so a client
	// that drops the header (the R1 mutation) fails the health probe above. A
	// server-side rejection knob additionally exercises an explicit rejection.
	fixture2 := newMCPFixture(t)
	fixture2.rejectNotification = true
	server2 := NewServer(fixture2, "127.0.0.1", "mcp", "")
	if _, err := server2.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("a rejected initialized notification was reported as healthy")
	}
	if got := fixture2.live(); got != 0 {
		t.Fatalf("a rejected notification left %d live session(s), want 0", got)
	}
}

// Every malformed initialize reply the server can produce must fail health and
// clean up the session it allocated.
func TestHealthInitializeFailuresCleanUpSessions(t *testing.T) {
	for _, mode := range []string{"empty-result", "unsupported-version", "no-capabilities", "jsonrpc-error"} {
		t.Run(mode, func(t *testing.T) {
			fixture := newMCPFixture(t)
			fixture.initMode = mode
			server := NewServer(fixture, "127.0.0.1", "mcp", "")
			if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
				t.Fatalf("initialize mode %q was accepted as healthy", mode)
			}
			if got := fixture.live(); got != 0 {
				t.Fatalf("initialize mode %q left %d live session(s), want 0", mode, got)
			}
		})
	}
}

// A canceled initialize must not strand the session the server allocated. The
// fixture allocates the session and returns a live response, but the body read
// fails (the caller's context is canceled), so the client must delete the session
// it captured from the header before the read failed.
func TestHealthCanceledInitializeLeavesNoSession(t *testing.T) {
	fixture := newMCPFixture(t)
	fixture.initMode = "read-error"
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
		t.Fatal("an initialize whose reply read failed was reported as healthy")
	}
	if got := fixture.live(); got != 0 {
		t.Fatalf("an initialize whose reply read failed left %d live session(s), want 0", got)
	}
}

// A malformed ping reply over a live session must make the probe unhealthy and drop
// the dead session, for every malformed shape the server can send.
func TestHealthPingRejectsBadReplies(t *testing.T) {
	for _, mode := range []string{"redirect", "no-content", "html", "wrong-id", "error"} {
		t.Run(mode, func(t *testing.T) {
			fixture := newMCPFixture(t)
			server := NewServer(fixture, "127.0.0.1", "mcp", "")
			if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
				t.Fatalf("first probe failed: %v", err)
			}
			fixture.mu.Lock()
			fixture.pingMode = mode
			fixture.mu.Unlock()
			if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err == nil {
				t.Fatalf("ping mode %q was accepted as healthy", mode)
			}
			if got := fixture.live(); got != 0 {
				t.Fatalf("ping mode %q left %d live session(s), want 0", mode, got)
			}
		})
	}
}

// A complete matching ping delivered as an SSE event on a held-open stream must be
// accepted before EOF. The old byte-count drain waited for 4096 bytes and stalled.
func TestHealthPingAcceptsHeldOpenSSE(t *testing.T) {
	fixture := newMCPFixture(t)
	server := NewServer(fixture, "127.0.0.1", "mcp", "")
	if _, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"}); err != nil {
		t.Fatalf("first probe failed: %v", err)
	}
	fixture.mu.Lock()
	fixture.pingMode = "held-sse"
	fixture.mu.Unlock()
	done := make(chan error, 1)
	go func() {
		_, err := server.Probe(context.Background(), &dispatchpb.ProbeRequest{Nonce: "n"})
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("held-open SSE ping was rejected: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ping blocked on a held-open SSE stream instead of returning at the complete event")
	}
}

// The result parser must skip an unrelated leading event and accept the matching
// one, so a multi-event stream is not a false negative or a false positive.
func TestReadSSEResultSkipsUnrelatedEvents(t *testing.T) {
	stream := "event: ping\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"other\",\"result\":{}}\n\n" +
		"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":\"source-link-health\",\"result\":{\"protocolVersion\":\"2025-06-18\"}}\n\n"
	if _, err := readSSEResult(bufio.NewReader(bytes.NewBufferString(stream)), healthProbeID); err != nil {
		t.Fatalf("matching event after an unrelated one was rejected: %v", err)
	}
}
