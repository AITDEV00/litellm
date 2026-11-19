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
	"time"
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

// healthPingID is the JSON-RPC id every health ping carries, correlated the same
// way so a stale or unrelated event cannot stand in for a live session.
const healthPingID = "source-link-ping"

// maxHealthMessage bounds one aggregated SSE/JSON reply message.
const maxHealthMessage = 256 << 10

// healthCleanupTimeout bounds the DELETE issued after a failed or closed session,
// so a stuck origin cannot pin teardown. It is independent of the failed request's
// context, which may already be canceled by the time cleanup runs.
const healthCleanupTimeout = 5 * time.Second

// mcpHealth owns the per-source probe session.
type mcpHealth struct {
	mu        sync.Mutex
	sessionID string
}

// Close deletes the health session if one is open, so a removed or restarted
// source does not leave an orphaned upstream session behind. Cleanup uses its own
// bounded context so a canceled caller context cannot strand the DELETE.
func (h *mcpHealth) Close(_ context.Context, d *Server) {
	h.mu.Lock()
	session := h.sessionID
	h.sessionID = ""
	h.mu.Unlock()
	if session == "" {
		return
	}
	_ = h.deleteSession(d, session)
}

// deleteSession terminates one upstream session best effort, on a fresh bounded
// context. A server that has already forgotten the session answers 404; that is a
// success for teardown, so any status is accepted but a transport-level error is
// returned to the caller.
func (h *mcpHealth) deleteSession(d *Server, session string) error {
	ctx, cancel := context.WithTimeout(context.Background(), healthCleanupTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, d.healthURL(), nil)
	if err != nil {
		return err
	}
	request.Header.Set("Mcp-Session-Id", session)
	resp, err := d.origin.RoundTrip(request)
	if err != nil {
		return err
	}
	_ = resp.Body.Close()
	return nil
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
		// than looping on a dead handle. Cleanup uses its own bounded context, so a
		// canceled ping cannot strand the delete.
		_ = h.deleteSession(d, h.sessionID)
		h.sessionID = ""
		return fmt.Errorf("health session no longer usable: %w", err)
	}
	return nil
}

// initialize performs the MCP handshake: POST initialize, capture any session id
// the moment HTTP admits the request, validate the JSON-RPC result and negotiated
// version, then send the completing initialized notification whose acceptance is
// part of readiness. Any failure after allocation deletes the session so a
// rejected handshake never strands it.
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
	// Capture the session id immediately: once the server accepted the initialize
	// it may have allocated one, and every later failure branch must be able to
	// delete it. The session is only recorded as health on complete success.
	session := strings.TrimSpace(resp.Header.Get("Mcp-Session-Id"))
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusMultipleChoices && resp.StatusCode < http.StatusBadRequest {
		h.cleanupAfterFailure(d, session)
		return fmt.Errorf("origin mcp initialize refused a redirect: %d", resp.StatusCode)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		h.cleanupAfterFailure(d, session)
		return fmt.Errorf("origin mcp initialize failed: %d", resp.StatusCode)
	}
	negotiated, err := readJSONRPCResult(resp.Header.Get("Content-Type"), resp.Body, healthProbeID)
	if err != nil {
		h.cleanupAfterFailure(d, session)
		return fmt.Errorf("origin mcp initialize result invalid: %w", err)
	}
	if err := validateInitializeResultShape(negotiated); err != nil {
		h.cleanupAfterFailure(d, session)
		return err
	}
	// The initialized notification completes the handshake. A serverless id is
	// tolerated only if the notification is still accepted; a rejected notification
	// means the session was never initialized and must not read as healthy.
	if err := h.notifyInitialized(ctx, d, session); err != nil {
		h.cleanupAfterFailure(d, session)
		return fmt.Errorf("origin mcp initialized notification not accepted: %w", err)
	}
	h.sessionID = session
	return nil
}

// cleanupAfterFailure deletes a session the server allocated before the handshake
// completed. Initialization already failed, so a cleanup transport error (already
// expired session) is expected and must not redefine the result as healthy.
func (h *mcpHealth) cleanupAfterFailure(d *Server, session string) {
	if session == "" {
		return
	}
	_ = h.deleteSession(d, session)
}

