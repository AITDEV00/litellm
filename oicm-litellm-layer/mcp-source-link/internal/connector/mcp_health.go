package connector

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
)

// The MCP health probe in front of a source is a first-class MCP client, not a
// bare HTTP poke. A conformant Streamable HTTP server answers `initialize` by
// allocating a session (an `Mcp-Session-Id` header) and expects the client to
// finish the handshake with `notifications/initialized` and to end the session
// with `DELETE`. A probe that posts a fresh `initialize` every interval and drops
// the response body therefore leaks one live session per interval: the server
// never sees a delete, and closing the HTTP body alone does not terminate an SDK
// session.
//
// The health client keeps exactly one session per source, reuses it across probe
// intervals, sends the finishing notification on the first successful initialize,
// and deletes it when the connector/source closes. It never shares that session
// with the gateway's real client routing: this is a private, probe-owned session.

// mcpProtocolVersion is the version the health client negotiates.
const mcpProtocolVersion = "2025-06-18"

// healthProbeID is the JSON-RPC id every health initialize carries, so a delayed
// or reordered event is only accepted when it answers this exact request.
const healthProbeID = "source-link-health"

// maxHealthMessage bounds one aggregated SSE/JSON initialize message.
const maxHealthMessage = 256 << 10

// mcpHealth owns the per-source probe session.
type mcpHealth struct {
	mu        sync.Mutex
	sessionID string
	initializ bool
}

// Close deletes the health session if one is open, so a removed or restarted
// source does not leave an orphaned upstream session behind.
func (h *mcpHealth) Close(ctx context.Context, d *Server) {
	h.mu.Lock()
	session := h.sessionID
	h.sessionID = ""
	h.initializ = false
	h.mu.Unlock()
	if session == "" {
		return
	}
	h.deleteSession(ctx, d, session)
}

// deleteSession terminates one upstream session, best effort. A server that has
// already forgotten the session answers 404; that is a success for teardown.
func (h *mcpHealth) deleteSession(ctx context.Context, d *Server, session string) {
	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, d.healthURL(), nil)
	if err != nil {
		return
	}
	request.Header.Set("Mcp-Session-Id", session)
	if resp, err := d.origin.RoundTrip(request); err == nil {
		_ = resp.Body.Close()
	}
}

// Probe runs one health cycle: initialize (and remember the session) if none is
// open, otherwise confirm the existing session still answers. It returns an error
// only when the origin is not a usable MCP backend.
func (h *mcpHealth) Probe(ctx context.Context, d *Server) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.sessionID == "" {
		return h.initialize(ctx, d)
	}
	if err := h.ping(ctx, d); err != nil {
		// The server may have expired or rejected the session; delete whatever is
		// left and drop the id so the next interval re-initializes cleanly rather
		// than looping on a dead handle. The delete is best effort: the ping said
		// the session is unusable, so a 404 from the delete is expected.
		h.deleteSession(ctx, d, h.sessionID)
		h.sessionID = ""
		h.initializ = false
		return fmt.Errorf("health session no longer usable: %w", err)
	}
	return nil
}

// initialize performs the MCP handshake, captures the session id, and sends the
// completing notification. The initialize result is read incrementally so a
// server that answers with a held-open SSE stream is accepted as soon as its
// matching event arrives rather than waiting for EOF (which may never come).
func (h *mcpHealth) initialize(ctx context.Context, d *Server) error {
	body, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      healthProbeID,
		"method":  "initialize",
		"params": map[string]any{
			"protocolVersion": mcpProtocolVersion,
			"capabilities":    map[string]any{},
			"clientInfo":      map[string]any{"name": "source-link-health", "version": "1"},
		},
	})
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, d.healthURL(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	resp, err := d.origin.RoundTrip(request)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusMultipleChoices && resp.StatusCode < http.StatusBadRequest {
		return fmt.Errorf("origin mcp initialize refused a redirect: %d", resp.StatusCode)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("origin mcp initialize failed: %d", resp.StatusCode)
	}
	session := strings.TrimSpace(resp.Header.Get("Mcp-Session-Id"))
	if err := readInitializeResult(resp.Header.Get("Content-Type"), resp.Body); err != nil {
		return err
	}
	h.sessionID = session
	// A server that issues no session id still proved it speaks MCP; keep probing
	// it statelessly. Only send the finishing notification when a session exists,
	// because an unsupported notification is a harmless 400 but a real one carries
	// the session.
	if session != "" {
		h.notifyInitialized(ctx, d, session)
	}
	h.initializ = true
	return nil
}