// notifyInitialized sends the MCP `notifications/initialized` that completes the
// handshake. Its acceptance is part of readiness: the installed SDK answers an
// ordinary client 202 here and 406 when the Accept header is missing, so a
// declined notification means the session is not actually initialized.
func (h *mcpHealth) notifyInitialized(ctx context.Context, d *Server, session string) error {
	body := []byte(`{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, d.healthURL(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	if session != "" {
		request.Header.Set("Mcp-Session-Id", session)
	}
	resp, err := d.origin.RoundTrip(request)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("origin mcp initialized notification rejected: %d", resp.StatusCode)
	}
	// A notification has no JSON-RPC reply; draining a bounded prefix lets the
	// transport reuse the connection without waiting for an absent body.
	_, _ = io.CopyN(io.Discard, resp.Body, 1024) //nolint:errcheck // bounded drain, ignore short read
	return nil
}

// ping confirms an open session still answers, without allocating a new one. It
// correlates the JSON-RPC reply by id so a redirect, an HTML page, a 202 with no
// result, or an unrelated event cannot be read as healthy.
func (h *mcpHealth) ping(ctx context.Context, d *Server) error {
	body := []byte(`{"jsonrpc":"2.0","id":"` + healthPingID + `","method":"ping"}`)
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
	if resp.StatusCode >= http.StatusMultipleChoices && resp.StatusCode < http.StatusBadRequest {
		return fmt.Errorf("origin mcp ping refused a redirect: %d", resp.StatusCode)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("origin mcp ping failed: %d", resp.StatusCode)
	}
	_, err = readJSONRPCResult(resp.Header.Get("Content-Type"), resp.Body, healthPingID)
	return err
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

// readJSONRPCResult parses one JSON-RPC 2.0 reply, incrementally for SSE, and
// returns the raw result of the message whose id matches want. It returns as soon
// as that message completes, so a held-open stream that already delivered the
// reply does not block until EOF.
func readJSONRPCResult(contentType string, body io.Reader, want string) (json.RawMessage, error) {
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(contentType)), "text/event-stream") {
		return readSSEResult(body, want)
	}
	payload, err := io.ReadAll(io.LimitReader(body, maxHealthMessage+1))
	if err != nil {
		return nil, err
	}
	if len(payload) > maxHealthMessage {
		return nil, errors.New("origin mcp reply exceeds the probe limit")
	}
	return validateJSONRPCResult(payload, want)
}

// readSSEResult incrementally parses a text/event-stream and returns the result of
// the first complete event whose JSON-RPC id matches want. It bounds total and
// per-event bytes so a malicious or broken stream cannot exhaust memory.
func readSSEResult(body io.Reader, want string) (json.RawMessage, error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 4096), maxHealthMessage)
	var event bytes.Buffer
	total := 0
	for scanner.Scan() {
		line := scanner.Text()
		total += len(line) + 1
		if total > maxHealthMessage {
			return nil, errors.New("origin mcp reply SSE exceeds the probe limit")
		}
		if line == "" {
			if event.Len() > 0 {
				if result, err := validateJSONRPCResult(event.Bytes(), want); err == nil {
					return result, nil
				}
				event.Reset()
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			event.WriteString(strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("origin mcp reply SSE read failed: %w", err)
	}
	if event.Len() > 0 {
		if result, err := validateJSONRPCResult(event.Bytes(), want); err == nil {
			return result, nil
		}
	}
	return nil, errors.New("origin mcp probe did not receive a matching JSON-RPC result")
}

// validateJSONRPCResult enforces the JSON-RPC 2.0 reply envelope for request id
// want: the right version, a matching id, a result present, and no error. A
// different request's later event, an error page, or an empty body is refused.
func validateJSONRPCResult(payload []byte, want string) (json.RawMessage, error) {
	var reply struct {
		JSONRPC string          `json:"jsonrpc"`
		ID      json.RawMessage `json:"id"`
		Result  json.RawMessage `json:"result"`
		Error   json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(payload, &reply); err != nil {
		return nil, errors.New("origin mcp reply is not JSON")
	}
	if reply.JSONRPC != "2.0" {
		return nil, errors.New("origin mcp reply is not JSON-RPC 2.0")
	}
	if len(reply.Error) > 0 && string(reply.Error) != "null" {
		return nil, errors.New("origin mcp reply returned an error")
	}
	if len(reply.Result) == 0 || string(reply.Result) == "null" {
		return nil, errors.New("origin mcp reply returned no result")
	}
	if !idMatchesValue(reply.ID, want) {
		return nil, fmt.Errorf("origin mcp reply id does not match %q", want)
	}
	return reply.Result, nil
}

// idMatchesValue accepts want as a JSON string id.
func idMatchesValue(raw json.RawMessage, want string) bool {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return false
	}
	return value == want
}

// validateInitializeResultShape checks the negotiated protocol version and the
// required initialize-result fields. The MCP initialize result carries a
// protocolVersion, a capabilities object, and a serverInfo object; an empty or
// unrelated result object is not a completed handshake.
func validateInitializeResultShape(result json.RawMessage) error {
	var shape struct {
		ProtocolVersion string          `json:"protocolVersion"`
		Capabilities    json.RawMessage `json:"capabilities"`
		ServerInfo      json.RawMessage `json:"serverInfo"`
	}
	if err := json.Unmarshal(result, &shape); err != nil {
		return errors.New("origin mcp initialize result is not an object")
	}
	if shape.ProtocolVersion == "" {
		return errors.New("origin mcp initialize result has no protocolVersion")
	}
	if !supportedProtocolVersion(shape.ProtocolVersion) {
		return fmt.Errorf("origin mcp negotiated an unsupported protocol version %q", shape.ProtocolVersion)
	}
	if len(shape.Capabilities) == 0 || string(shape.Capabilities) == "null" {
		return errors.New("origin mcp initialize result has no capabilities")
	}
	if len(shape.ServerInfo) == 0 || string(shape.ServerInfo) == "null" {
		return errors.New("origin mcp initialize result has no serverInfo")
	}
	return nil
}

// supportedProtocolVersion reports whether the server's negotiated version is one
// this health client understands. Only the revision it requests is accepted; a
// server that answers a different revision would need different client behavior,
// so treating it as ready would misrepresent the handshake.
func supportedProtocolVersion(version string) bool {
	return version == mcpProtocolVersion
}