// notifyInitialized sends the MCP `notifications/initialized` that completes the
// handshake. It is best effort: the session is already proven usable by the
// initialize result, so a notification the server declines must not fail health.
func (h *mcpHealth) notifyInitialized(ctx context.Context, d *Server, session string) {
	body := []byte(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, d.healthURL(), bytes.NewReader(body))
	if err != nil {
		return
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Mcp-Session-Id", session)
	if resp, err := d.origin.RoundTrip(request); err == nil {
		_ = resp.Body.Close()
	}
}

// ping confirms an open session still answers, without allocating a new one. It
// uses the standard MCP `ping` request under the existing session id.
func (h *mcpHealth) ping(ctx context.Context, d *Server) error {
	body := []byte(`{"jsonrpc":"2.0","id":"source-link-ping","method":"ping"}`)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, d.healthURL(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("Mcp-Session-Id", h.sessionID)
	resp, err := d.origin.RoundTrip(request)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusBadRequest {
		return fmt.Errorf("origin mcp ping failed: %d", resp.StatusCode)
	}
	// A ping reply may be empty in some servers; a 2xx with a live session is the
	// liveness proof. Drain a bounded prefix so the transport can reuse the conn.
	_, _ = io.CopyN(io.Discard, resp.Body, 4096) //nolint:errcheck // bounded drain, ignore short read
	return nil
}

// healthURL is the endpoint the probe dials.
func (d *Server) healthURL() string {
	path := d.path
	if path == "" {
		path = "/mcp"
	}
	target := url.URL{Scheme: "http", Host: d.authority, Path: path}
	return target.String()
}

// readInitializeResult reads the initialize reply, incrementally for SSE, and
// accepts it only when it is a JSON-RPC 2.0 response carrying the matching id and
// a result with no error. It stops as soon as the matching event is complete, so a
// deliberately long-lived stream that keeps the connection open after the reply is
// still accepted before EOF.
func readInitializeResult(contentType string, body io.Reader) error {
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(contentType)), "text/event-stream") {
		return readSSEInitialize(body)
	}
	payload, err := io.ReadAll(io.LimitReader(body, maxHealthMessage+1))
	if err != nil {
		return err
	}
	if len(payload) > maxHealthMessage {
		return errors.New("origin mcp initialize response exceeds the probe limit")
	}
	return validateInitializeResponse(payload)
}

// readSSEInitialize incrementally parses a text/event-stream body and returns as
// soon as one complete event carries a valid initialize result. It bounds total
// bytes and per-event bytes so a malicious or broken stream cannot exhaust memory.
func readSSEInitialize(body io.Reader) error {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 4096), maxHealthMessage)
	var event bytes.Buffer
	total := 0
	for scanner.Scan() {
		line := scanner.Text()
		total += len(line) + 1
		if total > maxHealthMessage {
			return errors.New("origin mcp initialize SSE exceeds the probe limit")
		}
		// An empty line terminates one SSE event; flush and test it.
		if line == "" {
			if event.Len() > 0 {
				if validateInitializeResponse(event.Bytes()) == nil {
					return nil
				}
				event.Reset()
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			data := strings.TrimPrefix(line, "data:")
			event.WriteString(strings.TrimPrefix(data, " "))
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("origin mcp initialize SSE read failed: %w", err)
	}
	// A final event without a trailing blank line still counts.
	if event.Len() > 0 && validateInitializeResponse(event.Bytes()) == nil {
		return nil
	}
	return errors.New("origin mcp probe did not receive a JSON-RPC initialize result")
}

// validateInitializeResponse enforces the JSON-RPC 2.0 initialize envelope: the
// right version, our request id, a result present, and no error. A different
// request's later event, an error page, or an empty body is refused.
func validateInitializeResponse(payload []byte) error {
	var reply struct {
		JSONRPC string          `json:"jsonrpc"`
		ID      json.RawMessage `json:"id"`
		Result  json.RawMessage `json:"result"`
		Error   json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(payload, &reply); err != nil {
		return errors.New("origin mcp initialize reply is not JSON")
	}
	if reply.JSONRPC != "2.0" {
		return errors.New("origin mcp initialize reply is not JSON-RPC 2.0")
	}
	if len(reply.Error) > 0 && string(reply.Error) != "null" {
		return errors.New("origin mcp initialize returned an error")
	}
	if len(reply.Result) == 0 || string(reply.Result) == "null" {
		return errors.New("origin mcp initialize returned no result")
	}
	if !idMatches(reply.ID) {
		return errors.New("origin mcp initialize reply id does not match the probe")
	}
	return nil
}

// idMatches accepts the health echo id as a JSON string.
func idMatches(raw json.RawMessage) bool {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return false
	}
	return value == healthProbeID
}
